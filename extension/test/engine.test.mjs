/** 模拟执行引擎对着内存假服务端（契约 §5，边角路径照 backend/services/execution.py）整条跑：
 *  正常、护栏拦截、提交后结果不明（只登记一次）、上游取消、账号占用、断网中断、
 *  结果重发与租约预算、登记响应丢失后浏览器重发、护栏响应丢失后重发、服务端要求中止的原因码等路径。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { PluginClient } from "../build/core/client.js";
import { RunEngine } from "../build/core/engine.js";
import { createFakeServer } from "./fake-server.mjs";

const TASKS = () => [
  { upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69" },
  { upstream_order_no: "PO-DEMO-0002", asin: "B0DEMO0002", quantity: 2, unit_price: "12.50", price_cap: "25.00" },
];

function setup({ outcome = "pass", server = {}, engine = {}, wrapFetch = null } = {}) {
  const srv = createFakeServer({ tasks: TASKS(), ...server });
  const api = new PluginClient({
    baseUrl: "http://127.0.0.1:8790",
    token: "tok-demo-1",
    extVersion: "0.1.0",
    fetch: wrapFetch ? wrapFetch(srv) : srv.fetch,
    sleep: async () => {},
    retryDelaysMs: [0, 0],
  });
  const logs = [];
  const views = [];
  const eng = new RunEngine({
    api,
    outcome,
    now: srv.now,
    log: (level, text) => logs.push({ level, text }),
    onView: (v) => views.push(v),
    timers: { setInterval: () => 0, clearInterval: () => {} },
    // 等待（结果重发的退避、领取的稍后再试）推进假服务端的时钟：租约预算按真实时间推演
    sleep: async (ms) => { srv.advance(ms); },
    ownDeviceId: 5,
    // 宿主重读并上报身份：这里直接用客户端上报一份绑定账号的身份（浏览器里是后台让各标签页重读、对账后上报）
    refreshIdentity: () => api.identity(IDENT),
    ...engine,
  });
  return { srv, eng, logs, views, api };
}

const IDENT = { signed_in: true, customer_id: "A1DEMO0000", site: "US", page_url: "https://www.amazon.com/gp/css/order-history" };

const paths = (srv, re) => srv.calls.filter((c) => re.test(c.path));
const resultsOf = (srv) => paths(srv, /\/result$/).map((c) => c.body);

test("正常路径：每笔都护栏放行、登记一次、上报成功，单号 SIM-<北京日期>-<尝试 ID>，最后结束批次", async () => {
  const { srv, eng, views } = setup();
  let notice = null;
  const s = await eng.run("manual", (n) => { notice = n; });
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
  const s = await eng.run("manual");
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
  await eng.run("manual");
  const d = paths(srv, /\/guard$/)[0].body.delivery;
  // 假服务端时钟 2026-11-05T06:00Z = 洛杉矶 11-04 22:00；上限 10 天
  assert.equal(d.date, "2026-11-15");
  assert.equal(resultsOf(srv)[0].error_code, "DELIVERY_TOO_LONG");
  assert.equal(paths(srv, /\/submitting$/).length, 0);
});

test("提交后结果不明：只登记一次，上报 unknown，账号暂停，本批次结束、不再领取", async () => {
  const { srv, eng } = setup({ outcome: "unknown_after_submit" });
  const s = await eng.run("scheduled");
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
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.equal(s.counts.aborted, 1);
  assert.equal(paths(srv, /\/submitting$/).length, 0);
  assert.equal(srv.state.tasks[0].status, "cancelled");
});

test("409 ACCOUNT_BUSY：显示持有者设备与开始时间（北京时间），不领取", async () => {
  const { srv, eng } = setup({
    server: { foreignRun: { device_id: 6, device_name: "PC-DEMO-2", since: "2026-11-05T05:58:00+00:00" } },
  });
  let notice = null;
  const s = await eng.run("manual", (n) => { notice = n; });
  assert.equal(s.status, "busy");
  assert.equal(notice.started, false);
  assert.equal(s.busy.holder_device_name, "PC-DEMO-2");
  assert.match(s.message, /「PC-DEMO-2」/);
  assert.match(s.message, /11-05 13:58/);
  assert.equal(paths(srv, /\/next$/).length, 0);
});

test("409 ACCOUNT_BUSY 且持有者就是本设备：换一句说明（可能是另一个标签页或上次异常退出）", async () => {
  const { srv, eng } = setup();
  srv.state.runs.set(1, { id: 1, started_at: srv.now(), ended: false, stop: false });
  const s = await eng.run("manual");
  assert.equal(s.status, "busy");
  assert.match(s.message, /本设备已有一个未结束的执行批次/);
});

test("403 身份不一致：拒绝开始，中文原因", async () => {
  const { srv, eng } = setup({ server: { identityStatus: "mismatch" } });
  const s = await eng.run("manual");
  assert.equal(s.status, "rejected");
  assert.equal(s.error.code, "IDENTITY_MISMATCH");
  assert.match(s.message, /^开始执行失败：身份未通过/);
  assert.match(s.message, /（服务端：浏览器登录的买家账号（A9OTHER0000）与绑定账号 环境-DEMO（A1DEMO0000）不符，拒绝领取）$/);
  assert.equal(paths(srv, /\/next$/).length, 0);
});

test("403 账号已暂停：带上服务端给的原因", async () => {
  const { eng } = setup({ server: { account: { execution_paused: true } } });
  const s = await eng.run("scheduled");
  assert.equal(s.status, "rejected");
  assert.equal(s.error.code, "ACCOUNT_PAUSED");
  assert.match(s.message, /账号已暂停执行/);
});

test("缺货：护栏之前上报 OUT_OF_STOCK，不调护栏，任务转异常", async () => {
  const { srv, eng } = setup({ outcome: "out_of_stock" });
  const s = await eng.run("manual");
  assert.equal(s.counts.failed, 2);
  assert.equal(paths(srv, /\/guard$/).length, 0);
  assert.deepEqual(resultsOf(srv).map((r) => r.error_code), ["OUT_OF_STOCK", "OUT_OF_STOCK"]);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "exception"]);
});

test("页面加载失败：服务端首次让任务回到已分配（自动重试一次），第二次转异常", async () => {
  const { srv, eng } = setup({ outcome: "page_load_failed" });
  const s = await eng.run("manual");
  assert.equal(s.status, "finished");
  assert.equal(s.counts.attempts, 4);
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "exception"]);
  assert.equal(paths(srv, /\/guard$/).length, 0);
});

test("验证码（账号级错误）：上报 CAPTCHA_ENCOUNTERED，本批次结束，第二笔不领取", async () => {
  const { srv, eng } = setup({ outcome: "account_captcha" });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
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
  await eng.run("manual");
  assert.ok(paths(srv, /\/runs\/\d+\/heartbeat$/).length >= 1);
});

test("提交登记断网：绝不重试、不上报成功、不再续租，交给服务端清扫（中断）", async () => {
  const { srv, eng } = setup({ server: { faults: [{ match: /\/submitting$/, times: 1 }] } });
  const s = await eng.run("manual");
  assert.equal(s.status, "abandoned");
  assert.equal(s.reason, "submitting_unconfirmed");
  assert.equal(paths(srv, /\/submitting$/).length, 1);
  assert.equal(resultsOf(srv).length, 0);
  assert.equal(paths(srv, /\/finish$/).length, 0);
  assert.match(s.message, /租约到期/);
});

test("提交登记的请求到了服务端、响应丢了：仍然只登记一次，不上报成功", async () => {
  const { srv, eng } = setup({ server: { faults: [{ match: /\/submitting$/, times: 1, after: true }] } });
  const s = await eng.run("manual");
  assert.equal(s.status, "abandoned");
  const a = srv.state.attempts.get(7);
  assert.equal(a.submits, 1);
  assert.notEqual(a.submitted_at, null, "服务端已记登记时间 —— 清扫时会进结果待核实");
  assert.equal(a.results.length, 0);
});

test("进度上报偶发断网：按重试策略再试一次后继续", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/progress$/, times: 1 }] } });
  const s = await eng.run("manual");
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 1);
});

test("执行权已失效（LEASE_LOST）：立即停止，不结束批次", async () => {
  const { srv, eng } = setup({
    server: { hooks: { afterClaim: (t, a, state) => { state.runs.get(a.run_id).ended = true; } } },
  });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.equal(s.reason, "stopped");
  assert.equal(s.counts.attempts, 1);
  assert.equal(paths(r.srv, /\/finish$/).length, 1);
});

test("单批次领取上限：防止服务端异常时无限循环", async () => {
  const { srv, eng } = setup({ outcome: "page_load_failed", engine: { maxAttempts: 3 } });
  const s = await eng.run("manual");
  assert.equal(s.reason, "attempt_cap");
  assert.equal(paths(srv, /\/next$/).length, 3);
});

test("一个引擎实例只跑一个批次", async () => {
  const { eng } = setup();
  await eng.run("manual");
  await assert.rejects(eng.run("manual"), /只能跑一个批次/);
});

// ── 结果一定送到（D2-06 / D6-03）────────────────────────────────────────────

test("成功结果遇到一次 5xx（事务已回滚）：客户端原样再发一次，送达，任务已采购，账号不暂停", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/result$/, times: 1, status: 500 }] } });
  const s = await eng.run("manual");
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/result$/).length, 2, "500 之后原样再发一次");
  assert.equal(srv.state.attempts.get(7).results.length, 1, "500 那一次没进服务端，第二次送达");
  assert.equal(srv.state.tasks[0].status, "purchased");
  assert.equal(srv.state.account.execution_paused, false);
});

test("成功结果一直送不到：在租约预算内按退避重发同一份结果、期间续租；送达之后才结束批次", async () => {
  const { srv, eng, logs } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/result$/, times: 15 }] } });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.equal(s.reason, "success_rejected");
  assert.equal(paths(srv, /\/result$/).length, 1, "409 是明确的答复，不重发");
  assert.equal(srv.state.lateReceipts.length, 1);
  assert.ok(logs.some((l) => /迟到回执记下/.test(l.text)));
});

// ── 提交登记的响应丢了、浏览器重发（D6-04）──────────────────────────────────

test("登记响应丢了、浏览器把 POST 重发、服务端回「已登记」：上报 unknown（不是 SUBMIT_REJECTED），本批次结束", async () => {
  const { srv, eng, logs } = setup({ server: { faults: [{ match: /\/submitting$/, times: 1, resend: true }] } });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "completed");
  assert.equal(s.counts.blocked, 2, "两笔都按拦截计");
  assert.equal(paths(srv, /\/next$/).length, 3, "第一笔之后照常领取");
  assert.deepEqual(srv.state.tasks.map((t) => t.status), ["exception", "exception"]);
  assert.equal(paths(srv, /\/submitting$/).length, 0);
});

test("护栏拦截后插件照常上报同一份失败：服务端认出重复（200 duplicate），不算冲突", async () => {
  const { srv, eng } = setup({ outcome: "price_over_cap", server: { tasks: [TASKS()[0]] } });
  const s = await eng.run("manual");
  assert.equal(s.counts.blocked, 1);
  assert.equal(srv.state.attempts.get(7).status, "failed", "护栏拦截时服务端已结束这一笔");
  assert.equal(resultsOf(srv)[0].error_code, "PRICE_OVER_CAP");
});

test("提交前复核护栏不过（409 GUARD_BLOCKED）：按拦截码上报，计拦截，其他任务继续", async () => {
  let n = 0;
  const { srv, eng } = setup({ server: { hooks: { recheck: () => (n++ === 0 ? "PRICE_OVER_CAP" : null) } } });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.equal(s.counts.aborted, 1);
  const r = resultsOf(srv);
  assert.equal(r[0].error_code, "ABORTED_BY_SERVER");
  assert.match(r[0].error_detail, /账号已暂停执行：人工检查银行卡/);
  assert.equal(srv.state.tasks[0].status, "assigned");
  const { srv: srv2, eng: eng2 } = setup({
    server: { cancelCodes: true, knowsAbortedByServer: true, hooks: { afterClaim: (t) => { if (t.upstream_order_no === "PO-DEMO-0001") t.cancelled = true; } } },
  });
  await eng2.run("manual");
  assert.equal(resultsOf(srv2)[0].error_code, "ABORTED_UPSTREAM_CANCELLED");
  assert.equal(srv2.state.tasks[0].status, "cancelled");
});

test("服务端给 cancel_code：身份不符也是 ABORTED_BY_SERVER（中止类，不走账号级暂停）", async () => {
  const { srv, eng } = setup({
    server: { cancelCodes: true, knowsAbortedByServer: true, hooks: { afterClaim: (t, a, state) => { state.identityStatus = "mismatch"; } } },
  });
  const s = await eng.run("manual");
  assert.equal(resultsOf(srv)[0].error_code, "ABORTED_BY_SERVER");
  assert.equal(s.counts.aborted, 1);
  assert.equal(srv.state.account.execution_paused, false);
});

test("旧服务端（没有 cancel_code）：仍报它唯一认得的中止码，任务回到已分配，不会被当成任务级失败转异常", async () => {
  const { srv, eng } = setup({
    server: { legacy: true, hooks: { afterClaim: (t, a, state) => { if (t.upstream_order_no === "PO-DEMO-0001") { state.account.execution_paused = true; } } } },
  });
  await eng.run("manual");
  const codes = resultsOf(srv).map((r) => r.error_code);
  assert.deepEqual(codes, ["ABORTED_UPSTREAM_CANCELLED"]);
  assert.ok(!codes.includes("ABORTED_BY_SERVER"));
  assert.equal(srv.state.tasks[0].status, "assigned");
});

test("进度说这一笔已结束（ATTEMPT_FINISHED）：不再上报，继续下一笔", async () => {
  const { srv, eng } = setup({
    server: { cancelCodes: true, hooks: { afterClaim: (t, a) => { if (t.upstream_order_no === "PO-DEMO-0001") { a.status = "aborted"; t.status = "assigned"; t.upstream_order_no = "PO-DEMO-0001x"; } } } },
  });
  const s = await eng.run("manual");
  assert.equal(paths(srv, /\/attempts\/7\/result$/).length, 0);
  assert.equal(s.counts.aborted, 1);
  assert.ok(s.counts.succeeded >= 1);
});

// ── 租约有效期（D6-10）──────────────────────────────────────────────────

test("续租节奏按服务端给的租约有效期：有效期 30 秒时每 10 秒续一次，慢服务端下也不丢执行权", async () => {
  const { srv, eng } = setup({ server: { leaseTtlS: 30, tickMs: 4_000 } });
  const s = await eng.run("manual");
  assert.equal(s.status, "finished");
  assert.equal(s.counts.succeeded, 2);
  assert.ok(paths(srv, /\/runs\/\d+\/heartbeat$/).length >= 3);
  // 对照：按写死的 35 秒续租，这个服务端会在中途收回执行权
  const { eng: eng2 } = setup({ server: { leaseTtlS: 30, tickMs: 4_000 }, engine: { heartbeatEveryMs: 35_000 } });
  assert.equal((await eng2.run("manual")).status, "finished", "上限 35 秒仍按有效期 / 3 取");
});

test("中断说明里的「约 N 分钟」来自租约有效期", async () => {
  const { eng } = setup({ server: { leaseTtlS: 120, faults: [{ match: /\/next$/, times: 1 }] } });
  const s = await eng.run("manual");
  assert.equal(s.status, "abandoned");
  assert.match(s.message, /约 2 分钟/);
});

// ── 其他服务端答复（K21 与领取的稍后再试）────────────────────────────────

test("领到非模拟尝试：上报 PLUGIN_SIMULATION_ONLY（账号级，服务端暂停账号），本批次结束", async () => {
  const { srv, eng } = setup({ server: { tasks: [{ ...TASKS()[0], simulated: false }, TASKS()[1]] } });
  const s = await eng.run("manual");
  assert.equal(s.reason, "non_simulated_attempt");
  const r = resultsOf(srv);
  assert.deepEqual(r.map((x) => [x.outcome, x.error_code]), [["failed", "PLUGIN_SIMULATION_ONLY"]]);
  assert.equal(paths(srv, /\/guard$|\/submitting$/).length, 0);
  assert.equal(srv.state.account.execution_paused, true);
  assert.equal(srv.state.tasks[0].status, "assigned");
});

test("提交登记被拒、再问进度服务端仍说继续（例如任务状态已变）：上报 SUBMIT_REJECTED（任务级），本批次结束", async () => {
  const { srv, eng } = setup({ server: { hooks: { submitRefusal: () => ({ message: "任务状态已变化，不能提交", detail: { order_status: "executing" } }) } } });
  const s = await eng.run("manual");
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
  await eng.run("manual");
  assert.equal(resultsOf(srv)[0].error_code, "GUARD_BLOCKED");
});

test("领取时服务端说「稍后再领」（retry_after_s）：等一会儿再领，不结束批次", async () => {
  let n = 0;
  const { srv, eng } = setup({
    server: { tasks: [TASKS()[0]], hooks: { beforeClaim: () => (n++ < 2 ? { attempt: null, retry_after_s: 5, reason: "有 1 笔可执行的任务正被其他操作占用" } : null) } },
  });
  const t0 = srv.now();
  const s = await eng.run("manual");
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/next$/).length, 4);
  assert.ok(srv.now() - t0 >= 10_000, "两次各等了 5 秒");
});

test("ACCOUNT_BUSY 的持有者就是本设备：开始那一步的结论标明 busy_mine（定时据此稍后再试，不算处理过）", async () => {
  const { srv, eng } = setup();
  srv.state.runs.set(1, { id: 1, started_at: srv.now(), lease_until: srv.now() + 180_000, ended: false, stop: false });
  let notice = null;
  await eng.run("scheduled", (n) => { notice = n; });
  assert.equal(notice.status, "busy");
  assert.equal(notice.busy_mine, true);
  const { eng: e2 } = setup({ server: { foreignRun: { device_id: 6, device_name: "PC-DEMO-2", since: null } } });
  let n2 = null;
  await e2.run("scheduled", (n) => { n2 = n; });
  assert.equal(n2.busy_mine, false);
});

test("批次心跳的 stop_reason 用中文说明（identity_mismatch 等不再原样显示）", async () => {
  const { eng, logs } = setup({
    server: { tickMs: 20_000, hooks: { afterClaim: (t, a, state) => { state.identityStatus = "mismatch"; } } },
  });
  await eng.run("manual");
  const line = logs.find((l) => /服务端要求停止/.test(l.text));
  assert.ok(line, "收到了停止要求");
  assert.doesNotMatch(line.text, /identity_mismatch/);
  assert.match(line.text, /买家账号与绑定账号不一致/);
});

// ── 第二轮集成：照真实服务端的答复（cancel_code、身份新鲜度、来源变化、稍后再领、结束说明）──────

/** 某一笔尝试按时间先后调过的接口（只留尝试级与身份上报）。 */
const attemptTrail = (srv, id) =>
  srv.calls
    .filter((c) => new RegExp(`/attempts/${id}/|/identity$`).test(c.path))
    .map((c) => (c.path.endsWith("/identity") ? "identity" : c.path.endsWith("/progress") ? `progress:${c.body.stage}` : c.path.split("/").pop()));

test("提交登记之前先报一次新鲜身份（POST /identity），再做登记前确认与登记", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]] } });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.ok(srv.now() - t0 > 600_000, "批次跨过了 10 分钟");
  assert.equal(s.counts.succeeded, 3);
  assert.equal(s.counts.aborted, 0);
  assert.ok(srv.state.identityReports.filter((r) => r.via === "identity").length >= 3);

  const { srv: srv2, eng: eng2 } = setup({ server: { tasks: tasks(), tickMs: 30_000 }, engine: { refreshIdentity: undefined } });
  const s2 = await eng2.run("manual");
  assert.ok(s2.counts.succeeded < 3, "不补报身份：10 分钟后服务端不再放行");
  const codes = resultsOf(srv2).map((r) => r.error_code).filter(Boolean);
  assert.ok(codes.every((c) => c === "ABORTED_BY_SERVER"), codes.join(","));
  assert.equal(srv2.state.account.execution_paused, false, "身份过期不是账号级错误");
});

test("身份过期（cancel_code IDENTITY_STALE）：上报 ABORTED_BY_SERVER，任务回到已分配，账号不暂停；之后领取不下发，批次正常结束", async () => {
  const { srv, eng, logs } = setup({
    server: { tasks: [TASKS()[0]], hooks: { afterClaim: (t, a, state) => { state.identityAt = srv.now() - 601_000; } } },
    engine: { refreshIdentity: undefined },
  });
  const s = await eng.run("manual");
  assert.equal(s.status, "finished");
  assert.equal(s.reason, "completed");
  assert.equal(s.counts.aborted, 1);
  const r = resultsOf(srv);
  assert.deepEqual(r.map((x) => [x.outcome, x.error_code]), [["failed", "ABORTED_BY_SERVER"]]);
  assert.match(r[0].error_detail, /超过 10 分钟没有上报登录身份/);
  assert.equal(srv.state.attempts.get(7).status, "aborted");
  assert.equal(srv.state.tasks[0].status, "assigned");
  assert.equal(srv.state.account.execution_paused, false);
  assert.ok(logs.some((l) => /没有可执行的任务了：.*超过 10 分钟没有上报登录身份/.test(l.text)), "领取说明了原因");
  assert.ok(!logs.some((l) => /IDENTITY_STALE/.test(l.text)));
});

test("领取后来源有变化（cancel_code SOURCE_CHANGED）：ABORTED_BY_SERVER，任务按来源变化重新定状态（不转异常），下一笔照常", async () => {
  const { srv, eng } = setup({
    server: { hooks: { afterClaim: (t) => { if (t.upstream_order_no === "PO-DEMO-0001") t.drift = { text: "领取后来源改了数量（1 → 3）", fields: ["quantity"], to: "pending_review" }; } } },
  });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
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
    server: { tasks: [TASKS()[0]], hooks: { afterProgress: (t, a, state, stage) => { if (stage === "before_submit") state.identityAt = srv.now() - 601_000; } } },
  });
  const s = await eng.run("manual");
  assert.equal(s.counts.aborted, 1);
  assert.deepEqual(resultsOf(srv).map((x) => x.error_code), ["ABORTED_BY_SERVER"]);
  assert.equal(srv.state.attempts.get(7).submitted_at, null);
});

test("提交前复核护栏不过、服务端没给 block_code：按 GUARD_BLOCKED 上报，计拦截，其他任务继续", async () => {
  let n = 0;
  const { srv, eng } = setup({ server: { hooks: { recheck: () => (n++ === 0 ? { code: null } : null) } } });
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
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
  const s = await eng.run("manual");
  assert.equal(s.counts.succeeded, 1);
  assert.equal(paths(srv, /\/next$/).length, 4);
  assert.ok(srv.now() - t0 >= 3_500);
});

test("结束批次：reason 恒为 finished，插件的细分原因进 detail（服务端记为 plugin_detail）", async () => {
  const { srv, eng } = setup();
  await eng.run("manual");
  const fin = paths(srv, /\/runs\/9\/finish$/);
  assert.deepEqual(fin.map((c) => c.body), [{ reason: "finished", detail: "completed" }]);
  assert.equal(srv.state.runs.get(9).end_reason, "finished");
  assert.equal(srv.state.runs.get(9).finish_detail, "completed");
  const { srv: srv2, eng: eng2 } = setup({ outcome: "unknown_after_submit" });
  await eng2.run("manual");
  assert.equal(srv2.state.runs.get(9).finish_detail, "result_unknown");
});

test("结果的请求到了服务端、响应丢了：客户端原样再发，服务端认出重复（200 duplicate），只算一次成功", async () => {
  const { srv, eng } = setup({ server: { tasks: [TASKS()[0]], faults: [{ match: /\/attempts\/7\/result$/, times: 1, after: true }] } });
  const s = await eng.run("manual");
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
    setup({ server: { hooks: { afterClaim: (t, a, state) => { state.identityAt = null; } } }, engine: { refreshIdentity: undefined } }),
    setup({ server: { hooks: { recheck: () => ({ code: null }) } } }),
    setup({ server: { identityStatus: "signed_out" } }),
    setup({ server: { faults: [{ match: /\/next$/, times: 1 }] } }),
    setup({ server: { tickMs: 20_000, hooks: { afterClaim: (t, a, state) => { state.account.archived = true; } } } }),
  ];
  for (const { eng, logs, views } of scenarios) {
    const s = await eng.run("manual");
    for (const text of [...logs.map((l) => l.text), s.message, ...views.map((v) => v.stage_label ?? ""), ...views.map((v) => v.message ?? "")]) {
      assert.doesNotMatch(text, re, text);
    }
  }
});
