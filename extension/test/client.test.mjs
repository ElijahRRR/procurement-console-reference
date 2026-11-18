/** 协议客户端：登记（不带令牌头）、心跳与身份上报的新形状、开始批次带买家号 ID 与时间点、错误映射、
 *  ACCOUNT_BUSY / DEVICE_BUSY 详情、只对 network 失败重试、提交登记绝不自动重试、HTTP 200 也要校验形状。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  FINISH_REASON,
  PluginClient,
  RETRY_ON_5XX,
  RETRY_POLICY,
  httpError,
  parseBusyHolder,
  parseDeviceBusy,
  parseIdentityRejection,
  parseLoginState,
  parseRetryAfter,
  retryAfterSeconds,
} from "../build/core/client.js";

const BASE = "http://127.0.0.1:8790/";

/** 按顺序回放响应的假 fetch。每一项是 Response、Error（抛出）或函数。 */
function scripted(...steps) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init, body: init.body ? JSON.parse(init.body) : null });
    const s = steps.length > 1 ? steps.shift() : steps[0];
    const v = typeof s === "function" ? s(url, init) : s;
    if (v instanceof Error) throw v;
    return v.clone ? v.clone() : v;
  };
  return { fetch, calls };
}
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const netErr = () => new TypeError("fetch failed");
const TOKEN = "demo-token-000000000000000000000000001";
const CID = "A1DEMO00000";
const client = (fetch, extra = {}) =>
  new PluginClient({ baseUrl: BASE, token: TOKEN, extVersion: "0.2.0", fetch, sleep: async () => {}, ...extra });

/** 心跳与身份上报的返回（同一形状，规格 §2.4）。 */
const HB = {
  device: { id: 12, name: "浏览器 12" },
  login: { state: "unknown", customer_id: null, at: null, stale: false },
  account: null,
  device_run: null,
  server_time: "2026-11-18T06:00:00Z",
  bj_date: "2026-11-18",
  simulation_only: true,
};
const ACCOUNT = {
  id: 3, name: "环境-A", amazon_customer_id: CID, purchase_enabled: true, execution_paused: false, pause_reason: null,
  purchase_mode: "scheduled", schedule_times: ["14:00"], effective_max_delivery_days: 7,
  quota_today: { limit: 20, used: 1, planned: 4, available: 19 }, executable_today: 4, future_assigned: 2,
  active_run: { id: 9, device_id: 12, device_name: "浏览器 12", started_at: "2026-11-18T05:58:00Z", is_mine: true },
};

test("请求带设备令牌与 JSON 的 Content-Type，地址末尾斜杠被去掉；开始批次带买家号 ID，永远 simulated: true", async () => {
  const s = scripted(json(200, { run_id: 9, lease_ttl_s: 180, lease_expires_at: "2026-11-18T06:03:00Z", account: { id: 3, name: "环境-A" } }));
  const r = await client(s.fetch).startRun("manual", CID);
  assert.equal(r.ok, true);
  assert.deepEqual(r.data, { run_id: 9, lease_ttl_s: 180, lease_expires_at: "2026-11-18T06:03:00Z", account: { id: 3, name: "环境-A" } });
  assert.equal(s.calls[0].url, "http://127.0.0.1:8790/plugin/v1/runs");
  assert.equal(s.calls[0].init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(s.calls[0].init.headers["Content-Type"], "application/json", "服务端的插件入口闸只接受 JSON 请求");
  assert.deepEqual(s.calls[0].body, { customer_id: CID, trigger: "manual", simulated: true });
});

test("开始批次：定时带 slot；手动即使传了 slot 也不带", async () => {
  const s = scripted(json(200, { run_id: 9 }));
  await client(s.fetch).startRun("scheduled", CID, "14:00");
  assert.deepEqual(s.calls[0].body, { customer_id: CID, trigger: "scheduled", slot: "14:00", simulated: true });
  await client(s.fetch).startRun("manual", CID, "14:00");
  assert.deepEqual(s.calls[1].body, { customer_id: CID, trigger: "manual", simulated: true });
});

test("登记：令牌在请求体里、**不带 Authorization**（即使客户端持有令牌）；工作区 ID 只在填了的时候带；缺 device_id 视为响应格式不对", async () => {
  const s = scripted(
    json(200, { device_id: 12, device_name: "浏览器 12", workspace_id: 1, workspace_name: "工作区-DEMO", created: true }),
    json(200, { device_id: 12, device_name: "浏览器 12", workspace_id: 2, workspace_name: null, created: false }),
    json(200, { device_name: "浏览器 12" }),
  );
  const c = client(s.fetch); // 持有令牌的客户端
  const ok = await c.register(TOKEN);
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.data, { device_id: 12, device_name: "浏览器 12", workspace_id: 1, workspace_name: "工作区-DEMO", created: true });
  assert.equal(s.calls[0].url, "http://127.0.0.1:8790/plugin/v1/register");
  assert.equal(s.calls[0].init.headers.Authorization, undefined, "登记不带令牌头");
  assert.deepEqual(s.calls[0].body, { device_token: TOKEN, ext_version: "0.2.0" });
  const again = await c.register(TOKEN, 2);
  assert.deepEqual(s.calls[1].body, { device_token: TOKEN, ext_version: "0.2.0", workspace_id: 2 });
  assert.equal(again.data.created, false);
  const bad = await c.register(TOKEN);
  assert.equal(bad.ok, false);
  assert.equal(bad.error.kind, "bad_response");
});

test("登记的失败：多个工作区（409 WORKSPACE_REQUIRED）、插件过旧（409 PLUGIN_OUTDATED）、上限（429 带秒数）都带着服务端的原话", async () => {
  const c = (resp) => client(scripted(resp).fetch).register(TOKEN);
  const ws = await c(json(409, { code: "WORKSPACE_REQUIRED", message: "系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）", detail: {} }));
  assert.deepEqual([ws.error.status, ws.error.code], [409, "WORKSPACE_REQUIRED"]);
  assert.match(ws.error.message, /工作区 ID/);
  const old = await c(json(409, { code: "PLUGIN_OUTDATED", message: "插件版本过旧：请重新构建并加载新版插件（需要 0.2.0 及以上）", detail: {} }));
  assert.equal(old.error.code, "PLUGIN_OUTDATED");
  const rl = await c(new Response(JSON.stringify({ code: "RATE_LIMITED", message: "登记的浏览器太多：请稍后再试", detail: { retry_after_s: 42 } }), { status: 429 }));
  assert.equal(retryAfterSeconds(rl.error), 42);
});

test("错误映射：契约错误体 {code, message, detail}", async () => {
  const s = scripted(json(403, { code: "IDENTITY_MISMATCH", message: "浏览器没有登录亚马逊", detail: { login_state: "signed_out", identity_at: "2026-11-18T05:59:00Z", stale: false } }));
  const r = await client(s.fetch).startRun("scheduled", CID, "14:00");
  assert.equal(r.ok, false);
  assert.deepEqual(
    { kind: r.error.kind, status: r.error.status, code: r.error.code, message: r.error.message },
    { kind: "http", status: 403, code: "IDENTITY_MISMATCH", message: "浏览器没有登录亚马逊" },
  );
  assert.deepEqual(parseIdentityRejection(r.error.detail), { login_state: "signed_out", identity_at: "2026-11-18T05:59:00Z", stale: false });
  assert.deepEqual(parseIdentityRejection("garbage"), { login_state: null, identity_at: null, stale: false });
});

test("错误映射：FastAPI 原生 {detail: {...}} 与 {detail: '...'}，码缺失按状态码推", () => {
  const a = httpError(409, { detail: { code: "ACCOUNT_BUSY", message: "占用", detail: { holder_device_name: "PC-DEMO" } } });
  assert.equal(a.code, "ACCOUNT_BUSY");
  assert.equal(a.message, "占用");
  assert.deepEqual(a.detail, { holder_device_name: "PC-DEMO" });
  const b = httpError(404, { detail: "Not Found" });
  assert.equal(b.code, "NOT_FOUND");
  assert.equal(b.message, "服务端没有这个接口或资源", "框架层的英文短语不直接给人看");
  assert.equal(httpError(404, { detail: "采购尝试不存在" }).message, "采购尝试不存在", "中文原话照用");
  const c = httpError(401, null);
  assert.equal(c.code, "UNAUTHORIZED");
  assert.match(c.message, /自动重新登记/);
  assert.doesNotMatch(c.message, /配对/);
  const d = httpError(503, null);
  assert.equal(d.code, "HTTP_503");
  assert.equal(d.message, "服务端出错（状态码 503）");
  assert.equal(httpError(500, { code: "INTERNAL", message: "Internal Server Error" }).message, "服务端内部错误");
  assert.equal(httpError(405, null).code, "METHOD_NOT_ALLOWED");
  assert.equal(httpError(429, null).code, "RATE_LIMITED");
});

test("ACCOUNT_BUSY 的 detail 带占用的浏览器名与开始时间；DEVICE_BUSY 的 detail 带本浏览器未结束的批次", async () => {
  const s = scripted(json(409, {
    code: "ACCOUNT_BUSY",
    message: "账号 环境-A 正由浏览器「浏览器 13」执行",
    detail: { holder_device_id: 13, holder_device_name: "浏览器 13", since: "2026-11-05T06:01:00+00:00" },
  }));
  const r = await client(s.fetch).startRun("manual", CID);
  assert.equal(r.error.code, "ACCOUNT_BUSY");
  assert.deepEqual(parseBusyHolder(r.error.detail), {
    holder_device_id: 13,
    holder_device_name: "浏览器 13",
    since: "2026-11-05T06:01:00+00:00",
  });
  assert.deepEqual(parseBusyHolder("garbage"), { holder_device_id: null, holder_device_name: null, since: null });
  const d = await client(scripted(json(409, {
    code: "DEVICE_BUSY",
    message: "这个浏览器上一个批次还没有结束",
    detail: { run_id: 9, account_name: "环境-A", since: "2026-11-05T06:01:00+00:00", lease_expires_at: "2026-11-05T06:04:00+00:00" },
  })).fetch).startRun("manual", CID);
  assert.deepEqual([d.error.status, d.error.code], [409, "DEVICE_BUSY"]);
  assert.deepEqual(parseDeviceBusy(d.error.detail), { run_id: 9, account_name: "环境-A", since: "2026-11-05T06:01:00+00:00", lease_expires_at: "2026-11-05T06:04:00+00:00" });
  assert.deepEqual(parseDeviceBusy(null), { run_id: null, account_name: null, since: null, lease_expires_at: null });
});

test("非 2xx 且不是 JSON → http 错误，不重试", async () => {
  const s = scripted(new Response("<html>bad gateway</html>", { status: 502 }));
  const r = await client(s.fetch).heartbeat(null);
  assert.equal(r.error.kind, "http");
  assert.equal(r.error.code, "HTTP_502");
  assert.equal(s.calls.length, 1);
});

test("HTTP 200 不等于业务成功：不是 JSON、形状不对都算失败", async () => {
  const c1 = client(scripted(new Response("ok", { status: 200 })).fetch);
  assert.equal((await c1.heartbeat(null)).error.code, "BAD_RESPONSE");
  const c2 = client(scripted(json(200, { verdict: "ok" })).fetch);
  const g = await c2.guard(7, {});
  assert.equal(g.ok, false);
  assert.equal(g.error.kind, "bad_response");
  const c3 = client(scripted(json(200, { stop: "no" })).fetch);
  assert.equal((await c3.runHeartbeat(9)).ok, false);
  const c4 = client(scripted(json(200, {})).fetch);
  assert.equal((await c4.progress(7, "started", {})).ok, false, "progress 缺 continue 不能当成继续");
  assert.equal((await c4.next(9)).ok, false, "next 缺 attempt 键不能当成没有任务");
  assert.equal((await c4.heartbeat(null)).ok, false, "心跳缺 device 不认");
  const noLogin = client(scripted(json(200, { ...HB, login: undefined })).fetch);
  assert.equal((await noLogin.heartbeat(null)).ok, false, "心跳缺 login 不认（旧版服务端的形状不读成「登录状态未知」）");
  const oldShape = client(scripted(json(200, { device: { id: 5, name: "D", bound_account_id: 3 }, account: null, identity_status: "match", active_run: null })).fetch);
  assert.equal((await oldShape.heartbeat(null)).error.code, "BAD_RESPONSE");
});

test("提交登记只认 HTTP 200（202、204 都不算）", async () => {
  const s = scripted(new Response(null, { status: 202 }), new Response(null, { status: 204 }), json(200, { ok: true }));
  const c = client(s.fetch);
  assert.equal((await c.submitting(7)).ok, false);
  assert.equal((await c.submitting(7)).ok, false);
  assert.equal((await c.submitting(7)).ok, true);
});

test("重试只针对 network 失败：心跳断两次后第三次成功", async () => {
  const s = scripted(netErr(), netErr(), json(200, HB));
  const r = await client(s.fetch).heartbeat(null);
  assert.equal(r.ok, true);
  assert.equal(s.calls.length, 3);
});

test("重试有上限：一直断网时心跳一共发 3 次后放弃，结论是 network", async () => {
  const s = scripted(netErr());
  const r = await client(s.fetch).heartbeat(null);
  assert.equal(r.error.kind, "network");
  assert.equal(r.error.code, "NETWORK");
  assert.equal(s.calls.length, 3);
});

test("服务端明确拒绝不重试（500 带错误体也一样）", async () => {
  const s = scripted(json(500, { code: "INTERNAL", message: "boom" }));
  const r = await client(s.fetch).heartbeat(null);
  assert.equal(r.error.kind, "http");
  assert.equal(s.calls.length, 1);
});

test("提交登记断网绝不自动重试；开始批次、领取同样不重试；登记也不在客户端里连发（由后台按退避再试）", async () => {
  for (const [name, call] of [
    ["submitting", (c) => c.submitting(7)],
    ["startRun", (c) => c.startRun("manual", CID)],
    ["next", (c) => c.next(9)],
    ["register", (c) => c.register(TOKEN)],
  ]) {
    const s = scripted(netErr(), json(200, { ok: true }));
    const r = await call(client(s.fetch));
    assert.equal(r.ok, false, name);
    assert.equal(r.error.kind, "network", name);
    assert.equal(s.calls.length, 1, `${name} 只发了一次`);
    assert.equal(RETRY_POLICY[name], false);
  }
  assert.equal(RETRY_POLICY.submitting, false);
});

test("超时 → TIMEOUT（可重试的接口会再试）", async () => {
  const timeout = new DOMException("The operation timed out.", "TimeoutError");
  const s = scripted(timeout, json(200, { continue: true, cancel_reason: null }));
  const r = await client(s.fetch).progress(7, "started", {});
  assert.equal(r.ok, true);
  assert.equal(s.calls.length, 2);
  const s2 = scripted(timeout);
  const r2 = await client(s2.fetch).submitting(7);
  assert.equal(r2.error.code, "TIMEOUT");
  assert.equal(r2.error.message, "请求超时");
});

test("心跳：身份为 null 时不带 identity 键；新形状 {device, login, account, device_run} 逐项解析", async () => {
  const s = scripted(json(200, {
    ...HB,
    login: { state: "ready", customer_id: CID, at: "2026-11-18T05:59:30Z", stale: false },
    account: ACCOUNT,
    device_run: { id: 9, account_id: 3, account_name: "环境-A", started_at: "2026-11-18T05:58:00Z" },
  }));
  const r = await client(s.fetch).heartbeat(null);
  assert.deepEqual(s.calls[0].body, { ext_version: "0.2.0" });
  assert.deepEqual(r.data.device, { id: 12, name: "浏览器 12" });
  assert.deepEqual(r.data.login, { state: "ready", customer_id: CID, at: "2026-11-18T05:59:30Z", stale: false });
  assert.deepEqual(r.data.account, ACCOUNT, "账号配置原样读出（含 executable_today、future_assigned、active_run）");
  assert.equal("expected_card_last4" in r.data.account, false, "卡尾号与金额区间不在心跳里");
  assert.equal("min_amount" in r.data.account, false);
  assert.deepEqual(r.data.device_run, { id: 9, account_id: 3, account_name: "环境-A", started_at: "2026-11-18T05:58:00Z" });
  assert.equal(r.data.simulation_only, true);
  assert.equal(r.data.bj_date, "2026-11-18");
  const s2 = scripted(json(200, HB));
  await client(s2.fetch).heartbeat({ signed_in: true, customer_id: CID, site: "US", page_url: "https://www.amazon.com/gp/css/order-history", conflict: false });
  assert.deepEqual(s2.calls[0].body.identity, { signed_in: true, customer_id: CID, site: "US", page_url: "https://www.amazon.com/gp/css/order-history", conflict: false });
});

test("心跳：认不出的登录状态按 unknown（不往 ready 猜）；没给的数字是 null，不是 0；account、device_run 可以没有", async () => {
  const r = await client(scripted(json(200, { ...HB, login: { state: "weird", customer_id: CID, at: null }, account: { id: 3, name: "环境-A" } })).fetch).heartbeat(null);
  assert.equal(r.data.login.state, "unknown");
  assert.equal(r.data.login.stale, false);
  assert.equal(r.data.account.executable_today, null, "没给就是没读到");
  assert.equal(r.data.account.future_assigned, null);
  assert.equal(r.data.account.quota_today, null);
  assert.equal(r.data.account.active_run, null);
  assert.equal(r.data.device_run, null);
  for (const st of ["unknown", "signed_out", "no_id", "conflict", "unregistered", "ignored", "ready"]) assert.equal(parseLoginState(st), st);
  for (const v of ["match", "signed_in", "", null, 3]) assert.equal(parseLoginState(v), "unknown", String(v));
});

test("身份上报：请求体是身份字段本身（带 conflict）加 ext_version；返回与心跳同形", async () => {
  const s = scripted(json(200, { ...HB, login: { state: "conflict", customer_id: null, at: "2026-11-18T06:00:00Z", stale: false } }));
  const r = await client(s.fetch).identity({ signed_in: true, customer_id: "A9OTHER00000", site: "US", page_url: null, conflict: true });
  assert.equal(s.calls[0].url, "http://127.0.0.1:8790/plugin/v1/identity");
  assert.deepEqual(s.calls[0].body, { signed_in: true, customer_id: "A9OTHER00000", site: "US", page_url: null, conflict: true, ext_version: "0.2.0" });
  assert.equal(r.ok, true);
  assert.equal(r.data.login.state, "conflict");
  assert.deepEqual(r.data.device, { id: 12, name: "浏览器 12" });
  const bad = await client(scripted(json(200, { identity_status: "match" })).fetch).identity({ signed_in: false, customer_id: null, site: "US", page_url: null, conflict: false });
  assert.equal(bad.ok, false, "旧版只回 identity_status 的形状不认");
});

test("开始批次的返回形状 {id} / {run_id} / {run: {id}} 都认", async () => {
  for (const body of [{ id: 9 }, { run_id: "9" }, { run: { id: 9, lease_expires_at: "2026-11-05T06:03:00Z" } }]) {
    const r = await client(scripted(json(200, body)).fetch).startRun("manual", CID);
    assert.equal(r.data.run_id, 9);
    assert.equal(r.data.account, null, "没给账号就是 null");
  }
  assert.equal((await client(scripted(json(200, { ok: true })).fetch).startRun("manual", CID)).ok, false);
});

test("领取：attempt 为 null 是「没有任务」；尝试缺 ID 不认；simulated 只认 true", async () => {
  const none = await client(scripted(json(200, { attempt: null, reason: "今天没有可执行的任务" })).fetch).next(9);
  assert.deepEqual(none.data, { attempt: null, reason: "今天没有可执行的任务", retry_after_s: null });
  assert.equal((await client(scripted(json(200, { attempt: { asin: "B0DEMO0001" } })).fetch).next(9)).ok, false);
  const a = await client(scripted(json(200, { attempt: { attempt_id: 7, unit_price: 41.97, simulated: "true" } })).fetch).next(9);
  assert.equal(a.data.attempt.simulated, false);
  assert.equal(a.data.attempt.unit_price, "41.97");
});

test("5xx（服务端事务已回滚）：结果、进度、批次心跳、结束批次、等待验证原样再发；心跳、身份不再发", async () => {
  const body = { outcome: "succeeded", amazon_order_no: "SIM-20261105-7", amounts: null, card_last4: null, delivery: null, error_code: null, error_detail: null };
  for (const [name, call] of [
    ["result", (c) => c.result(7, body)],
    ["progress", (c) => c.progress(7, "started", {})],
    ["runHeartbeat", (c) => c.runHeartbeat(9)],
    ["finishRun", (c) => c.finishRun(9, "completed")],
    ["verifying", (c) => c.verifying(7, "银行验证")],
  ]) {
    const ok = name === "progress" ? { continue: true, cancel_reason: null } : name === "runHeartbeat" ? { stop: false, lease_expires_at: null, stop_reason: null } : { ok: true };
    const s = scripted(json(503, { code: "INTERNAL", message: "boom" }), json(200, ok));
    const r = await call(client(s.fetch));
    assert.equal(r.ok, true, name);
    assert.equal(s.calls.length, 2, `${name} 再发了一次`);
    assert.deepEqual(s.calls[1].body, s.calls[0].body, `${name} 再发的是同一份请求体`);
    assert.ok(RETRY_ON_5XX.has(name));
  }
  for (const [name, call] of [["heartbeat", (c) => c.heartbeat(null)], ["identity", (c) => c.identity({ signed_in: false, customer_id: null, site: "US", page_url: null, conflict: false })]]) {
    const s = scripted(json(500, { code: "INTERNAL", message: "boom" }), json(200, HB));
    await call(client(s.fetch));
    assert.equal(s.calls.length, 1, `${name} 不因 5xx 再发`);
  }
});

test("5xx 也绝不让提交登记、开始批次、领取、护栏再发", async () => {
  for (const [name, call] of [
    ["submitting", (c) => c.submitting(7)],
    ["startRun", (c) => c.startRun("manual", CID)],
    ["next", (c) => c.next(9)],
    ["guard", (c) => c.guard(7, {})],
  ]) {
    const s = scripted(json(500, { code: "INTERNAL", message: "boom" }), json(200, { ok: true }));
    const r = await call(client(s.fetch));
    assert.equal(r.ok, false, name);
    assert.equal(r.error.status, 500);
    assert.equal(s.calls.length, 1, `${name} 只发了一次`);
    assert.equal(RETRY_ON_5XX.has(name), false);
  }
});

test("4xx 是明确的答复：结果上报也不再发", async () => {
  const s = scripted(json(409, { code: "CONFLICT", message: "已登记为成功", detail: { attempt_status: "succeeded" } }), json(200, { ok: true }));
  const r = await client(s.fetch).result(7, { outcome: "failed" });
  assert.equal(r.error.code, "CONFLICT");
  assert.deepEqual(r.error.detail, { attempt_status: "succeeded" });
  assert.equal(s.calls.length, 1);
});

test("开始批次：读出 lease_ttl_s（正整数才认）；结束批次上报 reason=finished、细分原因放 detail", async () => {
  const r = await client(scripted(json(200, { run_id: 9, lease_ttl_s: 60, lease_expires_at: "2026-11-05T06:01:00Z" })).fetch).startRun("manual", CID);
  assert.deepEqual(r.data, { run_id: 9, lease_ttl_s: 60, lease_expires_at: "2026-11-05T06:01:00Z", account: null });
  for (const bad of [0, -5, "x", null]) {
    const b = await client(scripted(json(200, { run_id: 9, lease_ttl_s: bad })).fetch).startRun("manual", CID);
    assert.equal(b.data.lease_ttl_s, null, `lease_ttl_s=${bad} 读不出就是 null`);
  }
  const s = scripted(json(200, { run_id: 9, end_reason: "finished" }));
  await client(s.fetch).finishRun(9, "attempt_cap");
  assert.equal(FINISH_REASON, "finished");
  assert.deepEqual(s.calls[0].body, { reason: "finished", detail: "attempt_cap" });
});

test("进度：cancel_code 读出（没给为 null）；领取：retry_after_s 读出（只对 attempt 为 null）", async () => {
  const p1 = await client(scripted(json(200, { continue: false, cancel_reason: "账号已暂停执行：人工", cancel_code: "ACCOUNT_PAUSED" })).fetch).progress(7, "started", {});
  assert.deepEqual(p1.data, { continue: false, cancel_reason: "账号已暂停执行：人工", cancel_code: "ACCOUNT_PAUSED" });
  const p2 = await client(scripted(json(200, { continue: false, cancel_reason: "上游已取消" })).fetch).progress(7, "started", {});
  assert.equal(p2.data.cancel_code, null);
  const n = await client(scripted(json(200, { attempt: null, retry_after_s: 5, reason: "被占用" })).fetch).next(9);
  assert.deepEqual(n.data, { attempt: null, reason: "被占用", retry_after_s: 5 });
});

test("领取：retry_after_s 只要是不小于 0 的数（整数、小数、数字字符串）就算「稍后再领」", async () => {
  for (const [v, want] of [[5, 5], [2.5, 2.5], ["3", 3], [0, 0], [-1, null], ["soon", null], [null, null]]) {
    const r = await client(scripted(json(200, { attempt: null, reason: "占用", retry_after_s: v })).fetch).next(9);
    assert.equal(r.data.retry_after_s, want, String(v));
  }
});

test("Retry-After：detail.retry_after_s 优先，其次响应头；登记被限速时读得出秒数", async () => {
  const s = scripted(new Response(JSON.stringify({ code: "RATE_LIMITED", message: "登记的浏览器太多：请稍后再试", detail: {} }), { status: 429, headers: { "retry-after": "17" } }));
  const r = await client(s.fetch, { token: null }).register(TOKEN);
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "RATE_LIMITED");
  assert.equal(r.error.retry_after_s, 17);
  assert.equal(retryAfterSeconds(r.error), 17);
  assert.equal(r.error.message, "登记的浏览器太多：请稍后再试");
  const d = httpError(429, { code: "RATE_LIMITED", message: "登记的浏览器太多：请稍后再试", detail: { retry_after_s: 42 } }, "17");
  assert.equal(retryAfterSeconds(d), 42);
  assert.equal(retryAfterSeconds(httpError(429, null)), null, "没给就是没给");
  assert.equal(parseRetryAfter("Wed, 21 Oct 2026 07:28:00 GMT"), null, "HTTP 日期格式不认");
});

test("插件入口闸的拒绝（403 FORBIDDEN）：原话照用，不拼别的建议", () => {
  const e = httpError(403, { code: "FORBIDDEN", message: "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持", detail: {} });
  assert.deepEqual([e.kind, e.status, e.code], ["http", 403, "FORBIDDEN"]);
  assert.equal(e.message, "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持");
});

test("没说上话：给人看的原因是中文，不带浏览器的英文报错", async () => {
  const r = await client(scripted(new TypeError("Failed to fetch")).fetch, { retryDelaysMs: [] }).heartbeat(null);
  assert.equal(r.error.kind, "network");
  assert.doesNotMatch(r.error.message, /[A-Za-z]{4,}/);
  assert.match(r.error.message, /^无法连接服务端/);
});
