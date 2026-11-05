/** 模拟执行引擎对着内存假服务端（契约 §5）整条跑：
 *  正常、护栏拦截、提交后结果不明（只登记一次）、上游取消、账号占用、断网中断等路径。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { PluginClient } from "../build/core/client.js";
import { RunEngine } from "../build/core/engine.js";
import { createFakeServer } from "./fake-server.mjs";

const TASKS = () => [
  { upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69" },
  { upstream_order_no: "PO-DEMO-0002", asin: "B0DEMO0002", quantity: 2, unit_price: "12.50", price_cap: "25.00" },
];

function setup({ outcome = "pass", server = {}, engine = {} } = {}) {
  const srv = createFakeServer({ tasks: TASKS(), ...server });
  const api = new PluginClient({
    baseUrl: "http://127.0.0.1:8790",
    token: "tok-demo-1",
    extVersion: "0.1.0",
    fetch: srv.fetch,
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
    ownDeviceId: 5,
    ...engine,
  });
  return { srv, eng, logs, views };
}

const paths = (srv, re) => srv.calls.filter((c) => re.test(c.path));
const resultsOf = (srv) => paths(srv, /\/result$/).map((c) => c.body);

test("正常路径：每笔都护栏放行、登记一次、上报成功，单号 SIM-<北京日期>-<尝试号>，最后结束批次", async () => {
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
  assert.equal(paths(srv, /\/finish$/)[0].body.reason, "completed");
  assert.equal([...srv.state.runs.values()][0].ended, true);
  assert.equal(views.at(-1).phase, "ended");
  // 请求顺序：开始批次 → 领取 → 进度 → 护栏 → 登记 → 结果
  const seq = srv.calls.slice(0, 8).map((c) => c.path.replace(/\/\d+(?=\/|$)/g, "/:id"));
  assert.deepEqual(seq, [
    "/plugin/v1/runs",
    "/plugin/v1/runs/:id/next",
    "/plugin/v1/attempts/:id/progress",
    "/plugin/v1/attempts/:id/progress",
    "/plugin/v1/attempts/:id/guard",
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
  assert.match(s.message, /（服务端：身份不一致）$/);
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
