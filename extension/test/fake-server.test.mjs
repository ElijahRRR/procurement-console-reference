/** 假服务端（test/fake-server.mjs）的边角答复与真实服务端一致：引擎、客户端、后台测试都对着它跑，
 *  它一偏，那些测试就会「过了，真跑不对」。这里逐条钉住真实服务端（backend/services/devices.py、execution.py）
 *  的原话与形状：版本闸、登录状态的说法、开始批次的参数检查次序、租约到期的边界、领取不到的原因、提交登记与
 *  迟到回执，以及同一个定时时间点的两种答复（还在执行 → ACCOUNT_BUSY；以 finished 结束 → SLOT_DONE）。
 *  所有数据都是虚构的。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { DEMO_CID, DEMO_TOKEN, MAX_DEVICES_MESSAGE, OTHER_CID, createFakeServer } from "./fake-server.mjs";
import { PluginClient, parseAttempt } from "../build/core/client.js";
import { classifyResultReply } from "../build/core/receipts.js";
import { buildCheckoutFacts, simOrderNo, successResult } from "../build/core/simulate.js";

const URL_A = "http://127.0.0.1:8790";
const OTHER_TOKEN = "other-browser-token-demo-000000000000000001";
const TASK = { upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69" };
const AT_1400 = Date.parse("2026-11-05T06:00:05Z"); // 北京 14:00:05

/** 输入：假服务端、令牌（null = 不带令牌头）→ 输出：call(path, body) → {status, body, headers}。 */
function caller(srv, token = DEMO_TOKEN) {
  return async (path, body = {}) => {
    const res = await srv.fetch(`${URL_A}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body),
    });
    return { status: res.status, body: await res.json(), headers: res.headers };
  };
}

const startManual = (call, extra = {}) => call("/plugin/v1/runs", { customer_id: DEMO_CID, trigger: "manual", simulated: true, ...extra });

test("身份上报也过版本闸：0.1.0 → 409 PLUGIN_OUTDATED，什么也不记", async () => {
  const srv = createFakeServer({ identity: null });
  const r = await caller(srv)("/plugin/v1/identity", { signed_in: true, customer_id: DEMO_CID, site: "US", page_url: null, conflict: false, ext_version: "0.1.0" });
  assert.deepEqual([r.status, r.body.code, r.body.message], [409, "PLUGIN_OUTDATED", "插件版本过旧：请重新构建并加载新版插件（需要 0.2.0 及以上）"]);
  assert.deepEqual(r.body.detail, { ext_version: "0.1.0", min_ext_version: "0.2.0" });
  assert.equal(srv.state.device.identity, null);
  assert.equal(srv.state.identityReports.length, 0);
});

test("login.customer_id 只在登录状态是 signed_in 时有：各标签页有分歧、没登录、没读到 ID 时为 null", async () => {
  const srv = createFakeServer({ identity: null });
  const call = caller(srv);
  const conflict = await call("/plugin/v1/identity", { signed_in: true, customer_id: DEMO_CID, conflict: true, ext_version: "0.2.0" });
  assert.deepEqual([conflict.body.login.state, conflict.body.login.customer_id], ["conflict", null]);
  const out = await call("/plugin/v1/identity", { signed_in: false, customer_id: DEMO_CID, ext_version: "0.2.0" });
  assert.deepEqual([out.body.login.state, out.body.login.customer_id], ["signed_out", null]);
  const ready = await call("/plugin/v1/identity", { signed_in: true, customer_id: DEMO_CID, ext_version: "0.2.0" });
  assert.deepEqual([ready.body.login.state, ready.body.login.customer_id, ready.body.login.stale], ["ready", DEMO_CID, false]);
});

test("从没上报过登录状态的浏览器：心跳 stale = false；开始批次 403 IDENTITY_MISMATCH，服务端原话，detail.stale = false", async () => {
  const srv = createFakeServer({ identity: null, account: { purchase_mode: "manual" } });
  const call = caller(srv);
  const hb = await call("/plugin/v1/heartbeat", { ext_version: "0.2.0" });
  assert.deepEqual(hb.body.login, { state: "unknown", customer_id: null, at: null, stale: false });
  const r = await startManual(call);
  assert.deepEqual([r.status, r.body.code, r.body.message], [403, "IDENTITY_MISMATCH", "插件还没有上报这个浏览器的登录状态，请打开或刷新亚马逊页面后重试"]);
  assert.deepEqual(r.body.detail, { login_state: "unknown", identity_at: null, stale: false });
});

test("开始批次的参数检查在占用检查之前：simulated 不是布尔 → 422（浏览器忙着也一样）；trigger 缺省 manual；买家号 ID 的原话", async () => {
  const srv = createFakeServer({ account: { purchase_mode: "manual" } });
  const call = caller(srv);
  let r = await startManual(call, { simulated: "yes" });
  assert.deepEqual([r.status, r.body.code, r.body.message, r.body.detail], [422, "VALIDATION", "simulated 必须是 true 或 false", { field: "simulated" }]);
  r = await call("/plugin/v1/runs", { customer_id: DEMO_CID, simulated: true }); // 不带 trigger
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(srv.state.runs.get(r.body.run_id).trigger, "manual");
  r = await startManual(call, { simulated: "yes" });
  assert.deepEqual([r.status, r.body.code], [422, "VALIDATION"], "浏览器忙着也先回参数不对，不是 DEVICE_BUSY");
  r = await startManual(call, { simulated: false });
  assert.equal(r.body.code, "DEVICE_BUSY", "simulated 是布尔：先看占用，再看是不是模拟");
  r = await startManual(call, { trigger: "auto" });
  assert.deepEqual([r.status, r.body.message, r.body.detail], [422, "触发方式只能是 manual 或 scheduled", { field: "trigger" }]);
  r = await startManual(call, { customer_id: "  " });
  assert.deepEqual([r.status, r.body.message], [422, "插件版本过旧，请更新插件"]);
  r = await startManual(call, { customer_id: "A12" });
  assert.deepEqual([r.status, r.body.message], [422, "买家号 ID 格式不对（应以 A 开头，至少 9 位字母或数字）"]);
});

test("非模拟、暂停、没登记：原话与 detail 照真实服务端", async () => {
  const srv = createFakeServer({ account: { purchase_mode: "manual" } });
  const call = caller(srv);
  let r = await startManual(call, { simulated: false });
  assert.deepEqual([r.status, r.body.code, r.body.message], [403, "REAL_PURCHASE_DISABLED", "本阶段只允许模拟执行，服务端拒绝真实采购批次"]);
  srv.state.account.execution_paused = true;
  srv.state.account.pause_reason = "人工检查";
  r = await startManual(call);
  assert.deepEqual([r.status, r.body.code, r.body.message, r.body.detail], [403, "ACCOUNT_PAUSED", "账号 环境-DEMO 已暂停执行：人工检查", { pause_reason: "人工检查", paused_at: new Date(srv.now()).toISOString() }], "暂停总带着暂停时刻");
  srv.setLogin({ signed_in: true, customer_id: OTHER_CID });
  r = await call("/plugin/v1/runs", { customer_id: OTHER_CID, trigger: "manual", simulated: true });
  assert.deepEqual([r.status, r.body.code, r.body.detail], [403, "ACCOUNT_NOT_REGISTERED", { customer_id: OTHER_CID }]);
});

test("账号被暂停时记下最初的暂停时刻（真实服务端 accounts.pause：已暂停时只更新原因）", async () => {
  const srv = createFakeServer({ tasks: [TASK, { ...TASK, upstream_order_no: "PO-DEMO-0002", asin: "B0DEMO0002" }], account: { purchase_mode: "manual" } });
  const call = caller(srv);
  const { runId, a } = await toGuardPassed(srv, call);
  assert.equal((await call(`/plugin/v1/attempts/${a.attempt_id}/submitting`)).status, 200);
  const t0 = srv.now();
  await call(`/plugin/v1/attempts/${a.attempt_id}/result`, { outcome: "unknown", error_code: null });
  assert.deepEqual([srv.state.account.execution_paused, srv.state.account.pause_reason, srv.state.account.paused_at], [true, "结果待核实", t0]);
  assert.equal((await call(`/plugin/v1/runs/${runId}/finish`, { reason: "finished" })).status, 200);
  srv.advance(60_000);
  const r = await startManual(call);
  assert.deepEqual([r.status, r.body.code, r.body.detail], [403, "ACCOUNT_PAUSED", { pause_reason: "结果待核实", paused_at: new Date(t0).toISOString() }]);
});

test("定时参数的原话照真实服务端（_norm_slot / _check_slot）：没带时间点与写法不对是两句话；不是定时模式带 detail.purchase_mode；不在执行时间里的原话带上执行时间、detail.schedule_times；还没到点", async () => {
  const srv = createFakeServer({ now: AT_1400, account: { schedule_times: ["09:30", "14:00", "20:00"] } });
  const call = caller(srv);
  const sched = (slot) => call("/plugin/v1/runs", { customer_id: DEMO_CID, trigger: "scheduled", simulated: true, ...(slot === undefined ? {} : { slot }) });
  for (const slot of [undefined, null, "", "  "]) {
    const r = await sched(slot);
    assert.deepEqual([r.status, r.body.code, r.body.message, r.body.detail], [422, "VALIDATION", "定时执行需要带上时间点（slot，北京时间 HH:MM）", { field: "slot" }], JSON.stringify(slot));
  }
  for (const slot of ["25:00", "9:5", "0930", "09:60", 930, "09:30:00"]) {
    const r = await sched(slot);
    assert.deepEqual([r.status, r.body.code, r.body.message, r.body.detail], [422, "VALIDATION", "时间点格式不对（应为北京时间 HH:MM）", { field: "slot" }], JSON.stringify(slot));
  }
  let r = await sched("11:11");
  assert.deepEqual([r.status, r.body.message, r.body.detail], [422, "时间点 11:11 不在账号 环境-DEMO 的执行时间里（09:30、14:00、20:00）", { field: "slot", schedule_times: ["09:30", "14:00", "20:00"] }]);
  r = await sched("20:00");
  assert.deepEqual([r.status, r.body.message, r.body.detail], [422, "北京时间还没有到 20:00，不能开始这个时间点的批次", { field: "slot" }]);
  srv.state.account.schedule_times = [];
  r = await sched("14:00");
  assert.deepEqual([r.status, r.body.message, r.body.detail], [422, "时间点 14:00 不在账号 环境-DEMO 的执行时间里（未设置）", { field: "slot", schedule_times: [] }]);
  srv.state.account.purchase_mode = "manual";
  r = await sched("14:00");
  assert.deepEqual([r.status, r.body.message, r.body.detail], [422, "账号 环境-DEMO 不是定时执行模式，不能按时间点开始", { field: "slot", purchase_mode: "manual" }]);
  srv.state.account.purchase_mode = "scheduled";
  srv.state.account.schedule_times = ["09:30"];
  r = await sched(" 9:30 ");
  assert.equal(r.status, 200, "写成 H:MM、带空白的照样认，按 HH:MM 记");
  assert.equal(srv.state.runs.get(r.body.run_id).scheduled_slot, "09:30");
});

test("登录状态的原话：没读到买家号 ID；执行中换了买家号（领取不到的原因）", async () => {
  const srv = createFakeServer({ tasks: [TASK], identity: { signed_in: true, customer_id: null }, account: { purchase_mode: "manual" } });
  const call = caller(srv);
  let r = await startManual(call);
  assert.deepEqual([r.status, r.body.message, r.body.detail.login_state], [403, "插件没有读到浏览器登录的买家号 ID，请打开亚马逊订单页后重试", "no_id"]);
  srv.setLogin({ signed_in: true, customer_id: DEMO_CID });
  r = await startManual(call);
  assert.equal(r.status, 200);
  srv.setLogin({ signed_in: true, customer_id: OTHER_CID });
  const n = await call(`/plugin/v1/runs/${r.body.run_id}/next`);
  assert.deepEqual(n.body, { attempt: null, reason: "浏览器登录的买家号变了（现在是 A9OTHER00000，这个批次的账号 环境-DEMO 登记的是 A1DEMO00000），停止领取" });
});

test("租约到期那一刻起就算过期：lease_expires_at 之前一毫秒还能续租，到了那一刻 409 LEASE_LOST", async () => {
  const srv = createFakeServer({ account: { purchase_mode: "manual" } });
  const call = caller(srv);
  const s = await startManual(call);
  const run = srv.state.runs.get(s.body.run_id);
  srv.setNow(run.lease_until - 1);
  assert.equal((await call(`/plugin/v1/runs/${run.id}/heartbeat`)).status, 200);
  srv.setNow(run.lease_until);
  const r = await call(`/plugin/v1/runs/${run.id}/heartbeat`);
  assert.deepEqual([r.status, r.body.code, r.body.message], [409, "LEASE_LOST", "执行权已超时（租约过期），请重新开始批次"]);
  assert.deepEqual(Object.keys(r.body.detail).sort(), ["lease_expires_at", "run_id"]);
});

test("领取不到的两个原因：当天额度已用完；账号在别的批次还有在途尝试", async () => {
  const srv = createFakeServer({ tasks: [TASK], account: { purchase_mode: "manual", daily_limit: 2, used_today: 2 } });
  const call = caller(srv);
  const s = await startManual(call);
  let n = await call(`/plugin/v1/runs/${s.body.run_id}/next`);
  assert.deepEqual(n.body, { attempt: null, reason: "账号 环境-DEMO 今天（2026-11-05）的额度已用完：已占用 2 / 上限 2" });
  const hb = await call("/plugin/v1/heartbeat", { ext_version: "0.2.0" });
  assert.deepEqual(hb.body.account.quota_today, { limit: 2, used: 2, planned: 1, available: 0 });

  srv.state.account.used_today = 0;
  srv.state.attempts.set(70, { id: 70, task_id: 99, run_id: 1, account_id: srv.state.account.id, bj_date: "2026-11-05", status: "submitting", submitted_at: srv.now() });
  n = await call(`/plugin/v1/runs/${s.body.run_id}/next`);
  assert.deepEqual(n.body, { attempt: null, reason: "账号还有一笔进行中的采购尝试（#70），等它结束后再领取" });
});

/** 走到护栏放行：开始批次 → 领取 → 进度 → 护栏。 */
async function toGuardPassed(srv, call) {
  const s = await startManual(call);
  const n = await call(`/plugin/v1/runs/${s.body.run_id}/next`);
  const a = parseAttempt(n.body.attempt);
  await call(`/plugin/v1/attempts/${a.attempt_id}/progress`, { stage: "started", detail: {} });
  const facts = buildCheckoutFacts(a, "pass", srv.now(), DEMO_CID);
  const g = await call(`/plugin/v1/attempts/${a.attempt_id}/guard`, facts);
  assert.equal(g.body.verdict, "pass");
  return { runId: s.body.run_id, a, facts };
}

test("提交登记：任务已不在执行这一笔（控制台改了它）→ 409「任务状态已变化，不能提交」，detail.order_status", async () => {
  const srv = createFakeServer({ tasks: [TASK], account: { purchase_mode: "manual" } });
  const call = caller(srv);
  const { a } = await toGuardPassed(srv, call);
  srv.state.tasks[0].status = "assigned";
  const r = await call(`/plugin/v1/attempts/${a.attempt_id}/submitting`);
  assert.deepEqual([r.status, r.body.code, r.body.message, r.body.detail], [409, "CONFLICT", "任务状态已变化，不能提交", { order_status: "assigned" }]);
  assert.equal(srv.state.attempts.get(a.attempt_id).submitted_at, null, "什么都没改");
});

test("迟到回执：尝试已由控制台核实（未下单 / 已下单）而这份不同 → 409 late_receipt_recorded（插件算送达），不改核实结论", async () => {
  const srv = createFakeServer({ tasks: [TASK], account: { purchase_mode: "manual" } });
  const call = caller(srv);
  const { a, facts } = await toGuardPassed(srv, call);
  assert.equal((await call(`/plugin/v1/attempts/${a.attempt_id}/submitting`)).status, 200);
  srv.resolve(a.attempt_id, "not_ordered");
  const client = new PluginClient({ baseUrl: URL_A, token: DEMO_TOKEN, extVersion: "0.2.0", fetch: srv.fetch, sleep: async () => {} });
  const body = successResult(facts, simOrderNo(a.attempt_id, srv.now()));
  const r = await client.result(a.attempt_id, body);
  assert.equal(r.ok, false);
  assert.deepEqual([r.error.status, r.error.code], [409, "CONFLICT"]);
  assert.equal(r.error.message, "该采购尝试已人工核实为「未下单」，迟到的结果（成功）已记录，与核实结论矛盾，请在控制台核实是否重复下单");
  assert.deepEqual(r.error.detail, { attempt_status: "failed", order_status: "assigned", late_receipt_recorded: true });
  assert.equal(classifyResultReply(r), "delivered", "插件按送达处理，不再补交");
  assert.equal(srv.state.attempts.get(a.attempt_id).status, "failed", "不改核实结论");

  // 已下单（单号与回执一致）：同一份是重复上报；失败的回执与结论不矛盾，只记下
  const srv2 = createFakeServer({ tasks: [TASK], account: { purchase_mode: "manual" } });
  const call2 = caller(srv2);
  const g2 = await toGuardPassed(srv2, call2);
  await call2(`/plugin/v1/attempts/${g2.a.attempt_id}/submitting`);
  const no = simOrderNo(g2.a.attempt_id, srv2.now());
  srv2.resolve(g2.a.attempt_id, "ordered", no);
  const dup = await call2(`/plugin/v1/attempts/${g2.a.attempt_id}/result`, successResult(g2.facts, no));
  assert.deepEqual([dup.status, dup.body.duplicate], [200, true]);
  const late = await call2(`/plugin/v1/attempts/${g2.a.attempt_id}/result`, { outcome: "unknown", error_code: null });
  assert.deepEqual([late.status, late.body.message, late.body.detail.late_receipt_recorded], [409, "该采购尝试已人工核实为「已下单」，迟到的结果（结果不明）已记录，不改变核实结论", true]);
});

test("同一个定时时间点：批次还在执行 → 后到的浏览器得到 ACCOUNT_BUSY（带持有者）；它没跑完就结束（租约过期被清扫）→ 再问就能开始；以 finished 结束之后 → SLOT_DONE", async () => {
  const srv = createFakeServer({ tasks: [TASK], now: AT_1400 });
  const a = caller(srv);
  const b = caller(srv, OTHER_TOKEN);
  assert.equal((await caller(srv, null)("/plugin/v1/register", { device_token: OTHER_TOKEN, ext_version: "0.2.0" })).status, 200);
  await b("/plugin/v1/identity", { signed_in: true, customer_id: DEMO_CID, ext_version: "0.2.0" });
  const slot = { customer_id: DEMO_CID, trigger: "scheduled", slot: "14:00", simulated: true };

  const sa = await a("/plugin/v1/runs", slot);
  assert.equal(sa.status, 200);
  assert.equal((await a(`/plugin/v1/runs/${sa.body.run_id}/next`)).body.attempt.order_id, 1, "A 领到了这一笔");
  let rb = await b("/plugin/v1/runs", slot);
  assert.deepEqual([rb.status, rb.body.code], [409, "ACCOUNT_BUSY"], "不是 SLOT_DONE：这个时间点还没执行完");
  assert.deepEqual(rb.body.detail, { holder_device_id: 5, holder_device_name: "浏览器 5", since: "2026-11-05T06:00:05.000Z" });
  assert.equal(rb.body.message, "账号 环境-DEMO 正由浏览器「浏览器 5」执行（北京时间 11-05 14:00 起），同一账号同时只能有一个执行者");

  // A 的浏览器崩了：租约过期、清扫结束批次，这一笔回到已分配
  srv.advance(180_000);
  assert.deepEqual(srv.sweep(), [sa.body.run_id]);
  assert.equal(srv.state.runs.get(sa.body.run_id).end_reason, "lease_expired");
  assert.equal(srv.state.tasks[0].status, "assigned");
  rb = await b("/plugin/v1/runs", slot);
  assert.equal(rb.status, 200, JSON.stringify(rb.body));
  const nb = await b(`/plugin/v1/runs/${rb.body.run_id}/next`);
  assert.deepEqual([nb.body.attempt.order_id, nb.body.attempt.attempt_no], [1, 2], "B 接手了同一笔");

  // B 正常结束：这个时间点今天执行过了，谁再问都是 SLOT_DONE
  await b(`/plugin/v1/attempts/${nb.body.attempt.attempt_id}/result`, { outcome: "failed", error_code: "OUT_OF_STOCK" });
  assert.equal((await b(`/plugin/v1/runs/${rb.body.run_id}/finish`, { reason: "finished" })).status, 200);
  for (const who of [a, b]) {
    const r = await who("/plugin/v1/runs", slot);
    assert.deepEqual([r.status, r.body.code, r.body.message], [409, "SLOT_DONE", "这个时间点今天已经执行过"]);
  }
});

test("登记上限：只数还在用的浏览器（移除了的不算）；到上限时 429 RATE_LIMITED，原话写明怎么办，不带等待秒数", async () => {
  const srv = createFakeServer({ maxDevices: 1 });
  const reg = caller(srv, null);
  const bad = await reg("/plugin/v1/register", { device_token: "short", ext_version: "0.2.0" });
  assert.deepEqual([bad.status, bad.body.message, bad.body.detail], [422, "浏览器令牌格式不对（应为 32–128 位字母、数字、下划线或连字符）", { field: "device_token" }]);
  const r = await reg("/plugin/v1/register", { device_token: OTHER_TOKEN, ext_version: "0.2.0" });
  assert.deepEqual([r.status, r.body.code, r.body.message, r.body.detail], [429, "RATE_LIMITED", MAX_DEVICES_MESSAGE, {}]);
  assert.equal(r.headers.get("retry-after"), null);
  assert.equal(MAX_DEVICES_MESSAGE, "登记的浏览器太多：请在控制台「账号与浏览器」里移除不用的浏览器，或调大 PC_MAX_DEVICES");
  srv.state.devices[0].hidden = true; // 控制台「移除」
  const ok = await reg("/plugin/v1/register", { device_token: OTHER_TOKEN, ext_version: "0.2.0" });
  assert.deepEqual([ok.status, ok.body.created], [200, true]);
  // 已登记的浏览器再登记（幂等）不查上限，被移除的那个再联系也回到列表
  const again = await reg("/plugin/v1/register", { device_token: DEMO_TOKEN, ext_version: "0.2.0" });
  assert.deepEqual([again.status, again.body.created, srv.state.devices[0].hidden], [200, false, false]);
});
