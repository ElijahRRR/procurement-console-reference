/** 模拟执行引擎对着内存假服务端（契约 §5，边角路径照 backend/services/execution.py）整条跑：
 *  正常、护栏拦截、提交后结果不明（只登记一次）、上游取消、账号占用、断网中断、
 *  结果重发与租约预算、登记响应丢失后浏览器重发、护栏响应丢失后重发、服务端要求中止的原因码、
 *  护栏 buyer 项拦截（服务端自己中止这一笔并结束批次：不上报结果、不调用结束批次）等路径。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { PluginClient } from "../build/core/client.js";
import { BUYER_CHANGED_TEXT } from "../build/core/codes.js";
import { RunEngine, describeStartError } from "../build/core/engine.js";
import { buildCheckoutFacts, failureResult } from "../build/core/simulate.js";
import { DEMO_CID, DEMO_TOKEN, END_REASON_ZH, OTHER_CID, createFakeServer } from "./fake-server.mjs";

/** 演示账号登记的买家号 ID：引擎开始批次时带上它（浏览器里是当前页面读到的那个）。 */
const CID = DEMO_CID;

const TASKS = () => [
  { upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69" },
  { upstream_order_no: "PO-DEMO-0002", asin: "B0DEMO0002", quantity: 2, unit_price: "12.50", price_cap: "25.00" },
];

function setup({ outcome = "pass", server = {}, engine = {}, wrapFetch = null } = {}) {
  const srv = createFakeServer({ tasks: TASKS(), ...server });
  const api = new PluginClient({
    baseUrl: "http://127.0.0.1:8790",
    token: DEMO_TOKEN,
    extVersion: "0.2.0",
    fetch: wrapFetch ? wrapFetch(srv) : srv.fetch,
    sleep: async () => {},
    retryDelaysMs: [0, 0],
  });
  const logs = [];
  const views = [];
  /** 「这一页」此刻读到的身份：引擎补报身份、取结算事实里的买家号 ID 都读它。 */
  const page = { ident: IDENT };
  /** 执行中途浏览器换登了另一个买家号（或退出登录）：页面上读到的变了，服务端也收到了新的上报。 */
  const switchBuyer = (ident = { ...IDENT, customer_id: OTHER_CID }) => {
    page.ident = ident;
    srv.setLogin(ident);
  };
  /** 同一个假服务端、同一页上再起一个引擎（一个引擎只跑一个批次；重新开始批次就是再起一个）。 */
  const newEngine = () => new RunEngine({
    api,
    outcome,
    now: srv.now,
    log: (level, text) => logs.push({ level, text }),
    onView: (v) => views.push(v),
    timers: { setInterval: () => 0, clearInterval: () => {} },
    // 等待（结果重发的退避、领取的稍后再试）推进假服务端的时钟：租约预算按真实时间推演
    sleep: async (ms) => { srv.advance(ms); },
    ownDeviceId: 5,
    // 宿主重读并上报身份：这里直接用客户端上报「这一页」此刻的身份（浏览器里是后台让各标签页重读、对账后上报）
    refreshIdentity: () => api.identity(page.ident),
    // 结算事实里的买家号 ID：取事实那一刻这一页上的读数
    readCustomerId: () => (page.ident.signed_in ? page.ident.customer_id : null),
    ...engine,
  });
  const eng = newEngine();
  return { srv, eng, logs, views, api, page, switchBuyer, newEngine };
}

const IDENT = { signed_in: true, customer_id: DEMO_CID, site: "US", page_url: "https://www.amazon.com/gp/css/order-history", conflict: false };

const paths = (srv, re) => srv.calls.filter((c) => re.test(c.path));
const resultsOf = (srv) => paths(srv, /\/result$/).map((c) => c.body);

test("正常路径：每笔都护栏放行、登记一次、上报成功，单号 SIM-<北京日期>-<尝试 ID>，最后结束批次", async () => {
  const { srv, eng, views } = setup();
  let notice = null;
  const s = await eng.run("manual", CID, (n) => { notice = n; });
  assert.equal(notice.started, true);
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "completed");
  assert.deepEqual(s.counts, { attempts: 2, succeeded: 2, failed: 0, blocked: 0, unknown: 0, aborted: 0 });
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["purchased", "purchased"]);
  const ok = resultsOf(srv);
  assert.deepEqual(ok.map((r) => [r.outcome, r.amazon_order_no]), [["succeeded", "SIM-20261105-7"], ["succeeded", "SIM-20261105-8"]]);
  assert.deepEqual(ok[1].amounts, { subtotal: "25.00", shipping: "0.00", tax: "0.00", total_before_tax: "25.00", total: "25.00" });
  for (const a of srv.state.attempts.values()) assert.equal(a.submits, 1);
  // 护栏上报的是模拟事实：小计 = 单价 × 数量，邮编取任务的，币种 USD
  const g = paths(srv, /\/guard$/).map((c) => c.body);
  assert.equal(g[1].subtotal, "25.00");
  assert.equal(g[1].ship_to.postal_code, "32960");
  assert.equal(g[1].currency, "USD");
  // 开始批次带当前页读到的买家号 ID；护栏事实里也带着取事实那一刻读到的买家号 ID
  assert.deepEqual(srv.calls[0].body, { customer_id: CID, trigger: "manual", simulated: true });
  assert.deepEqual(g.map((f) => f.customer_id), [CID, CID]);
  assert.equal(notice.message, "执行批次 #9 已开始");
  // 每笔的进度阶段
  const stages = paths(srv, /\/attempts\/7\/progress$/).map((c) => c.body.stage);
  assert.deepEqual(stages, ["started", "checkout", "before_submit"]);
  assert.equal(paths(srv, /\/finish$/).length, 1);
  // 结束原因：上报契约缺省的 finished（服务端的批次结束原因表认得它），插件的细分原因放在 detail
  assert.deepEqual(paths(srv, /\/finish$/)[0].body, { reason: "finished", detail: "completed" });
  assert.equal([...srv.state.runs.values()][0].ended, true);
  assert.equal(views.at(-1).phase, "ended");
  // 请求顺序：开始批次 → 领取 → 进度 → 护栏 → 上报身份 → 登记前确认 → 登记 → 结果
  const seq = srv.calls.slice(0, 9).map((c) => c.path.replace(/\/\d+(?=\/|$)/g, "/:id"));
  assert.deepEqual(seq, [
    "/plugin/v1/runs",
    "/plugin/v1/runs/:id/next",
    "/plugin/v1/attempts/:id/progress",
    "/plugin/v1/attempts/:id/progress",
    "/plugin/v1/attempts/:id/guard",
    "/plugin/v1/identity",
    "/plugin/v1/attempts/:id/progress",
    "/plugin/v1/attempts/:id/submitting",
    "/plugin/v1/attempts/:id/result",
  ]);
});

test("护栏拦截（超限价）：不登记提交，上报 failed + 拦截码，任务转异常，其他任务继续", async () => {
  const { srv, eng } = setup({ outcome: "price_over_cap" });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.blocked, 2);
  assert.equal(paths(srv, /\/submitting$/).length, 0);
  const g = paths(srv, /\/guard$/).map((c) => c.body.total);
  assert.deepEqual(g, ["79.69", "26.00"], "总额 = 限价 + 1");
  assert.deepEqual(resultsOf(srv).map((r) => [r.outcome, r.error_code]), [["failed", "PRICE_OVER_CAP"], ["failed", "PRICE_OVER_CAP"]]);
  assert.deepEqual(srv.state.tasks.map((t) => [t.status, t.status_reason]), [["exception", "PRICE_OVER_CAP"], ["exception", "PRICE_OVER_CAP"]]);
});

test("护栏拦截（配送超时）：送达日期 = 洛杉矶今天 + 上限 + 1", async () => {
  const { srv, eng } = setup({ outcome: "delivery_too_long" });
  await eng.run("manual", CID);
  const d = paths(srv, /\/guard$/)[0].body.delivery;
  // 假服务端时钟 2026-11-05T06:00Z = 洛杉矶 11-04 22:00；上限 10 天
  assert.equal(d.date, "2026-11-15");
  assert.equal(resultsOf(srv)[0].error_code, "DELIVERY_TOO_LONG");
  assert.equal(paths(srv, /\/submitting$/).length, 0);
});

test("提交后结果不明：只登记一次，上报 unknown，账号暂停，本批次结束、不再领取", async () => {
  const { srv, eng } = setup({ outcome: "unknown_after_submit" });
  const s = await eng.run("scheduled", CID, undefined, "14:00");
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "result_unknown");
  assert.equal(s.counts.unknown, 1);
  assert.equal(paths(srv, /\/submitting$/).length, 1, "整个批次只登记过一次");
  assert.equal(paths(srv, /\/next$/).length, 1, "结果不明之后不再领取");
  const r = resultsOf(srv);
  assert.equal(r.length, 1);
  assert.equal(r[0].outcome, "unknown");
  assert.equal(r[0].amazon_order_no, null);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["result_unknown", "assigned"]);
  assert.equal(srv.state.account.execution_paused, true);
});

test("上游取消：进度拿到 continue=false → 上报 ABORTED_UPSTREAM_CANCELLED，不走护栏与登记；下一笔照常", async () => {
  const { srv, eng } = setup({
    server: { hooks: { afterClaim: (t) => { if (t.upstream_order_no === "PO-DEMO-0001") t.cancelled = true; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.aborted, 1);
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/attempts\/7\/guard$/).length, 0);
  assert.equal(paths(srv, /\/attempts\/7\/submitting$/).length, 0);
  assert.equal(resultsOf(srv)[0].error_code, "ABORTED_UPSTREAM_CANCELLED");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["cancelled", "purchased"]);
});

test("上游在护栏之后、登记之前取消：最后一次进度拦住，不登记", async () => {
  const { srv, eng } = setup({
    server: {
      tasks: [TASKS()[0]],
      hooks: {
        afterClaim: (t, a, state) => {
          const orig = state.attempts.get(a.id);
          Object.defineProperty(orig, "guard", {
            configurable: true,
            get() { return this._g ?? null; },
            set(v) { this._g = v; t.cancelled = true; }, // 护栏一裁决完，上游就取消
          });
        },
      },
    },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.aborted, 1);
  assert.equal(paths(srv, /\/submitting$/).length, 0);
  assert.equal(srv.state.tasks[0].status, "cancelled");
});

test("409 ACCOUNT_BUSY（账号被占用）：开始执行失败：这个买家号正在浏览器「…」里执行（HH:MM 开始）。不领取", async () => {
  const { srv, eng } = setup({
    server: { foreignRun: { device_id: 13, device_name: "浏览器 13", since: "2026-11-05T05:58:00+00:00" } },
  });
  let notice = null;
  const s = await eng.run("manual", CID, (n) => { notice = n; });
  assert.equal(s.status, "busy");
  assert.equal(notice.started, false);
  assert.equal(notice.busy_mine, false);
  assert.equal(s.busy.holder_device_name, "浏览器 13");
  assert.equal(s.message, "开始执行失败：这个买家号正在浏览器「浏览器 13」里执行（13:58 开始）。");
  assert.equal(paths(srv, /\/next$/).length, 0);
});

test("409 DEVICE_BUSY（本浏览器未结束）：开始执行失败：这个浏览器上一个批次还没有结束。带上那个批次的账号与开始时间", async () => {
  const { srv, eng } = setup();
  srv.state.runs.set(1, { id: 1, account_id: 3, device_id: 5, started_at: srv.now() - 120_000, lease_until: srv.now() + 60_000, ended: false, stop: false });
  let notice = null;
  const s = await eng.run("manual", CID, (n) => { notice = n; });
  assert.equal(s.status, "busy");
  assert.equal(s.error.code, "DEVICE_BUSY");
  assert.match(s.message, /^开始执行失败：这个浏览器上一个批次还没有结束。/);
  assert.match(s.message, /批次属于账号「环境-DEMO」（13:58 开始）/);
  assert.equal(notice.busy_mine, true, "占着的是这个浏览器自己：过一会儿自然会好");
  assert.equal(paths(srv, /\/next$/).length, 0);
});

test("403 IDENTITY_MISMATCH（开始批次）：四种情况都用服务端按情况写的原话，不暂停账号", async () => {
  const cases = [
    [{ identity: { signed_in: false } }, "开始执行失败：浏览器没有登录亚马逊", "signed_out"],
    [{ identity: { signed_in: true, customer_id: DEMO_CID, conflict: true } }, "开始执行失败：这个浏览器的亚马逊标签页显示的买家号不一致，请刷新或关闭较早打开的标签页", "conflict"],
    [{ identity: { signed_in: true, customer_id: OTHER_CID } }, "开始执行失败：页面上的买家号与插件最近上报的不一致，请刷新亚马逊页面后重试", "signed_in"],
    [{ identityAt: Date.parse("2026-11-05T05:40:00Z") }, "开始执行失败：插件超过 10 分钟没有上报登录状态，请打开或刷新亚马逊页面后重试", "signed_in"],
  ];
  for (const [server, message, loginState] of cases) {
    const { srv, eng } = setup({ server });
    const s = await eng.run("manual", CID);
    assert.equal(s.status, "rejected", message);
    assert.equal(s.error.code, "IDENTITY_MISMATCH");
    assert.equal(s.error.status, 403);
    assert.equal(s.error.detail.login_state, loginState);
    assert.equal(s.message, message);
    assert.equal(paths(srv, /\/next$/).length, 0);
    assert.equal(srv.state.account.execution_paused, false);
  }
});

test("IDENTITY_MISMATCH 没有服务端原话时按 detail {login_state, stale} 说；其余拒绝码各有说明", () => {
  const e = (code, detail = null, message = undefined, status = 403) => ({ kind: "http", status, code, message: message ?? "", detail });
  const im = (detail) => describeStartError(e("IDENTITY_MISMATCH", detail)).message;
  assert.equal(im({ login_state: "signed_out", stale: false }), "开始执行失败：浏览器没有登录亚马逊");
  assert.match(im({ login_state: "conflict", stale: false }), /标签页显示的买家号不一致/);
  assert.match(im({ login_state: "signed_in", stale: true }), /太久没有上报登录状态/);
  assert.match(im({ login_state: "no_id", stale: false }), /还没有读到这个浏览器登录的买家号/);
  assert.match(im({ login_state: "signed_in", stale: false }), /页面上的买家号与插件最近上报的不一致/);
  assert.match(im(null), /页面上的买家号与插件最近上报的不一致/);
  const nr = describeStartError(e("ACCOUNT_NOT_REGISTERED", null, "这个买家号还没有登记：请在控制台「账号」页登记后再开始"));
  assert.equal(nr.message, "开始执行失败：这个买家号还没有登记：请在控制台「账号」页登记后再开始");
  assert.match(describeStartError(e("UNAUTHORIZED", null, "这个浏览器还没有登记，插件会自动重新登记", 401)).message, /^开始执行失败：服务端不认识这个浏览器，插件正在重新登记，请稍后再点一次$/);
  assert.equal(describeStartError(e("SLOT_DONE", null, "这个时间点今天已经执行过", 409)).message, "开始执行失败：这个时间点今天已经执行过");
  assert.match(describeStartError(e("ACCOUNT_PAUSED", null, "账号已暂停执行：结果待核实")).message, /^开始执行失败：账号已暂停执行，请在控制台恢复后再试（服务端：账号已暂停执行：结果待核实）$/);
  // 持有者就是这个浏览器的 ACCOUNT_BUSY（并发时的另一种问法）按「本浏览器未结束」说
  const mine = describeStartError(e("ACCOUNT_BUSY", { holder_device_id: 5, holder_device_name: "浏览器 5", since: null }, "", 409), 5);
  assert.equal(mine.mine, true);
  assert.match(mine.message, /^开始执行失败：这个浏览器上一个批次还没有结束。/);
  for (const m of [nr.message, mine.message, im(null)]) assert.doesNotMatch(m, /设备|绑定|配对/);
});

test("403 ACCOUNT_NOT_REGISTERED：这个买家号还没有登记（登着一个没登记过的买家号去开始）", async () => {
  const { srv, eng } = setup({ server: { identity: { signed_in: true, customer_id: "A2TEST00000" } } });
  const s = await eng.run("manual", "A2TEST00000");
  assert.equal(s.status, "rejected");
  assert.equal(s.error.code, "ACCOUNT_NOT_REGISTERED");
  assert.equal(s.message, "开始执行失败：这个买家号还没有登记：请在控制台「账号」页登记后再开始");
  assert.equal(srv.loginState(), "unregistered");
  assert.equal(srv.state.runs.size, 0);
});

test("定时批次带时间点：POST /runs {customer_id, trigger: scheduled, slot}；同一个时间点今天执行过 → SLOT_DONE（拒绝，不是占用）", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]] } });
  const s = await eng.run("scheduled", CID, undefined, "14:00");
  assert.equal(s.status, "finished");
  assert.deepEqual(srv.calls[0].body, { customer_id: CID, trigger: "scheduled", slot: "14:00", simulated: true });
  assert.equal(srv.state.runs.get(9).scheduled_slot, "14:00");
  // 另一个浏览器（或这个浏览器晚些时候）再为同一个时间点申请
  const again = new RunEngine({ api: new PluginClient({ baseUrl: "http://127.0.0.1:8790", token: DEMO_TOKEN, extVersion: "0.2.0", fetch: srv.fetch }), outcome: "pass", now: srv.now });
  let notice = null;
  const s2 = await again.run("scheduled", CID, (n) => { notice = n; }, "14:00");
  assert.equal(s2.status, "rejected");
  assert.equal(notice.error.code, "SLOT_DONE");
  assert.equal(notice.busy_mine, false);
  assert.equal(s2.message, "开始执行失败：这个时间点今天已经执行过");
  // 手动批次不受时间点去重的影响
  const manual = new RunEngine({ api: new PluginClient({ baseUrl: "http://127.0.0.1:8790", token: DEMO_TOKEN, extVersion: "0.2.0", fetch: srv.fetch }), outcome: "pass", now: srv.now });
  assert.equal((await manual.run("manual", CID)).status, "finished");
});

test("定时批次的时间点不对（账号改成手动了、时间点不在执行时间里）→ 422，拒绝开始", async () => {
  const { eng } = setup({ server: { account: { purchase_mode: "manual" } } });
  let notice = null;
  const s = await eng.run("scheduled", CID, (n) => { notice = n; }, "14:00");
  assert.equal(s.status, "rejected");
  assert.deepEqual([notice.error.status, notice.error.code], [422, "VALIDATION"]);
  assert.match(s.message, /不是定时执行模式/);
  const { eng: e2 } = setup();
  const s2 = await e2.run("scheduled", CID, undefined, "09:30");
  assert.match(s2.message, /时间点 09:30 不在账号/);
});

test("403 账号已暂停：带上服务端给的原因", async () => {
  const { eng } = setup({ server: { account: { execution_paused: true } } });
  const s = await eng.run("scheduled", CID, undefined, "14:00");
  assert.equal(s.status, "rejected");
  assert.equal(s.error.code, "ACCOUNT_PAUSED");
  assert.match(s.message, /账号已暂停执行/);
});

test("缺货：护栏之前上报 OUT_OF_STOCK，不调护栏，任务转异常", async () => {
  const { srv, eng } = setup({ outcome: "out_of_stock" });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.failed, 2);
  assert.equal(paths(srv, /\/guard$/).length, 0);
  assert.deepEqual(resultsOf(srv).map((r) => r.error_code), ["OUT_OF_STOCK", "OUT_OF_STOCK"]);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "exception"]);
});

test("页面加载失败：服务端首次让任务回到已分配（自动重试一次），第二次转异常", async () => {
  const { srv, eng } = setup({ outcome: "page_load_failed" });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.attempts, 4);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "exception"]);
  assert.equal(paths(srv, /\/guard$/).length, 0);
});

test("验证码（账号级错误）：上报 CAPTCHA_ENCOUNTERED，本批次结束，第二笔不领取", async () => {
  const { srv, eng } = setup({ outcome: "account_captcha" });
  const s = await eng.run("manual", CID);
  assert.equal(s.reason, "account_level_error");
  assert.equal(paths(srv, /\/next$/).length, 1);
  assert.equal(resultsOf(srv)[0].error_code, "CAPTCHA_ENCOUNTERED");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["assigned", "assigned"]);
  assert.equal(srv.state.account.execution_paused, true);
});

test("批次心跳说 stop：当前这一笔结束后停止，并结束批次", async () => {
  const { srv, eng } = setup({
    server: { tickMs: 20_000, hooks: { afterClaim: (t, a, state) => { const r = state.runs.get(a.run_id); r.stop = true; r.stop_reason = "account_paused"; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "stopped");
  assert.equal(s.counts.succeeded, 1);
  assert.ok(paths(srv, /\/runs\/\d+\/heartbeat$/).length >= 1);
  assert.equal(paths(srv, /\/next$/).length, 1);
  assert.equal(paths(srv, /\/finish$/).length, 1);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["purchased", "assigned"]);
});

test("批次心跳到点补发：时钟走过 35 秒后，领取之前先续租", async () => {
  const { srv, eng } = setup({ server: { tickMs: 10_000 } });
  await eng.run("manual", CID);
  assert.ok(paths(srv, /\/runs\/\d+\/heartbeat$/).length >= 1);
});

test("提交登记断网：绝不重试、不上报成功、不再续租，交给服务端清扫（中断）", async () => {
  const { srv, eng } = setup({ server: { faults: [{ match: /\/submitting$/, times: 1 }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "abandoned");
  assert.equal(s.reason, "submitting_unconfirmed");
  assert.equal(paths(srv, /\/submitting$/).length, 1);
  assert.equal(resultsOf(srv).length, 0);
  assert.equal(paths(srv, /\/finish$/).length, 0);
  assert.match(s.message, /租约到期/);
});

test("提交登记的请求到了服务端、响应丢了：仍然只登记一次，不上报成功", async () => {
  const { srv, eng } = setup({ server: { faults: [{ match: /\/submitting$/, times: 1, after: true }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "abandoned");
  const a = srv.state.attempts.get(7);
  assert.equal(a.submits, 1);
  assert.notEqual(a.submitted_at, null, "服务端已记登记时间 —— 清扫时会进结果待核实");
  assert.equal(a.results.length, 0);
});

test("进度上报偶发断网：按重试策略再试一次后继续", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/progress$/, times: 1 }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 1);
});

test("执行权已失效（LEASE_LOST）：立即停止，不结束批次", async () => {
  const { srv, eng } = setup({
    server: { hooks: { afterClaim: (t, a, state) => { state.runs.get(a.run_id).ended = true; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "abandoned");
  assert.equal(paths(srv, /\/finish$/).length, 0);
  assert.equal(srv.state.attempts.get(7).submitted_at, null);
});

test("用户请求停止：当前一笔结束后停，结束批次", async () => {
  let eng;
  const r = setup({
    server: { hooks: { afterClaim: () => eng.requestStop("用户点击停止") } },
  });
  eng = r.eng;
  const s = await eng.run("manual", CID);
  assert.equal(s.reason, "stopped");
  assert.equal(s.counts.attempts, 1);
  assert.equal(paths(r.srv, /\/finish$/).length, 1);
});

test("单批次领取上限：防止服务端异常时无限循环", async () => {
  const { srv, eng } = setup({ outcome: "page_load_failed", engine: { maxAttempts: 3 } });
  const s = await eng.run("manual", CID);
  assert.equal(s.reason, "attempt_cap");
  assert.equal(paths(srv, /\/next$/).length, 3);
});

test("一个引擎实例只跑一个批次", async () => {
  const { eng } = setup();
  await eng.run("manual", CID);
  await assert.rejects(eng.run("manual", CID), /只能跑一个批次/);
});

// ── 结果一定送到（D2-06 / D6-03）────────────────────────────────────────────

test("成功结果遇到一次 5xx（事务已回滚）：客户端原样再发一次，送达，任务已采购，账号不暂停", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/result$/, times: 1, status: 500 }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/result$/).length, 2, "500 之后原样再发一次");
  assert.equal(srv.state.attempts.get(7).results.length, 1, "500 那一次没进服务端，第二次送达");
  assert.equal(srv.state.tasks[0].status, "purchased");
  assert.equal(srv.state.account.execution_paused, false);
});

test("成功结果一直送不到：在租约预算内按退避重发同一份结果、期间续租；送达之后才结束批次", async () => {
  const { srv, eng, logs } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/result$/, times: 15 }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 1);
  const calls = paths(srv, /\/result$/);
  assert.equal(calls.length, 16, "15 次没说上话（客户端每轮自己再试 2 次，引擎退避 5 轮）之后送达");
  for (const c of calls) assert.deepEqual(c.body, calls[0].body, "每次重发都是同一份结果（单号不变）");
  assert.ok(paths(srv, /\/runs\/\d+\/heartbeat$/).length >= 1, "等待期间续租");
  // 结束批次在结果送达之后
  const finishAt = srv.calls.findIndex((c) => /\/finish$/.test(c.path));
  const lastResult = srv.calls.map((c) => c.path).lastIndexOf(calls.at(-1).path);
  assert.ok(finishAt > lastResult);
  assert.equal(srv.state.tasks[0].status, "purchased");
  assert.ok(logs.some((l) => /已补交送达/.test(l.text)));
});

test("成功结果在租约预算内都送不到：中断、不结束批次（结束会把已登记的尝试判成结果待核实）", async () => {
  const { srv, eng, logs } = setup({
    server: { tasks: [TASKS()[0]], leaseTtlS: 60, faults: [{ match: /\/result$|\/heartbeat$/, times: 1000 }] },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "abandoned");
  assert.equal(s.reason, "result_not_delivered");
  assert.equal(paths(srv, /\/finish$/).length, 0);
  assert.ok(paths(srv, /\/result$/).length > 3, "放弃之前重发过几轮");
  assert.ok(srv.now() - Date.parse("2026-11-05T06:00:00Z") < 60_000, "没有超过租约（60 秒）还在等");
  assert.match(s.message, /回执已存在插件里/);
  assert.ok(logs.some((l) => /回执已存在插件里，服务端恢复后自动补交/.test(l.text)));
});

test("服务端已把这一笔判为结果待核实、迟到的成功回执被记下（409 late_receipt_recorded）：不再重发，本批次停", async () => {
  // 登记之后、上报之前，服务端把尝试清扫成了 unknown
  const { srv, eng, logs } = setup({
    server: { tasks: [TASKS()[0]] },
    wrapFetch: (srv) => async (url, init) => {
      const res = await srv.fetch(url, init);
      if (/\/submitting$/.test(new URL(url).pathname) && res.status === 200) {
        for (const a of srv.state.attempts.values()) if (a.status === "submitting") { a.status = "unknown"; a.finished_at = srv.now(); }
      }
      return res;
    },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.reason, "success_rejected");
  assert.equal(paths(srv, /\/result$/).length, 1, "409 是明确的答复，不重发");
  assert.equal(srv.state.lateReceipts.length, 1);
  assert.ok(logs.some((l) => /迟到回执记下/.test(l.text)));
});

// ── 提交登记的响应丢了、浏览器重发（D6-04）──────────────────────────────────

test("登记响应丢了、浏览器把 POST 重发、服务端回「已登记」：上报 unknown（不是 SUBMIT_REJECTED），本批次结束", async () => {
  const { srv, eng, logs } = setup({ server: { faults: [{ match: /\/submitting$/, times: 1, resend: true }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "result_unknown");
  assert.equal(s.counts.unknown, 1);
  const r = resultsOf(srv);
  assert.equal(r.length, 1);
  assert.equal(r[0].outcome, "unknown");
  assert.equal(r[0].error_code, null);
  assert.match(r[0].error_detail, /没有点击下单/);
  assert.equal(paths(srv, /\/progress$/).filter((c) => c.body.detail?.submit_rejected).length, 0, "不当成被拒去问进度");
  assert.equal(paths(srv, /\/next$/).length, 1, "不再领取下一笔");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["result_unknown", "assigned"]);
  assert.equal(srv.state.account.execution_paused, true);
  assert.ok(!logs.some((l) => /SUBMIT_REJECTED/.test(l.text)));
});

// ── 护栏的响应丢了、浏览器重发（D6-07）──────────────────────────────────────

test("护栏拦截的响应丢了、重发得到 409（尝试已失败）：算拦截，继续下一笔", async () => {
  const { srv, eng } = setup({ outcome: "price_over_cap", server: { faults: [{ match: /\/attempts\/7\/guard$/, times: 1, resend: true }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "completed");
  assert.equal(s.counts.blocked, 2, "两笔都按拦截计");
  assert.equal(paths(srv, /\/next$/).length, 3, "第一笔之后照常领取");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "exception"]);
  assert.equal(paths(srv, /\/submitting$/).length, 0);
});

test("护栏拦截后插件照常上报同一份失败：服务端认出重复（200 duplicate），不算冲突", async () => {
  const { srv, eng } = setup({ outcome: "price_over_cap", server: { tasks: [TASKS()[0]] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.blocked, 1);
  assert.equal(srv.state.attempts.get(7).status, "failed", "护栏拦截时服务端已结束这一笔");
  assert.equal(resultsOf(srv)[0].error_code, "PRICE_OVER_CAP");
});

test("提交前复核护栏不过（409 GUARD_BLOCKED）：按拦截码上报，计拦截，其他任务继续", async () => {
  let n = 0;
  const { srv, eng } = setup({ server: { hooks: { recheck: () => (n++ === 0 ? "PRICE_OVER_CAP" : null) } } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.blocked, 1);
  assert.equal(s.counts.succeeded, 1);
  assert.equal(resultsOf(srv)[0].error_code, "PRICE_OVER_CAP");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "purchased"]);
});

// ── 服务端要求中止的原因（D6-05）──────────────────────────────────────────

test("服务端给 cancel_code：账号暂停 → ABORTED_BY_SERVER（任务回已分配），只有上游取消才报 ABORTED_UPSTREAM_CANCELLED", async () => {
  const { srv, eng } = setup({
    server: {
      cancelCodes: true,
      knowsAbortedByServer: true,
      hooks: { afterClaim: (t, a, state) => { if (t.upstream_order_no === "PO-DEMO-0001") { state.account.execution_paused = true; state.account.pause_reason = "人工检查银行卡"; } } },
    },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.aborted, 1);
  const r = resultsOf(srv);
  assert.equal(r[0].error_code, "ABORTED_BY_SERVER");
  assert.match(r[0].error_detail, /账号已暂停执行：人工检查银行卡/);
  assert.equal(srv.state.tasks[0].status, "assigned");
  const { srv: srv2, eng: eng2 } = setup({
    server: { cancelCodes: true, knowsAbortedByServer: true, hooks: { afterClaim: (t) => { if (t.upstream_order_no === "PO-DEMO-0001") t.cancelled = true; } } },
  });
  await eng2.run("manual", CID);
  assert.equal(resultsOf(srv2)[0].error_code, "ABORTED_UPSTREAM_CANCELLED");
  assert.equal(srv2.state.tasks[0].status, "cancelled");
});

test("执行中换了买家号（进度 cancel_code IDENTITY_MISMATCH）：这一笔在提交前中止，上报 ABORTED_BY_SERVER，任务回已分配，**不暂停账号**", async () => {
  const ctx = setup({ server: { hooks: { afterClaim: () => ctx.switchBuyer() } } });
  const { srv, eng, logs, views } = ctx;
  const s = await eng.run("manual", CID);
  assert.equal(resultsOf(srv)[0].error_code, "ABORTED_BY_SERVER");
  assert.equal(s.counts.aborted, 1);
  assert.equal(s.counts.succeeded, 0);
  assert.equal(paths(srv, /\/(guard|submitting)$/).length, 0, "不走护栏与登记");
  assert.equal(srv.state.attempts.get(7).status, "aborted");
  assert.equal(srv.state.tasks[0].status, "assigned", "任务回已分配");
  assert.equal(srv.state.account.execution_paused, false, "换登另一个买家号是正常操作，不暂停账号");
  assert.ok(logs.some((l) => l.text.includes(BUYER_CHANGED_TEXT)), "日志里是规格 §5 的那一句");
  assert.ok(views.some((v) => v.message === `${BUYER_CHANGED_TEXT}。`), "面板上也是");
  assert.ok(logs.some((l) => /没有可执行的任务了：浏览器登录的买家号变了（现在是 A9OTHER00000，这个批次的账号 环境-DEMO 登记的是 A1DEMO00000），停止领取/.test(l.text)), "之后领取不下发（服务端的原话），批次正常结束");
  assert.equal(s.status, "finished");
});

// ── 护栏 buyer 项拦截（契约 §4 / §5）：服务端按中止类落地，并且自己把批次结束 ──────────────────────

/** 页面上读到的已经是另一个买家号，但对账后的上报还没到服务端（服务端的登录状态仍是批次的买家号）：
 *  进度照常放行，护栏的 buyer 项在结算事实上把它拦下。只换一次（第一笔开始之后）。 */
const buyerSwitchedOnPage = (extra = {}) => {
  let switched = false;
  const ctx = setup({
    ...extra,
    server: {
      ...(extra.server ?? {}),
      hooks: { afterProgress: (t, a, state, stage) => { if (stage === "started" && !switched) { switched = true; ctx.page.ident = { ...IDENT, customer_id: OTHER_CID }; } } },
    },
  });
  return ctx;
};
const TALLY_ABORTED_1 = "成功 0，失败 0，拦截 0，待核实 0，中止 1";
const RUN_ENDED_TEXT = "执行批次已结束（结算页登录的买家号不是这个批次的买家号），请重新开始批次";

test("执行中换了买家号、服务端还没收到新的上报：护栏的 buyer 项拦下 —— 服务端中止这一笔（任务回已分配，不进异常、不暂停账号）并结束批次；插件不上报结果、不调用结束批次，以「买家号变了」结束", async () => {
  const { srv, eng, logs, views } = buyerSwitchedOnPage();
  const s = await eng.run("manual", CID);
  const g = paths(srv, /\/guard$/);
  assert.equal(g.length, 1);
  assert.equal(g[0].body.customer_id, OTHER_CID, "事实里带的是取事实那一刻读到的买家号 ID，不是开始批次时的");
  // 服务端的落地：尝试中止（不是失败）、任务回已分配（不是异常）、账号不暂停、批次由服务端结束
  const a = srv.state.attempts.get(7);
  assert.equal(a.guard.code, "IDENTITY_MISMATCH");
  assert.deepEqual(a.guard.checks.buyer, { ok: false, expected: DEMO_CID, actual: OTHER_CID });
  assert.match(a.guard.reasons[0], /^结算页登录的买家号（A9OTHER00000）不是这个批次的买家号（A1DEMO00000）$/);
  assert.deepEqual([a.status, a.error_code, a.submitted_at, a.blocked_at], ["aborted", "IDENTITY_MISMATCH", null, "guard"]);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["assigned", "assigned"], "任务回已分配，不进异常；第二笔没有动");
  assert.match(srv.state.tasks[0].status_reason, /不是这个批次的买家号（A1DEMO00000），未提交前中止，任务保留待重新领取$/);
  assert.equal(srv.state.account.execution_paused, false, "不暂停账号");
  const run = srv.state.runs.get(9);
  assert.deepEqual([run.ended, run.end_reason, run.plugin_reason, run.finish_detail], [true, "identity_mismatch", null, null], "批次是服务端结束的，不是插件");
  assert.deepEqual(run.stats, { claimed: 1, aborted: 1 }, "批次统计记中止，不记拦截");
  // 插件：这一笔不上报结果、不登记、不再领取、不调用结束批次、不再续租 —— 护栏是它对这个批次的最后一个请求
  assert.equal(paths(srv, /\/submitting$/).length, 0, "没有登记提交");
  assert.equal(paths(srv, /\/result$/).length, 0, "服务端已经结束了这一笔，不再上报");
  assert.equal(paths(srv, /\/next$/).length, 1, "不再领下一笔");
  assert.equal(paths(srv, /\/finish$/).length, 0, "服务端已经结束了批次，不再调用结束批次");
  assert.match(srv.calls.at(-1).path, /\/attempts\/7\/guard$/);
  assert.ok(srv.calls.every((c) => c.path !== "/plugin/v1/runs/9/heartbeat"));
  // 结论与文案
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "buyer_changed");
  assert.equal(s.error, null);
  assert.deepEqual(s.counts, { attempts: 1, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 1 });
  assert.equal(s.message, `执行批次 #9 已结束：${BUYER_CHANGED_TEXT}（${TALLY_ABORTED_1}）`, "批次以「买家号变了」那一句结束");
  assert.deepEqual([views.at(-1).phase, views.at(-1).message, views.at(-1).attempt], ["ended", s.message, null], "面板最后显示的就是这一句");
  assert.ok(views.some((v) => v.message === `${BUYER_CHANGED_TEXT}。`));
  assert.ok(logs.some((l) => l.level === "warn" && l.text === `尝试 #7：${BUYER_CHANGED_TEXT}（结算页登录的买家号（A9OTHER00000）不是这个批次的买家号（A1DEMO00000）；服务端已结束本批次）`));
  assert.ok(!logs.some((l) => l.level === "err"), "不是错误");
  assert.ok(!logs.some((l) => /结束执行批次失败|执行中断|执行权已失效|异常/.test(l.text)), "不报错、不说成中断，也不说任务转异常");
});

test("护栏 buyer 项拦下之后：这个批次的每一个接口都回「批次已结束」（409 LEASE_LOST，带结束原因），这一笔再报结果按既有规则，都不会再建尝试；重新开始批次马上可以，再领到同一笔", async () => {
  const ctx = buyerSwitchedOnPage();
  const { srv, eng, api, page } = ctx;
  await eng.run("manual", CID);
  const facts = buildCheckoutFacts({ attempt_id: 7, asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69", max_delivery_days: 10, expected_card_last4: null, raw: { postal_code: "32960" } }, "pass", srv.now(), DEMO_CID);
  const later = [
    ["领取", () => api.next(9)],
    ["批次心跳", () => api.runHeartbeat(9)],
    ["结束批次", () => api.finishRun(9, "buyer_changed")],
    ["进度", () => api.progress(7, "before_submit", {})],
    ["护栏", () => api.guard(7, facts)],
    ["提交登记", () => api.submitting(7)],
    ["等待验证", () => api.verifying(7, "")],
  ];
  for (const [name, call] of later) {
    const r = await call();
    assert.equal(r.ok, false, name);
    assert.deepEqual([r.error.kind, r.error.status, r.error.code, r.error.message], ["http", 409, "LEASE_LOST", RUN_ENDED_TEXT], name);
    assert.deepEqual(r.error.detail, { run_id: 9, end_reason: "identity_mismatch" }, name);
  }
  assert.equal(RUN_ENDED_TEXT.includes(END_REASON_ZH.identity_mismatch), true);
  // 这一笔再报结果：报的正是 IDENTITY_MISMATCH 的失败 → 200 duplicate；别的 → 409 CONFLICT
  const same = await api.result(7, failureResult("failed", "IDENTITY_MISMATCH", "结算页登录的买家号不是这个批次的买家号"));
  assert.deepEqual(same.data, { attempt_status: "aborted", order_status: "assigned", duplicate: true });
  const other = await api.result(7, failureResult("failed", "PRICE_OVER_CAP", "结算总额超过限价"));
  assert.deepEqual([other.error.status, other.error.code, other.error.message], [409, "CONFLICT", "该采购尝试的结果已登记为「已中止」，与本次上报不一致"]);
  assert.deepEqual(other.error.detail, { attempt_status: "aborted", reported: "failed" });
  assert.equal(srv.state.attempts.size, 1, "旧批次里没有再建第二笔尝试");
  assert.deepEqual([srv.state.attempts.get(7).status, srv.state.tasks[0].status, srv.state.account.execution_paused], ["aborted", "assigned", false], "这些请求什么都没改");
  // 这个浏览器的心跳：没有未结束的批次了
  const hb = await api.heartbeat(null);
  assert.deepEqual([hb.data.device_run, hb.data.account.active_run, hb.data.account.execution_paused], [null, null, false]);

  // 登录回批次的买家号后重新开始：不用等租约到期，新批次再领到同一笔（第 2 次尝试），照常买完
  page.ident = IDENT;
  const s2 = await ctx.newEngine().run("manual", CID);
  assert.deepEqual([s2.status, s2.reason, s2.run_id], ["finished", "completed", 10]);
  assert.deepEqual(s2.counts, { attempts: 2, succeeded: 2, failed: 0, blocked: 0, unknown: 0, aborted: 0 });
  const a2 = srv.state.attempts.get(8);
  assert.deepEqual([a2.task_id, a2.attempt_no, a2.run_id, a2.status], [1, 2, 10, "succeeded"]);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["purchased", "purchased"]);
  assert.deepEqual(srv.state.runs.get(10).stats, { claimed: 2, succeeded: 2 });
});

test("定时批次被护栏 buyer 项拦下、由服务端结束：不占这个时间点（只有插件正常结束的才占），同一个时间点可以再开始", async () => {
  const ctx = buyerSwitchedOnPage();
  const { srv, page } = ctx;
  const s = await ctx.eng.run("scheduled", CID, undefined, "14:00");
  assert.deepEqual([s.status, s.reason, s.trigger], ["finished", "buyer_changed", "scheduled"]);
  const run = srv.state.runs.get(9);
  assert.deepEqual([run.trigger, run.scheduled_slot, run.ended, run.end_reason], ["scheduled", "14:00", true, "identity_mismatch"]);
  page.ident = IDENT;
  let notice = null;
  const s2 = await ctx.newEngine().run("scheduled", CID, (n) => { notice = n; }, "14:00");
  assert.equal(notice.started, true, "不是 SLOT_DONE");
  assert.deepEqual([s2.status, s2.reason, s2.counts.succeeded], ["finished", "completed", 2]);
  // 这一次是插件正常结束的：时间点才算执行过
  let n3 = null;
  await ctx.newEngine().run("scheduled", CID, (n) => { n3 = n; }, "14:00");
  assert.deepEqual([n3.started, n3.error.code], [false, "SLOT_DONE"]);
});

test("护栏 buyer 项拦截的答复丢了、请求被重发（客户端重试，或浏览器在复用的连接上重发）：拿到的是 409 LEASE_LOST（批次已因它结束）—— 同样以「买家号变了」结束，不当成中断", async () => {
  for (const fault of [{ after: true }, { resend: true }]) {
    const { srv, eng, logs } = buyerSwitchedOnPage({ server: { faults: [{ match: /\/attempts\/7\/guard$/, times: 1, ...fault }] } });
    const s = await eng.run("manual", CID);
    const why = JSON.stringify(fault);
    assert.equal(paths(srv, /\/attempts\/7\/guard$/).length, 2, why);
    assert.equal(srv.state.attempts.get(7).guards, 1, `${why}：服务端只判了一次，第二次在批次这一关就回了`);
    assert.deepEqual([s.status, s.reason], ["finished", "buyer_changed"], why);
    assert.equal(s.message, `执行批次 #9 已结束：${BUYER_CHANGED_TEXT}（${TALLY_ABORTED_1}）`, why);
    assert.equal(paths(srv, /\/result$|\/finish$|\/submitting$/).length, 0, why);
    assert.equal(paths(srv, /\/next$/).length, 1, why);
    assert.deepEqual([srv.state.attempts.get(7).status, srv.state.tasks[0].status, srv.state.account.execution_paused], ["aborted", "assigned", false], why);
    assert.ok(logs.some((l) => l.text === `尝试 #7：${BUYER_CHANGED_TEXT}（第一次的响应没有收到，服务端：${RUN_ENDED_TEXT}）`), why);
    assert.ok(!logs.some((l) => /执行中断|租约到期/.test(l.text)), `${why}：没有在途尝试留给清扫，不说「租约到期后处理」`);
  }
});

test("护栏 buyer 项拦截与并发的批次心跳：心跳先拿到 409（批次已结束）也不把批次说成中断，仍以「买家号变了」结束", async () => {
  let beat = null;
  const ctx = buyerSwitchedOnPage({
    engine: { timers: { setInterval: (fn) => { beat = fn; return 0; }, clearInterval: () => {} } },
    // 护栏的请求到了服务端、答复还在路上时，定时器上的批次心跳发了出去并先回来
    wrapFetch: (srv) => async (url, init) => {
      const res = await srv.fetch(url, init);
      if (/\/guard$/.test(new URL(url).pathname) && beat) {
        beat();
        for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
      }
      return res;
    },
  });
  const { srv, eng, logs } = ctx;
  const s = await eng.run("manual", CID);
  assert.equal(paths(srv, /\/runs\/9\/heartbeat$/).length, 1, "心跳确实发了，而且在护栏的答复之前回来");
  assert.ok(logs.some((l) => l.text.includes(`执行权已失效：${RUN_ENDED_TEXT}`)), "心跳这一路如实记下服务端的原话");
  assert.deepEqual([s.status, s.reason], ["finished", "buyer_changed"]);
  assert.equal(s.message, `执行批次 #9 已结束：${BUYER_CHANGED_TEXT}（${TALLY_ABORTED_1}）`);
  assert.equal(paths(srv, /\/finish$|\/result$/).length, 0);
  assert.ok(!logs.some((l) => /执行中断/.test(l.text)));
});

/** 护栏放行之后、登记之前，账号登记的买家号 ID 改成了另一个，浏览器也登录（并上报）了那一个：身份检查过得去，
 *  但存下的结算事实里是原来的买家号 —— 提交登记前的复核由 buyer 项拦下。 */
const buyerChangedBeforeSubmit = (extra = {}) => {
  const ctx = setup({
    ...extra,
    server: {
      ...(extra.server ?? {}),
      hooks: { afterProgress: (t, a, state, stage) => { if (stage === "before_submit") { state.account.amazon_customer_id = OTHER_CID; ctx.switchBuyer(); } } },
    },
  });
  return ctx;
};

test("提交前复核时护栏的 buyer 项拦下（409 GUARD_BLOCKED，detail.block_code = IDENTITY_MISMATCH、run_ended）：没有登记提交，服务端已中止这一笔并结束批次；插件不上报结果、不再问进度、不调用结束批次", async () => {
  let reply = null;
  const { srv, eng, logs, views } = buyerChangedBeforeSubmit({
    wrapFetch: (srv) => async (url, init) => {
      const res = await srv.fetch(url, init);
      if (/\/submitting$/.test(new URL(url).pathname)) reply = { status: res.status, body: await res.clone().json() };
      return res;
    },
  });
  const s = await eng.run("manual", CID);
  // 服务端的答复（真实服务端这里不抛异常、事务照常提交）
  const reason = "结算页登录的买家号（A1DEMO00000）不是这个批次的买家号（A9OTHER00000）";
  assert.deepEqual(reply, {
    status: 409,
    body: {
      code: "GUARD_BLOCKED",
      message: `按最新配置复核护栏未通过：${reason}，不能提交`,
      detail: { block_code: "IDENTITY_MISMATCH", reasons: [reason], attempt_status: "aborted", order_status: "assigned", run_ended: true, end_reason: "identity_mismatch" },
    },
  });
  const a = srv.state.attempts.get(7);
  assert.deepEqual([a.status, a.error_code, a.submitted_at, a.submits, a.blocked_at], ["aborted", "IDENTITY_MISMATCH", null, 1, "submitting"], "没有登记提交");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["assigned", "assigned"]);
  assert.equal(srv.state.account.execution_paused, false);
  assert.deepEqual([srv.state.runs.get(9).ended, srv.state.runs.get(9).end_reason], [true, "identity_mismatch"]);
  assert.deepEqual(srv.state.runs.get(9).stats, { claimed: 1, aborted: 1 });
  // 插件
  assert.equal(paths(srv, /\/result$/).length, 0, "不上报结果（不报失败，也不报结果不明）");
  assert.equal(paths(srv, /\/finish$/).length, 0, "不调用结束批次");
  assert.equal(paths(srv, /\/progress$/).filter((c) => c.body.detail?.submit_rejected).length, 0, "不当成一般的被拒去问进度");
  assert.equal(paths(srv, /\/next$/).length, 1);
  assert.match(srv.calls.at(-1).path, /\/attempts\/7\/submitting$/);
  assert.deepEqual([s.status, s.reason], ["finished", "buyer_changed"]);
  assert.deepEqual(s.counts, { attempts: 1, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 1 });
  assert.equal(s.message, `执行批次 #9 已结束：${BUYER_CHANGED_TEXT}（${TALLY_ABORTED_1}）`);
  assert.equal(views.at(-1).message, s.message);
  assert.ok(logs.some((l) => l.text === `尝试 #7：${BUYER_CHANGED_TEXT}（${reason}；服务端已结束本批次）`));
  assert.ok(!logs.some((l) => l.level === "err" || /结束执行批次失败|执行中断|提交登记被拒绝/.test(l.text)));
});

test("提交登记的答复丢了、浏览器把 POST 重发，而第一次复核已由 buyer 项拦下：拿到 409 LEASE_LOST（批次已因它结束）—— 没有登记，不报结果不明，同样以「买家号变了」结束", async () => {
  const { srv, eng, logs } = buyerChangedBeforeSubmit({ server: { faults: [{ match: /\/submitting$/, times: 1, resend: true }] } });
  const s = await eng.run("manual", CID);
  const a = srv.state.attempts.get(7);
  assert.deepEqual([a.status, a.error_code, a.submitted_at, a.submits], ["aborted", "IDENTITY_MISMATCH", null, 2]);
  assert.equal(resultsOf(srv).length, 0, "没有登记提交，就不是结果待核实");
  assert.equal(paths(srv, /\/finish$/).length, 0);
  assert.deepEqual([s.status, s.reason, s.counts.aborted, s.counts.unknown], ["finished", "buyer_changed", 1, 0]);
  assert.deepEqual([srv.state.tasks[0].status, srv.state.account.execution_paused], ["assigned", false]);
  assert.ok(logs.some((l) => l.text === `尝试 #7：${BUYER_CHANGED_TEXT}（第一次的响应没有收到，服务端：${RUN_ENDED_TEXT}）`));
  assert.ok(!logs.some((l) => /执行中断|结果不明/.test(l.text)));
});

test("护栏 buyer 项拦下时上游已取消：任务按取消处理（不是回已分配），插件这边一样", async () => {
  // 「读结算信息」那一步的进度放行之后上游才取消；结算事实里带着另一个买家号
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], hooks: { afterProgress: (t, a, state, stage) => { if (stage === "checkout") t.cancelled = true; } } }, engine: { readCustomerId: () => OTHER_CID } });
  const s = await eng.run("manual", CID);
  assert.deepEqual([srv.state.attempts.get(7).status, srv.state.tasks[0].status], ["aborted", "cancelled"]);
  assert.deepEqual([s.status, s.reason, paths(srv, /\/result$|\/finish$/).length], ["finished", "buyer_changed", 0]);
});

test("批次账号没有登记买家号 ID（登记被清空）时护栏同样由 buyer 项拦下，落地相同", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], hooks: { afterProgress: (t, a, state, stage) => { if (stage === "checkout") state.account.amazon_customer_id = null; } } } });
  const s = await eng.run("manual", CID);
  const a = srv.state.attempts.get(7);
  assert.deepEqual([a.guard.code, a.guard.reasons[0], a.status], ["IDENTITY_MISMATCH", "这个批次的账号没有登记买家号 ID，不能放行", "aborted"]);
  assert.deepEqual(a.guard.checks.buyer, { ok: false, expected: null, actual: DEMO_CID });
  assert.deepEqual([s.reason, srv.state.runs.get(9).end_reason, srv.state.tasks[0].status], ["buyer_changed", "identity_mismatch", "assigned"]);
});

test("一般的护栏拦截不受影响：尝试失败、任务转异常、批次继续，批次统计记拦截；结束批次的答复带 ended_at 与 stats", async () => {
  const { srv, eng, api } = setup({ outcome: "price_over_cap" });
  const s = await eng.run("manual", CID);
  assert.deepEqual([s.status, s.reason, s.counts.blocked, s.counts.aborted], ["finished", "completed", 2, 0]);
  const run = srv.state.runs.get(9);
  assert.deepEqual([run.ended, run.end_reason, run.plugin_reason, run.finish_detail], [true, "finished", "finished", "completed"]);
  assert.deepEqual(run.stats, { claimed: 2, blocked: 2 });
  const a = srv.state.attempts.get(7);
  assert.deepEqual([a.status, a.error_code, a.guard.checks.price_cap.ok, a.guard.checks.buyer.ok], ["failed", "PRICE_OVER_CAP", false, true]);
  // 插件结束的批次再问一次：同样是「批次已结束」，结束原因是 finished
  const again = await api.finishRun(9, "completed");
  assert.deepEqual([again.error.code, again.error.message, again.error.detail], ["LEASE_LOST", "执行批次已结束（插件结束了批次），请重新开始批次", { run_id: 9, end_reason: "finished" }]);
  // 结束批次的答复（真实服务端的形状）：结束原因恒为 finished，插件的细分原因不进 end_reason
  const r2 = await api.startRun("manual", CID);
  const fin = await api.finishRun(r2.data.run_id, "stopped");
  assert.deepEqual([Object.keys(fin.data).sort(), fin.data.end_reason, fin.data.stats], [["end_reason", "ended_at", "run_id", "stats"], "finished", {}]);
  assert.deepEqual([srv.state.runs.get(10).plugin_reason, srv.state.runs.get(10).finish_detail], ["finished", "stopped"]);
});

test("来源指定的买家号已经不是这个账号（领取时服务端解除分配、跳过）：插件不看分配方式，只把服务端给的原因写进日志，其余任务照常", async () => {
  const { srv, eng, logs } = setup({ server: { tasks: [{ ...TASKS()[0], off_target: true }, TASKS()[1]] } });
  const s = await eng.run("manual", CID);
  assert.deepEqual([s.status, s.reason, s.counts.attempts, s.counts.succeeded], ["finished", "completed", 1, 1]);
  assert.deepEqual(srv.state.tasks.map((t) => [t.status, t.account_id]), [["queued", null], ["purchased", 3]]);
  assert.equal(srv.state.tasks[0].status_reason, "来源指定的买家号已不是账号 环境-DEMO，领取时解除分配");
  assert.ok(logs.some((l) => l.text === "没有可执行的任务了：今天没有可执行的任务"), "第二次领取时已经没有要解除的了");
  // 只有这一笔的时候，原因里带着解除分配的说明
  const { eng: eng2, logs: logs2, srv: srv2 } = setup({ server: { tasks: [{ ...TASKS()[0], off_target: true }] } });
  const s2 = await eng2.run("manual", CID);
  assert.deepEqual([s2.status, s2.counts.attempts], ["finished", 0]);
  assert.ok(logs2.some((l) => l.text === "没有可执行的任务了：今天没有可执行的任务（1 笔来源指定的买家号已不是本账号、已解除分配）"));
  assert.equal(srv2.state.attempts.size, 0, "原账号领不到这笔任务");
});

test("取事实时这一页没读到买家号 ID：事实里是 null（不拿开始批次时的顶替），服务端按数据不可靠拦截", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]] }, engine: { readCustomerId: () => null } });
  const s = await eng.run("manual", CID);
  assert.equal(paths(srv, /\/guard$/)[0].body.customer_id, null);
  assert.equal(srv.state.attempts.get(7).guard.code, "DATA_UNRELIABLE");
  assert.equal(s.counts.blocked, 1);
  assert.equal(paths(srv, /\/submitting$/).length, 0);
  // 没有提供读取函数、读取时抛错：同样是 null
  const { srv: srv2, eng: eng2 } = setup({ server: { tasks: [TASKS()[0]] }, engine: { readCustomerId: undefined } });
  await eng2.run("manual", CID);
  assert.equal(paths(srv2, /\/guard$/)[0].body.customer_id, null);
  const { srv: srv3, eng: eng3 } = setup({ server: { tasks: [TASKS()[0]] }, engine: { readCustomerId: () => { throw new Error("dom gone"); } } });
  await eng3.run("manual", CID);
  assert.equal(paths(srv3, /\/guard$/)[0].body.customer_id, null);
});

test("旧服务端（没有 cancel_code）：仍报它唯一认得的中止码，任务回到已分配，不会被当成任务级失败转异常", async () => {
  const { srv, eng } = setup({
    server: { legacy: true, hooks: { afterClaim: (t, a, state) => { if (t.upstream_order_no === "PO-DEMO-0001") { state.account.execution_paused = true; } } } },
  });
  await eng.run("manual", CID);
  const codes = resultsOf(srv).map((r) => r.error_code);
  assert.deepEqual(codes, ["ABORTED_UPSTREAM_CANCELLED"]);
  assert.ok(!codes.includes("ABORTED_BY_SERVER"));
  assert.equal(srv.state.tasks[0].status, "assigned");
});

test("进度说这一笔已结束（ATTEMPT_FINISHED）：不再上报，继续下一笔", async () => {
  const { srv, eng } = setup({
    server: { cancelCodes: true, hooks: { afterClaim: (t, a) => { if (t.upstream_order_no === "PO-DEMO-0001") { a.status = "aborted"; t.status = "assigned"; t.upstream_order_no = "PO-DEMO-0001x"; } } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(paths(srv, /\/attempts\/7\/result$/).length, 0);
  assert.equal(s.counts.aborted, 1);
  assert.ok(s.counts.succeeded >= 1);
});

// ── 租约有效期（D6-10）──────────────────────────────────────────────────

test("续租节奏按服务端给的租约有效期：有效期 30 秒时每 10 秒续一次，慢服务端下也不丢执行权", async () => {
  const { srv, eng } = setup({ server: { leaseTtlS: 30, tickMs: 4_000 } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 2);
  assert.ok(paths(srv, /\/runs\/\d+\/heartbeat$/).length >= 3);
  // 对照：按写死的 35 秒续租，这个服务端会在中途收回执行权
  const { eng: eng2 } = setup({ server: { leaseTtlS: 30, tickMs: 4_000 }, engine: { heartbeatEveryMs: 35_000 } });
  assert.equal((await eng2.run("manual", CID)).status, "finished", "上限 35 秒仍按有效期 / 3 取");
});

test("中断说明里的「约 N 分钟」来自租约有效期", async () => {
  const { eng } = setup({ server: { leaseTtlS: 120, faults: [{ match: /\/next$/, times: 1 }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "abandoned");
  assert.match(s.message, /约 2 分钟/);
});

// ── 其他服务端答复（K21 与领取的稍后再试）────────────────────────────────

test("领到非模拟尝试：上报 PLUGIN_SIMULATION_ONLY（账号级，服务端暂停账号），本批次结束", async () => {
  const { srv, eng } = setup({ server: { tasks: [{ ...TASKS()[0], simulated: false }, TASKS()[1]] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.reason, "non_simulated_attempt");
  const r = resultsOf(srv);
  assert.deepEqual(r.map((x) => [x.outcome, x.error_code]), [["failed", "PLUGIN_SIMULATION_ONLY"]]);
  assert.equal(paths(srv, /\/guard$|\/submitting$/).length, 0);
  assert.equal(srv.state.account.execution_paused, true);
  assert.equal(srv.state.tasks[0].status, "assigned");
});

test("提交登记被拒、再问进度服务端仍说继续（例如任务状态已变）：上报 SUBMIT_REJECTED（任务级），本批次结束", async () => {
  const { srv, eng } = setup({ server: { hooks: { submitRefusal: () => ({ message: "任务状态已变化，不能提交", detail: { order_status: "executing" } }) } } });
  const s = await eng.run("manual", CID);
  assert.equal(s.reason, "submit_rejected");
  assert.equal(s.counts.failed, 1);
  const r = resultsOf(srv);
  assert.deepEqual(r.map((x) => [x.outcome, x.error_code]), [["failed", "SUBMIT_REJECTED"]]);
  assert.equal(r[0].error_detail, "任务状态已变化，不能提交", "error_detail 写服务端的中文原话，不带英文码");
  assert.equal(srv.state.tasks[0].status, "exception", "服务端按任务级处理");
  assert.equal(paths(srv, /\/next$/).length, 1);
  assert.equal(paths(srv, /\/finish$/).length, 1);
});

test("护栏没给拦截码：按 GUARD_BLOCKED 上报", async () => {
  const { srv, eng } = setup({
    outcome: "price_over_cap",
    server: { tasks: [TASKS()[0]] },
    wrapFetch: (srv) => async (url, init) => {
      const res = await srv.fetch(url, init);
      if (!/\/guard$/.test(new URL(url).pathname)) return res;
      const b = await res.json();
      return new Response(JSON.stringify({ ...b, code: null }), { status: res.status });
    },
  });
  await eng.run("manual", CID);
  assert.equal(resultsOf(srv)[0].error_code, "GUARD_BLOCKED");
});

test("领取时服务端说「稍后再领」（retry_after_s）：等一会儿再领，不结束批次", async () => {
  let n = 0;
  const { srv, eng } = setup({
    server: { tasks: [TASKS()[0]], hooks: { beforeClaim: () => (n++ < 2 ? { attempt: null, retry_after_s: 5, reason: "有 1 笔可执行的任务正被其他操作占用" } : null) } },
  });
  const t0 = srv.now();
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/next$/).length, 4);
  assert.ok(srv.now() - t0 >= 10_000, "两次各等了 5 秒");
});

test("占着的是这个浏览器自己（DEVICE_BUSY）：开始那一步的结论标明 busy_mine（定时据此稍后再试，不算处理过）", async () => {
  const { srv, eng } = setup();
  srv.state.runs.set(1, { id: 1, account_id: 3, device_id: 5, started_at: srv.now(), lease_until: srv.now() + 180_000, ended: false, stop: false });
  let notice = null;
  await eng.run("scheduled", CID, (n) => { notice = n; }, "14:00");
  assert.equal(notice.status, "busy");
  assert.equal(notice.busy_mine, true);
  const { eng: e2 } = setup({ server: { foreignRun: { device_id: 13, device_name: "浏览器 13", since: null } } });
  let n2 = null;
  await e2.run("scheduled", CID, (n) => { n2 = n; }, "14:00");
  assert.equal(n2.busy_mine, false);
});

test("批次心跳的 stop_reason 用中文说明（identity_mismatch 等不再原样显示）", async () => {
  const ctx = setup({ server: { tickMs: 20_000, hooks: { afterClaim: () => ctx.switchBuyer() } } });
  const { eng, logs, srv } = ctx;
  await eng.run("manual", CID);
  const line = logs.find((l) => /服务端要求停止/.test(l.text));
  assert.ok(line, "收到了停止要求");
  assert.doesNotMatch(line.text, /identity_mismatch/);
  assert.match(line.text, /浏览器登录的买家号变了/);
  assert.doesNotMatch(line.text, /绑定/);
  assert.equal(srv.state.account.execution_paused, false);
});

// ── 第二轮集成：照真实服务端的答复（cancel_code、身份新鲜度、来源变化、稍后再领、结束说明）──────

/** 某一笔尝试按时间先后调过的接口（只留尝试级与身份上报）。 */
const attemptTrail = (srv, id) =>
  srv.calls
    .filter((c) => new RegExp(`/attempts/${id}/|/identity$`).test(c.path))
    .map((c) => (c.path.endsWith("/identity") ? "identity" : c.path.endsWith("/progress") ? `progress:${c.body.stage}` : c.path.split("/").pop()));

test("提交登记之前先报一次新鲜身份（POST /identity），再做登记前确认与登记", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.succeeded, 1);
  const trail = attemptTrail(srv, 7);
  const i = trail.indexOf("submitting");
  assert.deepEqual(trail.slice(i - 3, i + 1), ["guard", "identity", "progress:before_submit", "submitting"]);
  assert.equal(srv.state.identityReports.at(-1).via, "identity");
});

test("批次跑得久（超过 10 分钟）：每 3 分钟补报一次身份，服务端不会因身份过期而中止；不补报的对照组被中止", async () => {
  const tasks = () => [TASKS()[0], TASKS()[1], { ...TASKS()[0], upstream_order_no: "PO-DEMO-0003", asin: "B0DEMO0003" }];
  const { srv, eng } = setup({ server: { tasks: tasks(), tickMs: 30_000 } });
  const t0 = srv.now();
  const s = await eng.run("manual", CID);
  assert.ok(srv.now() - t0 > 600_000, "批次跨过了 10 分钟");
  assert.equal(s.counts.succeeded, 3);
  assert.equal(s.counts.aborted, 0);
  assert.ok(srv.state.identityReports.filter((r) => r.via === "identity").length >= 3);

  const { srv: srv2, eng: eng2 } = setup({ server: { tasks: tasks(), tickMs: 30_000 }, engine: { refreshIdentity: undefined } });
  const s2 = await eng2.run("manual", CID);
  assert.ok(s2.counts.succeeded < 3, "不补报身份：10 分钟后服务端不再放行");
  const codes = resultsOf(srv2).map((r) => r.error_code).filter(Boolean);
  assert.ok(codes.every((c) => c === "ABORTED_BY_SERVER"), codes.join(","));
  assert.equal(srv2.state.account.execution_paused, false, "身份过期不是账号级错误");
});

test("身份过期（cancel_code IDENTITY_STALE）：上报 ABORTED_BY_SERVER，任务回到已分配，账号不暂停；之后领取不下发，批次正常结束", async () => {
  const { srv, eng, logs } = setup({
    server: { tasks: [TASKS()[0]], hooks: { afterClaim: (t, a, state) => { state.device.identityAt = srv.now() - 601_000; } } },
    engine: { refreshIdentity: undefined },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "completed");
  assert.equal(s.counts.aborted, 1);
  const r = resultsOf(srv);
  assert.deepEqual(r.map((x) => [x.outcome, x.error_code]), [["failed", "ABORTED_BY_SERVER"]]);
  assert.match(r[0].error_detail, /超过 10 分钟没有上报登录状态/);
  assert.equal(srv.state.attempts.get(7).status, "aborted");
  assert.equal(srv.state.tasks[0].status, "assigned");
  assert.equal(srv.state.account.execution_paused, false);
  assert.ok(logs.some((l) => /没有可执行的任务了：.*超过 10 分钟没有上报登录状态/.test(l.text)), "领取说明了原因");
  assert.ok(!logs.some((l) => /IDENTITY_STALE/.test(l.text)));
});

test("领取后来源有变化（cancel_code SOURCE_CHANGED）：ABORTED_BY_SERVER，任务按来源变化重新定状态（不转异常），下一笔照常", async () => {
  const { srv, eng } = setup({
    server: { hooks: { afterClaim: (t) => { if (t.upstream_order_no === "PO-DEMO-0001") t.drift = { text: "领取后来源改了数量（1 → 3）", fields: ["quantity"], to: "pending_review" }; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.aborted, 1);
  assert.equal(s.counts.succeeded, 1);
  const r = resultsOf(srv);
  assert.equal(r[0].error_code, "ABORTED_BY_SERVER");
  assert.match(r[0].error_detail, /领取后来源改了数量/);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["pending_review", "purchased"]);
  assert.equal(paths(srv, /\/attempts\/7\/(guard|submitting)$/).length, 0, "不走护栏与登记");
});

test("来源把任务暂停了（cancel_code SOURCE_HOLD）：ABORTED_BY_SERVER，未提交前中止", async () => {
  const { srv, eng } = setup({
    server: { tasks: [TASKS()[0]], hooks: { afterClaim: (t) => { t.hold = { text: "来源里这笔订单与另一行冲突，已暂停执行", kind: "duplicate_row" }; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.aborted, 1);
  assert.equal(resultsOf(srv)[0].error_code, "ABORTED_BY_SERVER");
  assert.equal(srv.state.attempts.get(7).status, "aborted");
  assert.equal(srv.state.tasks[0].status, "conflict", "来源暂停着：不再领取，也不是异常");
  assert.equal(paths(srv, /\/next$/).length, 2);
});

test("登记前确认之后、登记之前来源才变（/submitting 409 SOURCE_CHANGED）：再问进度 → ABORTED_BY_SERVER，绝不报 SUBMIT_REJECTED", async () => {
  const { srv, eng } = setup({
    server: { tasks: [TASKS()[0]], hooks: { afterProgress: (t, a, state, stage) => { if (stage === "before_submit") t.drift = { text: "领取后来源改了限价", fields: ["price_cap"], to: "pending_review" }; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.aborted, 1);
  const sub = paths(srv, /\/attempts\/7\/submitting$/);
  assert.equal(sub.length, 1, "登记只发了一次");
  assert.equal(srv.state.attempts.get(7).submitted_at, null, "服务端没有登记");
  const r = resultsOf(srv);
  assert.deepEqual(r.map((x) => x.error_code), ["ABORTED_BY_SERVER"]);
  assert.equal(srv.state.tasks[0].status, "pending_review", "中止后按来源变化重新定状态，不是异常");
  assert.equal(srv.state.account.execution_paused, false);
});

test("登记那一刻身份过期（/submitting 409 IDENTITY_MISMATCH，detail.stale）：再问进度 → IDENTITY_STALE → ABORTED_BY_SERVER", async () => {
  const { srv, eng } = setup({
    server: { tasks: [TASKS()[0]], hooks: { afterProgress: (t, a, state, stage) => { if (stage === "before_submit") state.device.identityAt = srv.now() - 601_000; } } },
  });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.aborted, 1);
  assert.deepEqual(resultsOf(srv).map((x) => x.error_code), ["ABORTED_BY_SERVER"]);
  assert.equal(srv.state.attempts.get(7).submitted_at, null);
});

test("提交前复核护栏不过、服务端没给 block_code：按 GUARD_BLOCKED 上报，计拦截，其他任务继续", async () => {
  let n = 0;
  const { srv, eng } = setup({ server: { hooks: { recheck: () => (n++ === 0 ? { code: null } : null) } } });
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.blocked, 1);
  assert.equal(s.counts.succeeded, 1);
  assert.equal(resultsOf(srv)[0].error_code, "GUARD_BLOCKED");
  assert.equal(srv.state.tasks[0].status, "exception");
});

test("领取一直「稍后再领」：每轮最多等 30 秒、最多再领 6 次，等待期间照常续租（不丢执行权），然后以 completed 结束", async () => {
  const { srv, eng, logs } = setup({
    server: { leaseTtlS: 30, hooks: { beforeClaim: () => ({ attempt: null, retry_after_s: 90, reason: "有 2 笔可执行的任务正被其他操作（导入、同步或分配）占用，请 90 秒后再领取" }) } },
  });
  const t0 = srv.now();
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "completed");
  assert.equal(paths(srv, /\/next$/).length, 7, "第一次 + 再领 6 次");
  assert.equal(srv.now() - t0, 6 * 30_000, "每轮封顶 30 秒");
  assert.ok(paths(srv, /\/runs\/9\/heartbeat$/).length >= 18, "租约 30 秒：等待期间每 10 秒续一次");
  assert.equal(srv.state.runs.get(9).ended, true);
  assert.equal(srv.state.runs.get(9).finish_detail, "completed");
  assert.ok(logs.some((l) => /一直被其他操作占用/.test(l.text)));
});

test("稍后再领的 retry_after_s 是小数或 0：照样等（至少 1 秒）再领", async () => {
  let n = 0;
  const { srv, eng } = setup({
    server: { tasks: [TASKS()[0]], hooks: { beforeClaim: () => (n++ === 0 ? { attempt: null, retry_after_s: 0, reason: "占用" } : n === 2 ? { attempt: null, retry_after_s: 2.5, reason: "占用" } : null) } },
  });
  const t0 = srv.now();
  const s = await eng.run("manual", CID);
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/next$/).length, 4);
  assert.ok(srv.now() - t0 >= 3_500);
});

test("结束批次：reason 恒为 finished，插件的细分原因进 detail（服务端记为 plugin_detail）", async () => {
  const { srv, eng } = setup();
  await eng.run("manual", CID);
  const fin = paths(srv, /\/runs\/9\/finish$/);
  assert.deepEqual(fin.map((c) => c.body), [{ reason: "finished", detail: "completed" }]);
  assert.equal(srv.state.runs.get(9).end_reason, "finished");
  assert.equal(srv.state.runs.get(9).finish_detail, "completed");
  const { srv: srv2, eng: eng2 } = setup({ outcome: "unknown_after_submit" });
  await eng2.run("manual", CID);
  assert.equal(srv2.state.runs.get(9).finish_detail, "result_unknown");
});

test("结果的请求到了服务端、响应丢了：客户端原样再发，服务端认出重复（200 duplicate），只算一次成功", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/attempts\/7\/result$/, times: 1, after: true }] } });
  const s = await eng.run("manual", CID);
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 1);
  assert.equal(resultsOf(srv).length, 2, "同一份结果发了两次");
  assert.deepEqual(resultsOf(srv)[0], resultsOf(srv)[1]);
  assert.equal(srv.state.attempts.get(7).status, "succeeded");
  assert.equal(srv.state.account.execution_paused, false);
});

test("日志里不出现原样的英文错误码、中止原因码与阶段名", async () => {
  const { SERVER_CODE_LABEL, GUARD_CODE_LABEL, ATTEMPT_CODE_LABEL, LOCAL_CODE_LABEL, CANCEL_CODE_LABEL, STOP_REASON_LABEL, STAGE_LABEL } = await import("../build/core/codes.js");
  const codes = [...Object.keys(SERVER_CODE_LABEL), ...Object.keys(GUARD_CODE_LABEL), ...Object.keys(ATTEMPT_CODE_LABEL), ...Object.keys(LOCAL_CODE_LABEL), ...Object.keys(CANCEL_CODE_LABEL), ...Object.keys(STOP_REASON_LABEL), ...Object.keys(STAGE_LABEL)];
  const re = new RegExp(`(^|[^A-Za-z_])(${codes.join("|")})($|[^A-Za-z_])`);
  const scenarios = [
    setup({ outcome: "price_over_cap" }),
    setup({ outcome: "out_of_stock" }),
    setup({ outcome: "account_captcha" }),
    setup({ outcome: "unknown_after_submit" }),
    setup({ server: { hooks: { afterClaim: (t) => { t.drift = { text: "领取后来源改了数量", fields: ["quantity"] }; } } } }),
    setup({ server: { hooks: { afterClaim: (t, a, state) => { state.device.identityAt = null; } } }, engine: { refreshIdentity: undefined } }),
    setup({ server: { hooks: { recheck: () => ({ code: null }) } } }),
    setup({ server: { identity: { signed_in: false } } }),
    setup({ server: { identity: { signed_in: true, customer_id: OTHER_CID } } }),
    setup({ engine: { readCustomerId: () => OTHER_CID } }),
    setup({ server: { faults: [{ match: /\/next$/, times: 1 }] } }),
    setup({ server: { tickMs: 20_000, hooks: { afterClaim: (t, a, state) => { state.account.archived = true; } } } }),
  ];
  for (const { eng, logs, views } of scenarios) {
    const s = await eng.run("manual", CID);
    for (const text of [...logs.map((l) => l.text), s.message, ...views.map((v) => v.stage_label ?? ""), ...views.map((v) => v.message ?? "")]) {
      assert.doesNotMatch(text, re, text);
    }
  }
});
