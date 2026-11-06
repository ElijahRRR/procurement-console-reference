/** 协议客户端：错误映射、ACCOUNT_BUSY 详情、只对 network 失败重试、提交登记绝不自动重试、
 *  HTTP 200 也要校验形状。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { FINISH_REASON, PluginClient, RETRY_ON_5XX, RETRY_POLICY, httpError, pairFailureText, parseBusyHolder, parseRetryAfter, retryAfterSeconds } from "../build/core/client.js";

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
const client = (fetch, extra = {}) =>
  new PluginClient({ baseUrl: BASE, token: "tok-demo-1", extVersion: "0.1.0", fetch, sleep: async () => {}, ...extra });

const HB = { device: { id: 5, name: "Mac-AdsPower-DEMO", bound_account_id: 3 }, account: null, identity_status: "match", server_time: "2026-11-05T06:00:00Z", bj_date: "2026-11-05", simulation_only: true, active_run: null };

test("请求带设备令牌，地址末尾斜杠被去掉，开始批次永远 simulated: true", async () => {
  const s = scripted(json(200, { id: 9 }));
  const r = await client(s.fetch).startRun("manual");
  assert.equal(r.ok, true);
  assert.equal(r.data.run_id, 9);
  assert.equal(s.calls[0].url, "http://127.0.0.1:8790/plugin/v1/runs");
  assert.equal(s.calls[0].init.headers.Authorization, "Bearer tok-demo-1");
  assert.deepEqual(s.calls[0].body, { trigger: "manual", simulated: true });
});

test("配对不带令牌；返回缺 device_token 视为响应格式不对", async () => {
  const s = scripted(json(200, { device_id: 5, device_token: "tok-x", workspace_name: "工作区-DEMO" }), json(200, { device_id: 5 }));
  const c = new PluginClient({ baseUrl: BASE, extVersion: "0.1.0", fetch: s.fetch });
  const ok = await c.pair("DEMO2345", "Mac-DEMO", "adspower");
  assert.equal(ok.ok, true);
  assert.equal(s.calls[0].init.headers.Authorization, undefined);
  assert.deepEqual(s.calls[0].body, { code: "DEMO2345", device_name: "Mac-DEMO", browser: "adspower", ext_version: "0.1.0" });
  const bad = await c.pair("DEMO2345", "Mac-DEMO", "adspower");
  assert.equal(bad.ok, false);
  assert.equal(bad.error.kind, "bad_response");
});

test("错误映射：契约错误体 {code, message, detail}", async () => {
  const s = scripted(json(403, { code: "IDENTITY_MISMATCH", message: "身份不一致", detail: null }));
  const r = await client(s.fetch).startRun("scheduled");
  assert.equal(r.ok, false);
  assert.deepEqual(
    { kind: r.error.kind, status: r.error.status, code: r.error.code, message: r.error.message },
    { kind: "http", status: 403, code: "IDENTITY_MISMATCH", message: "身份不一致" },
  );
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
  assert.match(c.message, /重新配对/);
  const d = httpError(503, null);
  assert.equal(d.code, "HTTP_503");
  assert.equal(d.message, "服务端出错（状态码 503）");
  assert.equal(httpError(500, { code: "INTERNAL", message: "Internal Server Error" }).message, "服务端内部错误");
  assert.equal(httpError(405, null).code, "METHOD_NOT_ALLOWED");
  assert.equal(httpError(429, null).code, "RATE_LIMITED");
});

test("ACCOUNT_BUSY 的 detail 带持有者设备名与开始时间", async () => {
  const s = scripted(json(409, {
    code: "ACCOUNT_BUSY",
    message: "账号正在其他设备执行",
    detail: { holder_device_id: 6, holder_device_name: "PC-DEMO-2", since: "2026-11-05T06:01:00+00:00" },
  }));
  const r = await client(s.fetch).startRun("manual");
  assert.equal(r.error.code, "ACCOUNT_BUSY");
  assert.deepEqual(parseBusyHolder(r.error.detail), {
    holder_device_id: 6,
    holder_device_name: "PC-DEMO-2",
    since: "2026-11-05T06:01:00+00:00",
  });
  assert.deepEqual(parseBusyHolder("garbage"), { holder_device_id: null, holder_device_name: null, since: null });
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

test("提交登记断网绝不自动重试；开始批次、领取、配对同样不重试", async () => {
  for (const [name, call] of [
    ["submitting", (c) => c.submitting(7)],
    ["startRun", (c) => c.startRun("manual")],
    ["next", (c) => c.next(9)],
    ["pair", (c) => c.pair("DEMO2345", "Mac-DEMO", "chrome")],
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

test("心跳：身份为 null 时不带 identity 键；配置按契约 §10 解析", async () => {
  const s = scripted(json(200, {
    ...HB,
    account: {
      id: 3, name: "环境-DEMO", amazon_customer_id: "A1DEMO0000", purchase_mode: "scheduled", schedule_times: ["14:00"],
      effective_max_delivery_days: 10, purchase_enabled: true, execution_paused: false, pause_reason: null,
      min_amount: "20.00", max_amount: "50.00", daily_limit: 20, expected_card_last4: null,
      quota_today: { limit: 20, used: 3, planned: 5, available: 12 },
    },
    identity_status: "weird",
  }));
  const r = await client(s.fetch).heartbeat(null);
  assert.deepEqual(s.calls[0].body, { ext_version: "0.1.0" });
  assert.equal(r.data.account.quota_today.available, 12);
  assert.deepEqual(r.data.account.schedule_times, ["14:00"]);
  assert.equal(r.data.identity_status, "unknown", "认不出的身份状态按 unknown");
  const s2 = scripted(json(200, HB));
  await client(s2.fetch).heartbeat({ signed_in: true, customer_id: "A1DEMO0000", site: "US", page_url: "https://www.amazon.com/gp/css/order-history" });
  assert.equal(s2.calls[0].body.identity.customer_id, "A1DEMO0000");
});

test("开始批次的返回形状 {id} / {run_id} / {run: {id}} 都认", async () => {
  for (const body of [{ id: 9 }, { run_id: "9" }, { run: { id: 9, lease_expires_at: "2026-11-05T06:03:00Z" } }]) {
    const r = await client(scripted(json(200, body)).fetch).startRun("manual");
    assert.equal(r.data.run_id, 9);
  }
  assert.equal((await client(scripted(json(200, { ok: true })).fetch).startRun("manual")).ok, false);
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
  for (const [name, call] of [["heartbeat", (c) => c.heartbeat(null)], ["identity", (c) => c.identity({ signed_in: false, customer_id: null, site: "US", page_url: null })]]) {
    const s = scripted(json(500, { code: "INTERNAL", message: "boom" }), json(200, HB));
    await call(client(s.fetch));
    assert.equal(s.calls.length, 1, `${name} 不因 5xx 再发`);
  }
});

test("5xx 也绝不让提交登记、开始批次、领取、护栏再发", async () => {
  for (const [name, call] of [
    ["submitting", (c) => c.submitting(7)],
    ["startRun", (c) => c.startRun("manual")],
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
  const r = await client(scripted(json(200, { run_id: 9, lease_ttl_s: 60, lease_expires_at: "2026-11-05T06:01:00Z" })).fetch).startRun("manual");
  assert.deepEqual(r.data, { run_id: 9, lease_ttl_s: 60, lease_expires_at: "2026-11-05T06:01:00Z" });
  for (const bad of [0, -5, "x", null]) {
    const b = await client(scripted(json(200, { run_id: 9, lease_ttl_s: bad })).fetch).startRun("manual");
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

test("Retry-After：detail.retry_after_s 优先，其次响应头；配对限速给出秒数", async () => {
  const s = scripted(new Response(JSON.stringify({ code: "RATE_LIMITED", message: "尝试次数过多，请稍后再试", detail: {} }), { status: 429, headers: { "retry-after": "17" } }));
  const r = await client(s.fetch, { token: null }).pair("DEMO2345", "Mac-DEMO", "chrome");
  assert.equal(r.ok, false);
  assert.equal(r.error.code, "RATE_LIMITED");
  assert.equal(r.error.retry_after_s, 17);
  assert.equal(retryAfterSeconds(r.error), 17);
  assert.equal(pairFailureText(r.error, "http://127.0.0.1:8790"), "配对失败：尝试次数过多，请 17 秒后再试");
  const d = httpError(429, { code: "RATE_LIMITED", message: "尝试次数过多，请稍后再试", detail: { retry_after_s: 42 } }, "17");
  assert.equal(retryAfterSeconds(d), 42);
  assert.equal(pairFailureText(httpError(429, null), "http://127.0.0.1:8790"), "配对失败：尝试次数过多，请稍后再试");
  assert.equal(parseRetryAfter("Wed, 21 Oct 2026 07:28:00 GMT"), null, "HTTP 日期格式不认");
});

test("配对被 Host 允许名单拒绝（403 FORBIDDEN）：提示改用 127.0.0.1 / localhost，或请管理员加进 PC_ALLOWED_HOSTS", () => {
  const e = httpError(403, { code: "FORBIDDEN", message: "不接受这个主机名的请求：只接受本机地址；其他地址请在 PC_ALLOWED_HOSTS 里登记", detail: {} });
  const t = pairFailureText(e, "http://localhost:9001");
  assert.match(t, /^配对失败：服务端不接受主机名 localhost 的请求/);
  assert.match(t, /http:\/\/127\.0\.0\.1:9001 或 http:\/\/localhost:9001/);
  assert.match(t, /请管理员把 localhost 加进服务端的 PC_ALLOWED_HOSTS/);
  assert.equal(pairFailureText(httpError(400, { code: "VALIDATION", message: "配对码无效或已过期" }), "http://127.0.0.1:8790"), "配对失败：配对码无效或已过期");
});

test("没说上话：给人看的原因是中文，不带浏览器的英文报错", async () => {
  const r = await client(scripted(new TypeError("Failed to fetch")).fetch, { retryDelaysMs: [] }).heartbeat(null);
  assert.equal(r.error.kind, "network");
  assert.doesNotMatch(r.error.message, /[A-Za-z]{4,}/);
  assert.match(r.error.message, /^无法连接服务端/);
});
