/** 后台（真正的 build/background/service-worker.js）对着假 chrome 环境与内存假服务端跑：
 *  自动登记（令牌由插件生成、单飞、退避、401 后同令牌重新登记并重试一次）、特权消息的来源检查、
 *  改服务端地址换新令牌、心跳新形状的缓存与快心跳、开始执行前的对账与上报、对账的基准、
 *  执行页面刷新 / 关闭 / 不再答复时接管批次、结果回执的保存与补交、定时（按买家号 ID 分键、带时间点、各返回码）。 */

import { test, mock } from "node:test";
import assert from "node:assert/strict";

import { DEMO_CID, OTHER_CID, createFakeServer } from "./fake-server.mjs";
import { EXT_ORIGIN, EXT_ROOT, installFakeChrome } from "./fake-chrome.mjs";
import { buildCheckoutFacts, successResult, simOrderNo } from "../build/core/simulate.js";
import { parseAttempt } from "../build/core/client.js";
import { describeStartError } from "../build/core/engine.js";
import { pageStartProblem, replyForPage, replyFromNotice, replyFromPrepare } from "../build/core/start.js";
import { startGate } from "../build/core/startgate.js";
import { panelStatus } from "../build/core/status.js";

const AMZ = "https://www.amazon.com/gp/css/order-history";
const URL_A = "http://127.0.0.1:8790";
const CID = DEMO_CID;
const OTHER = OTHER_CID;
const TASKS = () => [
  { upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69" },
  { upstream_order_no: "PO-DEMO-0002", asin: "B0DEMO0002", quantity: 2, unit_price: "12.50", price_cap: "25.00" },
];

/** 内容脚本读到的一份身份（这一页的加载时刻默认一分钟前）。 */
const reading = (cid, loadedAt = Date.now() - 60_000, signedIn = cid !== null) => ({
  signed_in: signedIn, customer_id: cid, site: "US", page_url: AMZ, conflict: false, at: Date.now(), loaded_at: loadedAt,
});

/** boot 的 login 取这个值 = 不开亚马逊页面。 */
const NO_PAGE = "no-page";

/** 启动后台（像刚装好那样）→ 自动登记 → 一个亚马逊标签页（默认 3 号）报上买家号。
 *  login: NO_PAGE = 不开亚马逊页面；null = 未登录；字符串 = 登着这个买家号。 */
async function boot({ server = {}, manual = true, login = CID, tab = 3, manifestVersion = "0.2.0" } = {}) {
  const srv = createFakeServer({ tasks: TASKS(), preRegistered: false, ...server, account: { ...(manual ? { purchase_mode: "manual" } : {}), ...(server.account ?? {}) } });
  const env = installFakeChrome({ fetch: srv.fetch, manifestVersion });
  await env.startWorker();
  await env.fireInstalled("install");
  await env.settle(120);
  if (login !== NO_PAGE) {
    const r = reading(login);
    env.addTab(tab, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: { ...r, at: Date.now() } } : m.type === "pc.ping" ? { running: false, run_id: null } : { ok: true }));
    await env.fromTab(tab, { type: "pc.identity", identity: r, force: false });
    await env.settle();
  }
  return { srv, env };
}

const get = async (areaObj, key) => (await areaObj.get(key))[key];
const paths = (srv, re) => srv.calls.filter((c) => re.test(c.path));
const storedToken = () => globalThis.indexedDB._dbs.get("pc-secrets")?.stores.get("kv").get("device_token");
const storedTokenUrl = () => globalThis.indexedDB._dbs.get("pc-secrets")?.stores.get("kv").get("device_token_url");
const state = (env) => env.fromPage({ type: "pc.getState" });
const view = (runId) => ({ phase: "running", trigger: "manual", run_id: runId, attempt: null, stage: null, stage_label: null, counts: { attempts: 0, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 0 }, started_at: Date.now(), message: null });
const startRun = (env, tabId, args = ["manual", CID]) => env.fromTab(tabId, { type: "pc.api", op: "startRun", args });

/** 像引擎那样经后台走到「已登记提交」：开始批次 → 领取 → 进度 → 护栏 → 登记。 */
async function driveToSubmitted(env, tabId) {
  await env.fromTab(tabId, { type: "pc.lease", busy: true });
  const start = await startRun(env, tabId);
  assert.equal(start.ok, true, JSON.stringify(start));
  const runId = start.data.run_id;
  await env.fromTab(tabId, { type: "pc.runView", view: view(runId), active: true });
  const n = await env.fromTab(tabId, { type: "pc.api", op: "next", args: [runId] });
  const a = parseAttempt(n.data.attempt.raw ?? n.data.attempt);
  const facts = buildCheckoutFacts(a, "pass", Date.parse("2026-11-05T06:00:00Z"), CID);
  await env.fromTab(tabId, { type: "pc.api", op: "progress", args: [a.attempt_id, "started", {}] });
  const g = await env.fromTab(tabId, { type: "pc.api", op: "guard", args: [a.attempt_id, facts] });
  assert.equal(g.data.verdict, "pass");
  const s = await env.fromTab(tabId, { type: "pc.api", op: "submitting", args: [a.attempt_id] });
  assert.equal(s.ok, true);
  return { runId, attempt: a, body: successResult(facts, simOrderNo(a.attempt_id, Date.parse("2026-11-05T06:00:00Z"))) };
}

/** 客户端的断网重试会 setTimeout 1 秒、3 秒：用假定时器推着走。 */
async function settleTicking(env, rounds = 80) {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setImmediate(r));
    mock.timers.tick(1_000);
  }
  await env.settle();
}

/** 发一条消息、一边推假定时器一边等后台答复。 */
async function ticking(env, promise) {
  let done = false;
  const p = promise.finally(() => { done = true; });
  for (let i = 0; i < 400 && !done; i++) {
    await new Promise((r) => setImmediate(r));
    mock.timers.tick(1_000);
  }
  const v = await p;
  await settleTicking(env, 20);
  return v;
}

/** 服务端没起来：所有请求都连不上。返回恢复函数。 */
function serverDown() {
  const orig = globalThis.fetch;
  const hits = [];
  globalThis.fetch = async (url) => { hits.push(new URL(url).pathname); throw new TypeError("fetch failed"); };
  return { hits, up: () => { globalThis.fetch = orig; } };
}

// ── 自动登记 ─────────────────────────────────────────────────────────────

test("装好就自动登记：令牌由插件生成（43 个字符的 base64url），存在扩展源下的 IndexedDB，不进 storage.local；登记不带令牌头；安装后不打开设置页", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  const reg = paths(srv, /\/register$/);
  assert.equal(reg.length, 1, "只登记了一次");
  const token = storedToken();
  assert.match(token, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(storedTokenUrl(), URL_A, "令牌与它所属的服务端地址存在一起");
  assert.deepEqual(reg[0].body, { device_token: token, ext_version: "0.2.0" }, "零设置：没有配对码、设备名称、浏览器类型、工作区 ID");
  assert.equal(reg[0].auth, null, "登记不带 Authorization");
  assert.equal(reg[0].contentType, "application/json");
  assert.equal(reg[0].origin, URL_A, "缺省的服务端地址");
  assert.ok(paths(srv, /\/heartbeat$/).length >= 1);
  assert.ok(paths(srv, /\/heartbeat$/).every((c) => c.auth === `Bearer ${token}`), "其余接口带 Bearer 令牌");
  const rec = await get(env.local, "pc.device");
  assert.deepEqual([rec.device_id, rec.device_name, rec.workspace_id, rec.workspace_name, rec.server_url], [5, "浏览器 5", 1, "工作区-DEMO", URL_A]);
  assert.equal("token" in rec, false);
  assert.equal(JSON.stringify(env.local._all()).includes(token), false, "storage.local 里任何地方都没有令牌（内容脚本读得到那里）");
  assert.equal(JSON.stringify(env.session._all()).includes(token), false);
  assert.equal(env.openedOptions, 0, "安装后不自动打开设置页");
  const st = await state(env);
  assert.deepEqual([st.device.registered, st.device.device_name, st.connection.ok], [true, "浏览器 5", true]);
  assert.equal(st.config.heartbeat.login.state, "unknown", "还没有打开亚马逊页面");
  assert.equal(panelStatus(st).text, "还没有读到登录状态。打开或刷新亚马逊页面后自动读取。");
  assert.ok(env.logs().some((t) => /已在服务端登记：浏览器「浏览器 5」（工作区 工作区-DEMO）/.test(t)));
  assert.ok(!env.logs().some((t) => t.includes(token)), "令牌不写日志");
});

test("登记过了就不再登记：后台被回收后重新起来、浏览器重启、再装一次，都只发心跳；令牌不变", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  const token = storedToken();
  await env.startWorker();
  await env.fireStartup();
  await env.fireInstalled("update");
  await env.fireAlarm("pc.heartbeat");
  await env.settle(120);
  assert.equal(paths(srv, /\/register$/).length, 1);
  assert.equal(storedToken(), token);
  assert.equal(srv.state.devices.length, 1, "服务端没有多出浏览器");
  assert.equal(env.openedOptions, 0);
});

test("几路同时发现还没登记（安装、打开亚马逊页面、面板同步）：只发一个登记请求（单飞）", async () => {
  const srv = createFakeServer({ tasks: TASKS(), preRegistered: false, account: { purchase_mode: "manual" } });
  const env = installFakeChrome({ fetch: srv.fetch });
  await env.startWorker();
  env.addTab(3, AMZ, () => ({ ok: true }));
  await Promise.all([
    env.fireInstalled("install"),
    env.fromTab(3, { type: "pc.hello", href: AMZ, loaded_at: Date.now() }),
    env.fromTab(3, { type: "pc.sync", reason: "打开亚马逊页面", minAgeMs: 0 }),
    env.fromTab(3, { type: "pc.identity", identity: reading(CID), force: false }),
    env.fromPage({ type: "pc.heartbeatNow" }),
  ]);
  await env.settle(150);
  assert.equal(paths(srv, /\/register$/).length, 1);
  assert.equal(srv.state.devices.length, 1);
  assert.equal((await state(env)).config.heartbeat.login.state, "ready");
});

test("服务端晚启动：登记失败按 5 / 15 / 60 秒退避 —— 退避期内心跳闹钟、面板展开不发请求；点「重试」立即再试；连上后自动恢复", async () => {
  const T = Date.parse("2026-11-18T06:00:00Z");
  mock.timers.enable({ apis: ["Date"], now: T });
  let down = null;
  try {
    const srv = createFakeServer({ tasks: TASKS(), preRegistered: false, account: { purchase_mode: "manual" } });
    const env = installFakeChrome({ fetch: srv.fetch });
    down = serverDown();
    await env.startWorker();
    await env.fireInstalled("install");
    await env.settle(120);
    assert.deepEqual(down.hits, ["/plugin/v1/register"], "第一次：登记，没连上（客户端不在里面连发）");
    let reg = await get(env.local, "pc.registration");
    assert.deepEqual([reg.ok, reg.fails, reg.next_at, reg.halted], [false, 1, T + 5_000, false]);
    assert.equal(env.alarms.get("pc.register").scheduledTime, T + 5_000, "到点的闹钟");
    let st = await state(env);
    assert.equal(st.device.registered, false);
    assert.equal(panelStatus(st).text, "连接失败：连不上服务端 http://127.0.0.1:8790。请确认服务端已启动；地址不同时到设置里修改。插件会自动重试。");
    assert.deepEqual(panelStatus(st).actions, ["retry", "open_options"]);

    // 退避期内：心跳闹钟、面板展开（pc.sync）、页面加载（pc.hello）都不发请求
    mock.timers.tick(3_000);
    await env.fireAlarm("pc.heartbeat");
    env.addTab(3, AMZ, () => ({ ok: true }));
    await env.fromTab(3, { type: "pc.sync", reason: "打开亚马逊页面", minAgeMs: 0 });
    await env.fromTab(3, { type: "pc.hello", href: AMZ, loaded_at: T });
    await env.settle(120);
    assert.equal(down.hits.length, 1, "退避没到，不发");

    // 5 秒到：闹钟触发第二次；失败 → 15 秒
    mock.timers.tick(2_000);
    await env.fireAlarm("pc.register");
    await env.settle(120);
    assert.equal(down.hits.length, 2);
    reg = await get(env.local, "pc.registration");
    assert.deepEqual([reg.fails, reg.next_at], [2, T + 5_000 + 15_000]);

    // 点「重试」（面板上的按钮，内容脚本发来）：不等退避
    mock.timers.tick(3_000);
    await env.fromTab(3, { type: "pc.retry" });
    await env.settle(120);
    assert.equal(down.hits.length, 3, "手动重试立即发");
    reg = await get(env.local, "pc.registration");
    assert.deepEqual([reg.fails, reg.next_at], [3, T + 8_000 + 60_000], "第三次起 60 秒");
    // 连点：两次手动重试之间至少隔 2 秒
    await env.fromTab(3, { type: "pc.retry" });
    await env.settle(120);
    assert.equal(down.hits.length, 3, "不能连点成一片");
    assert.equal(env.logs().filter((t) => /^连接失败：连不上服务端/.test(t)).length, 1, "同一个原因只写一行日志，不刷屏");

    // 服务端起来了：下一次到点的自动重试连上
    down.up();
    down = null;
    mock.timers.tick(60_000);
    await env.fireAlarm("pc.heartbeat");
    await env.settle(150);
    assert.equal(paths(srv, /\/register$/).length, 1);
    st = await state(env);
    assert.deepEqual([st.device.registered, st.connection.ok, st.registration.ok, st.registration.fails], [true, true, true, 0]);
    assert.equal(env.alarms.has("pc.register"), false, "登记上了，重试闹钟撤掉");
    assert.equal(srv.state.devices.length, 1, "失败的几次没有在服务端留下任何东西：一直是同一个令牌");
  } finally {
    down?.up();
    mock.timers.reset();
  }
});

test("登记被限速（429）：按服务端给的 retry_after_s 等（没有则 60 秒），等待期间自动与手动都不发", async () => {
  const T = Date.parse("2026-11-18T06:00:00Z");
  mock.timers.enable({ apis: ["Date"], now: T });
  try {
    const { srv, env } = await boot({ login: NO_PAGE, server: { registerRateLimit: { retry_after_s: 42 } } });
    assert.equal(paths(srv, /\/register$/).length, 1);
    let reg = await get(env.local, "pc.registration");
    assert.deepEqual([reg.code, reg.next_at, reg.halted], ["RATE_LIMITED", T + 42_000, false]);
    const st = await state(env);
    assert.equal(panelStatus(st).text, "连接失败：登记的浏览器太多：请稍后再试。插件稍后会自动重试。");
    mock.timers.tick(30_000);
    await env.fireAlarm("pc.heartbeat");
    await env.fromPage({ type: "pc.retry" });
    await env.fromPage({ type: "pc.heartbeatNow" });
    await env.settle(120);
    assert.equal(paths(srv, /\/register$/).length, 1, "42 秒没到：闹钟、「重试」、「立即同步」都不发");
    mock.timers.tick(12_000);
    await env.fireAlarm("pc.register");
    await env.settle(120);
    assert.equal(paths(srv, /\/register$/).length, 2, "到点自动重试");

    // 没给秒数：60 秒
    const b = await boot({ login: NO_PAGE, server: { registerRateLimit: {} } });
    reg = await get(b.env.local, "pc.registration");
    assert.equal(reg.next_at - reg.at, 60_000);
  } finally {
    mock.timers.reset();
  }
});

test("系统里有多个工作区（409 WORKSPACE_REQUIRED）：不自动重试、面板写明原因；设置页填了工作区 ID 保存后马上登记上", async () => {
  const { srv, env } = await boot({ login: NO_PAGE, server: { workspaces: [{ id: 1, name: "工作区-甲" }, { id: 2, name: "工作区-乙" }] } });
  let reg = await get(env.local, "pc.registration");
  assert.deepEqual([reg.code, reg.halted, reg.next_at], ["WORKSPACE_REQUIRED", true, null]);
  assert.equal(env.alarms.has("pc.register"), false, "不排重试闹钟");
  let st = await state(env);
  assert.equal(panelStatus(st).text, "连接失败：系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）。处理后点「重试」。");
  assert.deepEqual(panelStatus(st).actions, ["open_options", "retry"]);
  await env.fireAlarm("pc.heartbeat");
  await env.fireAlarm("pc.heartbeat");
  await env.settle(120);
  assert.equal(paths(srv, /\/register$/).length, 1, "自动的不再试");

  const bad = await env.fromPage({ type: "pc.saveSettings", patch: { workspaceId: "x2" } });
  assert.deepEqual([bad.ok, /工作区 ID 应为正整数/.test(bad.message)], [false, true]);
  const saved = await env.fromPage({ type: "pc.saveSettings", patch: { workspaceId: 2 } });
  await env.settle(120);
  assert.equal(saved.ok, true, saved.message);
  const regs = paths(srv, /\/register$/);
  assert.equal(regs.length, 2);
  assert.equal(regs[1].body.workspace_id, 2);
  assert.equal(regs[1].body.device_token, regs[0].body.device_token, "还没登记过：同一个令牌");
  st = await state(env);
  assert.deepEqual([st.device.registered, st.device.workspace_id, st.device.workspace_name, st.settings.workspaceId], [true, 2, "工作区-乙", 2]);
});

test("插件过旧（409 PLUGIN_OUTDATED）与入口闸的拒绝（403）：不自动重试，面板写明原因", async () => {
  const old = await boot({ login: NO_PAGE, manifestVersion: "0.1.9" });
  assert.equal(paths(old.srv, /\/register$/).length, 1);
  assert.equal(paths(old.srv, /\/register$/)[0].body.ext_version, "0.1.9");
  let reg = await get(old.env.local, "pc.registration");
  assert.deepEqual([reg.code, reg.halted], ["PLUGIN_OUTDATED", true]);
  assert.equal(panelStatus(await state(old.env)).text, "连接失败：插件版本过旧，请重新构建并加载新版插件。");
  await old.env.fireAlarm("pc.heartbeat");
  await old.env.settle(120);
  assert.equal(paths(old.srv, /\/register$/).length, 1, "不自动重试");

  const gated = await boot({ login: NO_PAGE, server: { gate: { code: "FORBIDDEN", message: "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持" } } });
  reg = await get(gated.env.local, "pc.registration");
  assert.deepEqual([reg.code, reg.halted, reg.next_at], ["FORBIDDEN", true, null]);
  assert.equal(panelStatus(await state(gated.env)).text, "连接失败：插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持。处理后点「重试」。");
  await gated.env.fireAlarm("pc.heartbeat");
  await gated.env.settle(120);
  assert.equal(paths(gated.srv, /\/register$/).length, 1);
});

test("密钥库写不进去（IndexedDB 不可用）：不登记 —— 否则每次唤醒都带着一个新令牌去登记，服务端会多出一串浏览器", async () => {
  const srv = createFakeServer({ tasks: TASKS(), preRegistered: false });
  const env = installFakeChrome({ fetch: srv.fetch });
  globalThis.indexedDB = { open() { const req = { onerror: null, onsuccess: null, onupgradeneeded: null, error: new Error("blocked") }; setImmediate(() => req.onerror?.()); return req; } };
  for (let i = 0; i < 3; i++) {
    await env.startWorker();
    await env.fireInstalled("install");
    await env.fireAlarm("pc.heartbeat");
    await env.settle(120);
  }
  assert.equal(srv.calls.length, 0, "一个请求都没发");
  const reg = await get(env.local, "pc.registration");
  assert.deepEqual([reg.code, reg.halted], ["TOKEN_STORE", true]);
  const st = await state(env);
  assert.equal(st.device.registered, false);
  assert.match(panelStatus(st).text, /^连接失败：插件的密钥库写不进去（浏览器存储不可用），请重新加载插件后再试。/);
});

// ── 401：带着同一个令牌重新登记，原请求再发一次 ──────────────────────────

test("服务端不认识这个令牌了（换了库）：心跳回 401 → 用同一个令牌重新登记 → 心跳再发一次成功；本地的登记记录换成新的那一行", async () => {
  const { srv, env } = await boot();
  const token = storedToken();
  srv.forgetDevices();
  const n0 = srv.calls.length;
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  const seq = srv.calls.slice(n0).map((c) => c.path.replace("/plugin/v1/", ""));
  assert.deepEqual(seq.slice(0, 3), ["heartbeat", "register", "heartbeat"], "401 → 登记 → 原请求重试一次");
  const regs = paths(srv, /\/register$/);
  assert.equal(regs.at(-1).body.device_token, token, "同一个令牌");
  assert.equal(storedToken(), token, "令牌没有换");
  const st = await state(env);
  assert.deepEqual([st.device.registered, st.device.device_id, st.connection.ok], [true, 6, true], "服务端给了新的一行");
  assert.ok(env.logs().some((t) => /服务端不认识这个浏览器.*用原来的令牌重新登记/.test(t)));
  assert.ok(env.logs().some((t) => /已在服务端登记：浏览器「浏览器 6」/.test(t)));
});

test("协议请求回 401：重新登记后把原请求再发一次（401 说明服务端没处理它）；之后的请求照常", async () => {
  const { srv, env } = await boot();
  srv.forgetDevices();
  const n0 = srv.calls.length;
  // 开始批次：第一次 401 → 重新登记 → 再发。新的一行还没有报过身份，所以服务端这一次按「没读到登录状态」拒绝 —— 不是 401
  const r = await startRun(env, 3);
  const seq = srv.calls.slice(n0).map((c) => c.path.replace("/plugin/v1/", ""));
  assert.deepEqual(seq, ["runs", "register", "runs"]);
  assert.equal(r.ok, false);
  assert.deepEqual([r.error.status, r.error.code], [403, "IDENTITY_MISMATCH"]);
  // 这一页重新报上身份之后就能开始了
  await env.fromTab(3, { type: "pc.identity", identity: reading(CID), force: true });
  const again = await startRun(env, 3);
  assert.equal(again.ok, true, JSON.stringify(again));
});

test("重新登记之后还是 401（服务端有问题）：30 秒内不再重新登记，不让登记与 401 打转", async () => {
  const { srv, env } = await boot();
  const real = globalThis.fetch;
  const hits = [];
  globalThis.fetch = async (url, init) => {
    const path = new URL(url).pathname;
    hits.push(path.replace("/plugin/v1/", ""));
    if (path.endsWith("/register")) return real(url, init);
    return new Response(JSON.stringify({ code: "UNAUTHORIZED", message: "这个浏览器还没有登记，插件会自动重新登记", detail: {} }), { status: 401 });
  };
  try {
    await env.fireAlarm("pc.heartbeat");
    await env.settle(150);
    assert.deepEqual(hits, ["heartbeat", "register", "heartbeat"], "第一轮：重新登记一次、原请求重试一次");
    for (let i = 0; i < 3; i++) await env.fireAlarm("pc.heartbeat");
    await env.settle(150);
    assert.deepEqual(hits.slice(3), ["heartbeat", "heartbeat", "heartbeat"], "之后每次只发原请求，不再登记");
    const st = await state(env);
    assert.equal(st.connection.ok, false);
    assert.equal(st.connection.code, "UNAUTHORIZED");
    void srv;
  } finally {
    globalThis.fetch = real;
  }
});

test("401 之后重新登记没成功（新库里有多个工作区）：本地的登记记录作废，按「还没登记上」走 —— 停下等人处理，不会每来一个 401 就再登记一次", async () => {
  const { srv, env } = await boot();
  const token = storedToken();
  srv.forgetDevices();
  srv.state.workspaces.push({ id: 2, name: "工作区-乙" });
  const n0 = srv.calls.length;
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.deepEqual(srv.calls.slice(n0).map((c) => c.path.replace("/plugin/v1/", "")), ["heartbeat", "register"], "401 → 重新登记（被拒）→ 原请求不再发");
  let st = await state(env);
  assert.equal(st.device.registered, false);
  assert.deepEqual([st.registration.code, st.registration.halted], ["WORKSPACE_REQUIRED", true]);
  assert.equal(st.connection.code, "WORKSPACE_REQUIRED", "面板上是登记被拒的原因，不是 401");
  assert.match(panelStatus(st).text, /^连接失败：系统里有多个工作区/);
  const n1 = srv.calls.length;
  for (let i = 0; i < 3; i++) await env.fireAlarm("pc.heartbeat");
  await env.fromTab(3, { type: "pc.identity", identity: reading(CID), force: true });
  const r = await startRun(env, 3);
  await env.settle(150);
  assert.equal(srv.calls.length, n1, "停下等人处理：一个请求都不发");
  assert.deepEqual([r.ok, r.error.kind, r.error.code], [false, "network", "NOT_REGISTERED"]);
  assert.match(r.error.message, /^还没有连上服务端：系统里有多个工作区/);
  // 所有者填了工作区 ID：同一个令牌登记进去
  const saved = await env.fromPage({ type: "pc.saveSettings", patch: { workspaceId: 2 } });
  await env.settle(150);
  assert.equal(saved.ok, true);
  assert.equal(storedToken(), token);
  st = await state(env);
  assert.deepEqual([st.device.registered, st.device.workspace_id, st.connection.ok], [true, 2, true]);
});

test("几路请求同时收到 401：只重新登记一次，各自把原请求再发一次", async () => {
  const { srv, env } = await boot();
  srv.forgetDevices();
  const n0 = paths(srv, /\/register$/).length;
  const [, next, prog] = await Promise.all([
    env.fireAlarm("pc.heartbeat"),
    env.fromTab(3, { type: "pc.api", op: "next", args: [99] }),
    env.fromTab(3, { type: "pc.api", op: "progress", args: [99, "started", {}] }),
    env.fromTab(3, { type: "pc.identity", identity: reading(CID), force: true }),
  ]);
  await env.settle(150);
  assert.equal(paths(srv, /\/register$/).length, n0 + 1, "只重新登记了一次");
  assert.equal(srv.state.devices.length, 1);
  assert.notEqual(next.error.status, 401, "重试之后不再是 401（批次不存在是另一回事）");
  assert.notEqual(prog.error.status, 401);
});

// ── 旧版留下的令牌原样沿用 ───────────────────────────────────────────────

const LEGACY = "Zx0_-LEGACYtokenDEMO0000000000000000000000";

test("旧版配对得到的令牌（已在 IndexedDB 里、没有记过所属地址）：原样沿用，不重新登记，补记所属地址", async () => {
  const srv = createFakeServer({ tasks: TASKS(), token: LEGACY, identity: null, account: { purchase_mode: "manual" } });
  const env = installFakeChrome({ fetch: srv.fetch });
  globalThis.indexedDB._dbs.set("pc-secrets", { version: 1, stores: new Map([["kv", new Map([["device_token", LEGACY]])]]) });
  await env.local.set({ "pc.device": { device_id: 5, device_name: "Mac-DEMO", browser: "chrome", workspace_name: "工作区-DEMO", server_url: URL_A, paired_at: 1_700_000_000_000 } });
  await env.startWorker();
  await env.fireInstalled("update");
  await env.settle(120);
  assert.equal(paths(srv, /\/register$/).length, 0, "不重新登记");
  assert.ok(paths(srv, /\/heartbeat$/).length >= 1);
  assert.ok(srv.calls.every((c) => c.auth === `Bearer ${LEGACY}`));
  assert.equal(storedToken(), LEGACY);
  assert.equal(storedTokenUrl(), URL_A);
  const st = await state(env);
  assert.deepEqual([st.device.registered, st.device.device_id, st.device.registered_at, st.connection.ok], [true, 5, 1_700_000_000_000, true]);
  assert.equal(st.device.device_name, "浏览器 5", "名字以服务端的为准（心跳带回来的）");
});

test("更早的版本把令牌写在 storage.local 的登记记录里：后台读到时搬进 IndexedDB 并从记录里删掉，照常使用", async () => {
  const srv = createFakeServer({ tasks: TASKS(), token: LEGACY, identity: null, account: { purchase_mode: "manual" } });
  const env = installFakeChrome({ fetch: srv.fetch });
  await env.local.set({
    "pc.device": { token: LEGACY, device_id: 5, device_name: "Mac-DEMO", browser: "chrome", workspace_name: null, server_url: URL_A, paired_at: Date.now() },
  });
  await env.startWorker();
  const st = await env.fromPage({ type: "pc.heartbeatNow" });
  assert.equal(st.device.registered, true);
  assert.equal(st.connection.ok, true);
  assert.equal("token" in (await get(env.local, "pc.device")), false);
  assert.equal(JSON.stringify(env.local._all()).includes(LEGACY), false);
  assert.equal(storedToken(), LEGACY);
  assert.equal(paths(srv, /\/register$/).length, 0);
});

test("插件更新后留下的旧形状缓存（pc.config 没有 login、pc.schedule 不分买家号）：不拿来用，第一次心跳后换成新的", async () => {
  const srv = createFakeServer({ tasks: TASKS(), token: LEGACY, account: { purchase_mode: "manual" } });
  const env = installFakeChrome({ fetch: srv.fetch });
  globalThis.indexedDB._dbs.set("pc-secrets", { version: 1, stores: new Map([["kv", new Map([["device_token", LEGACY]])]]) });
  await env.local.set({
    "pc.device": { device_id: 5, device_name: "Mac-DEMO", browser: "chrome", workspace_name: null, server_url: URL_A, paired_at: Date.now() },
    "pc.config": { received_at: Date.now(), clock_offset_ms: 0, heartbeat: { device: { id: 5, name: "Mac-DEMO", bound_account_id: 3 }, account: { id: 3, name: "环境-DEMO" }, identity_status: "match", active_run: null } },
    "pc.schedule": { bj_date: "2026-11-05", done: ["14:00"], waiting_logged: [] },
  });
  await env.startWorker();
  let st = await state(env);
  assert.equal(st.config, null, "旧缓存当作没有");
  assert.equal(startGate(st).ok, false);
  assert.equal(panelStatus(st).kind, "connecting");
  await env.fireInstalled("update");
  await env.settle(120);
  st = await state(env);
  assert.equal(st.config.heartbeat.login.state, "ready");
  assert.equal(st.config.heartbeat.account.name, "环境-DEMO");
});

// ── 特权消息只接受扩展自己的页面 ─────────────────────────────────────────

test("保存设置、重新登记、清日志：内容脚本发来的一律拒绝，而且什么都不改；扩展自己的页面发来的才做", async () => {
  const { srv, env } = await boot();
  const before = JSON.stringify(await get(env.local, "pc.settings") ?? null);
  const logs0 = env.logs().length;
  const calls0 = srv.calls.length;
  const token = storedToken();
  const senders = [
    ["内容脚本", (m) => env.fromTab(3, m)],
    ["带着 tab、地址像设置页，但浏览器报的源是网页的", (m) => env.send(3, m, { tab: { id: 3 }, url: EXT_ROOT + "options.html", origin: "https://www.amazon.com" })],
    ["带着 tab、地址像设置页，没有源", (m) => env.send(3, m, { tab: { id: 3 }, url: EXT_ROOT + "options.html" })],
    ["带着 tab 的扩展页面，但不是设置页", (m) => env.send(3, m, { tab: { id: 3 }, url: EXT_ROOT + "popup.html", origin: EXT_ORIGIN })],
    ["没有 tab、地址不是扩展源", (m) => env.send(null, m, { url: "https://www.amazon.com/", origin: "https://www.amazon.com" })],
    ["没有 tab、别的扩展的地址", (m) => env.send(null, m, { url: "chrome-extension://someoneelse0000000000000000000000/options.html" })],
    ["没有 tab、地址在扩展源下，但源对不上", (m) => env.send(null, m, { url: EXT_ROOT + "popup.html", origin: "https://www.amazon.com" })],
    ["什么都没带", (m) => env.send(null, m, {})],
  ];
  for (const [who, send] of senders) {
    for (const msg of [
      { type: "pc.saveSettings", patch: { serverUrl: "http://127.0.0.1:9001" } },
      { type: "pc.saveSettings", patch: { simulationOutcome: "price_over_cap" } },
      { type: "pc.saveSettings", patch: { workspaceId: 2 } },
      { type: "pc.reregister" },
      { type: "pc.clearLogs" },
    ]) {
      const r = await send(msg);
      assert.equal(r.ok, false, `${who} / ${msg.type}`);
      assert.match(r.message, /只能在插件的设置页里做/);
    }
  }
  await env.settle();
  assert.equal(JSON.stringify(await get(env.local, "pc.settings") ?? null), before, "设置没有变");
  assert.equal(srv.calls.length, calls0, "没有触发任何请求（没有重新登记）");
  assert.equal(env.logs().length, logs0, "日志没有被清");
  assert.equal(storedToken(), token);
  assert.equal(storedTokenUrl(), URL_A);

  // 扩展自己的页面：没开在标签页里的（弹出页）……
  assert.equal((await env.fromPage({ type: "pc.saveSettings", patch: { simulationOutcome: "price_over_cap" } })).ok, true);
  assert.equal((await state(env)).settings.simulationOutcome, "price_over_cap");
  // ……以及开在标签页里的设置页（带着 sender.tab，源是扩展的源）
  assert.equal((await env.fromOptionsTab({ type: "pc.saveSettings", patch: { simulationOutcome: "out_of_stock" } })).ok, true);
  assert.equal((await state(env)).settings.simulationOutcome, "out_of_stock");
  const rr = await env.fromOptionsTab({ type: "pc.reregister" });
  assert.deepEqual([rr.ok, rr.message], [true, "已重新登记"]);
  assert.equal(paths(srv, /\/register$/).at(-1).body.device_token, token, "重新登记用的是同一个令牌");
  assert.equal(srv.state.devices.length, 1);
  assert.equal((await env.fromPage({ type: "pc.clearLogs" })).ok, true);
  assert.deepEqual(env.logs(), []);
});

test("不是特权的消息内容脚本照常能发（读状态、同步、重试、打开设置、打开控制台与订单页）", async () => {
  const { env } = await boot();
  assert.equal((await env.fromTab(3, { type: "pc.getState" })).device.registered, true);
  assert.equal((await env.fromTab(3, { type: "pc.retry" })).connection.ok, true);
  assert.equal((await env.fromTab(3, { type: "pc.openOptions" })).ok, true);
  assert.equal(env.openedOptions, 1);
  // 「打开控制台」「打开订单页」：地址由后台定（消息里带了也不看），只有两个固定的去处
  await env.fromTab(3, { type: "pc.openConsole", url: "https://evil.example/" });
  await env.fromTab(3, { type: "pc.openOrders", url: "https://evil.example/" });
  assert.deepEqual(env.createdTabs, ["http://127.0.0.1:8790/accounts", "https://www.amazon.com/gp/css/order-history"]);
});

test("storage.local 里的服务端地址被别处改了（没有经过设置页的保存）：后台仍按令牌所属的地址说话，并把设置改回来", async () => {
  const { srv, env } = await boot();
  const token = storedToken();
  const other = createFakeServer({ preRegistered: false });
  const real = globalThis.fetch;
  globalThis.fetch = (url, init) => (new URL(url).port === "9001" ? other.fetch(url, init) : real(url, init));
  try {
    // 内容脚本读写得到 chrome.storage.local：直接改设置
    await env.local.set({ "pc.settings": { serverUrl: "http://127.0.0.1:9001", simulationOutcome: "pass", workspaceId: null } });
    await env.fireAlarm("pc.heartbeat");
    await env.fromTab(3, { type: "pc.identity", identity: reading(CID), force: true });
    await env.settle(120);
    assert.equal(other.calls.length, 0, "9001 上一个请求都没有");
    assert.equal((await state(env)).settings.serverUrl, URL_A);
    assert.equal((await get(env.local, "pc.settings")).serverUrl, URL_A, "设置被改回来");
    assert.equal(storedToken(), token);
    assert.ok(srv.calls.every((c) => c.origin === URL_A));
  } finally {
    globalThis.fetch = real;
  }
});

// ── 改服务端地址：换一个新令牌再登记 ─────────────────────────────────────

test("改服务端地址（设置页）：换一个新令牌向新地址登记，旧令牌绝不发往新地址；旧地址那一侧的缓存清掉", async () => {
  const { srv, env } = await boot({ manual: false });
  const oldToken = storedToken();
  const srvB = createFakeServer({ tasks: TASKS(), preRegistered: false, account: { purchase_mode: "manual", name: "环境-乙" } });
  srvB.state.nextDeviceId = 41;
  const real = globalThis.fetch;
  globalThis.fetch = (url, init) => (new URL(url).port === "9001" ? srvB.fetch(url, init) : real(url, init));
  try {
    assert.ok(await get(env.local, "pc.config"));
    const callsA = srv.calls.length;
    const bad = await env.fromPage({ type: "pc.saveSettings", patch: { serverUrl: "http://192.168.1.20:8790" } });
    assert.deepEqual([bad.ok, /只支持本机服务端/.test(bad.message)], [false, true]);
    const r = await env.fromPage({ type: "pc.saveSettings", patch: { serverUrl: "http://127.0.0.1:9001/" } });
    await env.settle(150);
    assert.equal(r.ok, true, r.message);
    assert.equal(r.message, "已保存，并已重新登记");
    const newToken = storedToken();
    assert.notEqual(newToken, oldToken, "换了新令牌");
    assert.match(newToken, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(storedTokenUrl(), "http://127.0.0.1:9001");
    assert.ok(srvB.calls.length >= 2);
    assert.equal(JSON.stringify(srvB.calls).includes(oldToken), false, "旧令牌没有出现在发往新地址的任何请求里（头与请求体）");
    assert.equal(paths(srvB, /\/register$/)[0].body.device_token, newToken);
    assert.ok(srvB.calls.filter((c) => !c.path.endsWith("/register")).every((c) => c.auth === `Bearer ${newToken}`));
    assert.equal(srv.calls.length, callsA, "保存之后旧地址不再收到请求");
    const st = await state(env);
    assert.deepEqual([st.settings.serverUrl, st.device.registered, st.device.device_id, st.device.device_name], ["http://127.0.0.1:9001", true, 41, "浏览器 41"]);
    assert.equal(st.config.heartbeat.device.id, 41, "缓存是新服务端的");
    assert.equal([...env.alarms.keys()].some((k) => k.startsWith("pc.sched:")), false, "旧账号的定时闹钟清掉");
    assert.ok(env.logs().some((t) => /服务端地址改为 http:\/\/127\.0\.0\.1:9001：换用新的令牌重新登记/.test(t)));
    assert.ok(!env.logs().some((t) => t.includes(oldToken) || t.includes(newToken)));
  } finally {
    globalThis.fetch = real;
  }
});

test("改服务端地址：新地址连不上也照样换令牌（保存成功，说明连接失败），之后按退避自动重试；旧令牌仍不外发", async () => {
  const { env } = await boot();
  const oldToken = storedToken();
  const real = globalThis.fetch;
  const toNew = [];
  globalThis.fetch = async (url, init) => {
    if (new URL(url).port === "9001") { toNew.push({ auth: init.headers.Authorization ?? null, body: init.body }); throw new TypeError("fetch failed"); }
    return real(url, init);
  };
  try {
    const r = await env.fromPage({ type: "pc.saveSettings", patch: { serverUrl: "http://localhost:9001" } });
    await env.settle(120);
    assert.equal(r.ok, true);
    assert.match(r.message, /^已保存。连接失败：/);
    assert.notEqual(storedToken(), oldToken);
    assert.equal(storedTokenUrl(), "http://localhost:9001");
    assert.ok(toNew.length >= 1);
    assert.equal(JSON.stringify(toNew).includes(oldToken), false);
    const st = await state(env);
    assert.deepEqual([st.device.registered, st.settings.serverUrl], [false, "http://localhost:9001"]);
    assert.match(panelStatus(st).text, /^连接失败：连不上服务端 http:\/\/localhost:9001。/);
    assert.ok(env.alarms.has("pc.register"), "排了重试");
  } finally {
    globalThis.fetch = real;
  }
});

test("改服务端地址的前置条件：正在执行、有结果回执没送到、上一个批次还在收尾时拒绝（令牌与地址都不动）", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await boot();
    const token = storedToken();
    const change = () => env.fromPage({ type: "pc.saveSettings", patch: { serverUrl: "http://127.0.0.1:9001" } });
    // 1. 正在执行（执行页面的引擎还在）
    env.addTab(3, AMZ, (m) => (m.type === "pc.ping" ? { running: true, run_id: 9 } : { ok: true }));
    const { runId, attempt, body } = await driveToSubmitted(env, 3);
    let r = await ticking(env, change());
    assert.deepEqual([r.ok, r.message], [false, "保存失败：正在执行，结束后再修改"]);
    // 2. 引擎因结果送不到而中断：后台接管，回执没送到
    const orig = globalThis.fetch;
    globalThis.fetch = async (url, init) => (/\/result$|\/heartbeat$|\/finish$/.test(new URL(url).pathname) ? Promise.reject(new TypeError("fetch failed")) : orig(url, init));
    env.setTabHandler(3, () => ({ running: false, run_id: null }));
    await ticking(env, env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] }));
    await ticking(env, env.fromTab(3, { type: "pc.runView", view: view(runId), active: false }));
    r = await ticking(env, change());
    assert.equal(r.ok, false);
    assert.match(r.message, /^保存失败：还有 1 条结果回执没送到服务端/);
    // 3. 回执送到了、批次还没结束（结束批次的请求还发不出去）
    globalThis.fetch = async (url, init) => (/\/finish$|\/runs\/\d+\/heartbeat$/.test(new URL(url).pathname) ? Promise.reject(new TypeError("fetch failed")) : orig(url, init));
    const rcs = await get(env.local, "pc.receipts");
    const key = String(attempt.attempt_id);
    await env.local.set({ "pc.receipts": { ...rcs, [key]: { ...rcs[key], next_at: Date.now() - 1 } } });
    await ticking(env, env.fireAlarm("pc.receipts"));
    assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {}, "回执已送达");
    globalThis.fetch = orig;
    assert.equal(storedToken(), token);
    assert.equal(storedTokenUrl(), URL_A);
    assert.equal((await state(env)).settings.serverUrl, URL_A);
    assert.equal(srv.state.attempts.get(attempt.attempt_id).status, "succeeded");
    // 批次收尾完成之后就可以改了
    await ticking(env, env.fireAlarm("pc.runBeat"));
    assert.equal(await get(env.session, "pc.activeRun"), undefined);
    const down = serverDown();
    r = await ticking(env, change());
    down.up();
    assert.equal(r.ok, true, r.message);
    assert.notEqual(storedToken(), token);
  } finally {
    mock.timers.reset();
  }
});

test("换工作区：已经登记在别的工作区时改填工作区 ID → 换新令牌在新的工作区登记（服务端按令牌幂等，不会挪动原来那一行）", async () => {
  const { srv, env } = await boot({ login: NO_PAGE, server: { workspaces: [{ id: 1, name: "工作区-甲" }, { id: 2, name: "工作区-乙" }] } });
  await env.fromPage({ type: "pc.saveSettings", patch: { workspaceId: 1 } });
  await env.settle(120);
  const first = storedToken();
  assert.equal((await state(env)).device.workspace_id, 1);
  const r = await env.fromPage({ type: "pc.saveSettings", patch: { workspaceId: 2 } });
  await env.settle(120);
  assert.equal(r.ok, true, r.message);
  assert.notEqual(storedToken(), first, "换了令牌");
  const st = await state(env);
  assert.deepEqual([st.device.workspace_id, st.device.workspace_name, st.device.device_id], [2, "工作区-乙", 6]);
  assert.equal(srv.state.devices.length, 2);
  // 清空工作区 ID（不再需要）不换令牌
  const tok = storedToken();
  assert.equal((await env.fromPage({ type: "pc.saveSettings", patch: { workspaceId: null } })).ok, true);
  assert.equal(storedToken(), tok);
});

// ── 心跳的新形状、快心跳 ─────────────────────────────────────────────────

test("心跳与身份上报的返回按新形状缓存：登着已登记的买家号 → ready，带账号配置；面板文案照规格", async () => {
  const { srv, env } = await boot({ server: { tasks: [...TASKS(), { upstream_order_no: "PO-DEMO-0003", asin: "B0DEMO0003", unit_price: "20.00", price_cap: "30.00", future: true }] } });
  const st = await state(env);
  const hb = st.config.heartbeat;
  assert.deepEqual(hb.device, { id: 5, name: "浏览器 5" });
  assert.deepEqual([hb.login.state, hb.login.customer_id, hb.login.stale], ["ready", CID, false]);
  assert.deepEqual([hb.account.id, hb.account.name, hb.account.amazon_customer_id, hb.account.executable_today, hb.account.future_assigned], [3, "环境-DEMO", CID, 2, 1]);
  assert.equal(hb.device_run, null);
  assert.equal(panelStatus(st).text, "账号「环境-DEMO」· 今天可执行 2 笔（另有 1 笔建议采购日期在未来）· 额度 已用 0 / 上限 20。");
  assert.equal(startGate(st).ok, true);
  assert.equal(srv.state.identityReports.at(-1).via, "identity", "身份单独上报，返回直接更新缓存（不用再补一次心跳）");
  assert.deepEqual(srv.state.identityReports.at(-1).conflict, false);
  assert.ok(env.logs().some((t) => /当前买家号 A1DE…00：账号「环境-DEMO」/.test(t)), "日志里的买家号 ID 用遮掩形式");
  assert.ok(!env.logs().some((t) => t.includes(CID)), "完整的买家号 ID 不进日志");
});

test("买家号还没有登记（unregistered）：开快心跳（10 秒）；控制台里一登记，下一次快心跳就拿到账号并关掉快心跳", async () => {
  const { srv, env } = await boot({ login: "A2TEST00000" });
  let st = await state(env);
  assert.equal(st.config.heartbeat.login.state, "unregistered");
  assert.equal(st.config.heartbeat.account, null);
  assert.equal(panelStatus(st).text, "买家号 A2TEST00000 还没有登记。在控制台「账号」页的待登记列表里点「登记」，之后这里自动更新。");
  assert.equal(startGate(st).ok, false);
  assert.ok(env.alarms.has("pc.heartbeatFast"), "unregistered 时开快心跳");
  assert.ok(env.alarms.get("pc.heartbeatFast").periodInMinutes <= 10 / 60 + 1e-9, "请求 10 秒一次（打包安装时浏览器最短给 30 秒）");
  assert.ok(env.logs().some((t) => /买家号 A2TE…00 还没有登记/.test(t)));
  // 所有者在控制台把这个买家号登记成账号
  srv.state.accounts.push({ ...srv.state.account, id: 4, name: "环境-乙", amazon_customer_id: "A2TEST00000" });
  await env.fireAlarm("pc.heartbeatFast");
  st = await state(env);
  assert.deepEqual([st.config.heartbeat.login.state, st.config.heartbeat.account.name], ["ready", "环境-乙"]);
  assert.equal(startGate(st).ok, true, "「开始执行」马上可以点");
  assert.equal(env.alarms.has("pc.heartbeatFast"), false, "登记之后关掉快心跳");
});

test("快心跳只在 unregistered 时开，而且自这个状态出现起最多 10 分钟；其余状态（未登录、没读到 ID、已忽略、ready）一律 60 秒", async () => {
  const T = Date.parse("2026-11-18T06:00:00Z");
  mock.timers.enable({ apis: ["Date"], now: T });
  try {
    const { env } = await boot({ login: "A2TEST00000" });
    assert.ok(env.alarms.has("pc.heartbeatFast"));
    assert.equal((await get(env.local, "pc.config")).unregistered_since, T);
    mock.timers.tick(9 * 60_000);
    await env.fireAlarm("pc.heartbeatFast");
    assert.ok(env.alarms.has("pc.heartbeatFast"), "9 分钟：还开着");
    assert.equal((await get(env.local, "pc.config")).unregistered_since, T, "起点不随每次心跳往后推");
    mock.timers.tick(61_000);
    await env.fireAlarm("pc.heartbeatFast");
    assert.equal(env.alarms.has("pc.heartbeatFast"), false, "满 10 分钟：关掉，回到 60 秒一次");
    assert.equal((await state(env)).config.heartbeat.login.state, "unregistered");
  } finally {
    mock.timers.reset();
  }
  for (const [login, server, want] of [
    [null, {}, "signed_out"],
    [CID, {}, "ready"],
    ["A2TEST00000", { ignored: ["A2TEST00000"] }, "ignored"],
    [NO_PAGE, {}, "unknown"],
  ]) {
    const { env } = await boot({ login, server });
    assert.equal((await state(env)).config.heartbeat.login.state, want);
    assert.equal(env.alarms.has("pc.heartbeatFast"), false, want);
  }
  // 已登录但这一页没读到买家号 ID
  const { srv, env } = await boot({ login: NO_PAGE });
  env.addTab(3, AMZ, () => ({ ok: true }));
  await env.fromTab(3, { type: "pc.identity", identity: reading(null, Date.now() - 1_000, true), force: false });
  const st = await state(env);
  assert.equal(st.config.heartbeat.login.state, "no_id");
  assert.equal(panelStatus(st).text, "这一页没有读到买家号 ID。打开订单页后插件会自动读取。");
  assert.equal(env.alarms.has("pc.heartbeatFast"), false);
  assert.deepEqual([srv.state.device.identity.signed_in, srv.state.device.identity.customer_id], [true, null]);
});

test("界面打开 / 回到前台（pc.sync）与页面加载（pc.hello）：上一次心跳早于间隔就马上同步，间隔内不重复", async () => {
  const { srv, env } = await boot({ login: "A2TEST00000" });
  const n0 = srv.count(/\/heartbeat$/);
  srv.state.accounts.push({ ...srv.state.account, id: 4, name: "环境-乙", amazon_customer_id: "A2TEST00000" });
  const st = await env.fromTab(3, { type: "pc.sync", reason: "打开亚马逊页面", minAgeMs: 0 });
  assert.equal(st.config.heartbeat.account.id, 4, "答复里就是登记之后的配置");
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1);
  await env.fromTab(3, { type: "pc.sync", reason: "回到亚马逊页面", minAgeMs: 60_000 });
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1, "间隔内不重复");
  await env.session.set({ "pc.heartbeatTriedAt": Date.now() - 60_000 });
  env.addTab(4, AMZ, () => undefined);
  await env.fromTab(4, { type: "pc.hello", href: AMZ, loaded_at: Date.now() });
  await env.settle();
  assert.equal(srv.count(/\/heartbeat$/), n0 + 2, "页面加载时同步了一次");
});

test("刚在控制台登记完这个买家号、随即打开亚马逊页面：几秒前那次心跳（答复还是「没登记」）不挡这次同步，面板马上有账号", async () => {
  const { srv, env } = await boot({ login: "A2TEST00000" });
  assert.equal((await state(env)).config.heartbeat.login.state, "unregistered");
  srv.state.accounts.push({ ...srv.state.account, id: 4, name: "环境-乙", amazon_customer_id: "A2TEST00000" });
  await env.session.set({ "pc.heartbeatTriedAt": Date.now() - 2_000 }); // 2 秒前刚发过一次（登记之前）
  env.addTab(4, AMZ, () => undefined);
  await env.fromTab(4, { type: "pc.hello", href: AMZ, loaded_at: Date.now() });
  await env.settle();
  const st = await state(env);
  assert.deepEqual([st.config.heartbeat.login.state, st.config.heartbeat.account.id], ["ready", 4]);
  assert.equal(startGate(st).ok, true);
  // 已登记之后恢复正常的节流：5 秒内的页面加载不再各发一次心跳
  const n0 = srv.count(/\/heartbeat$/);
  await env.session.set({ "pc.heartbeatTriedAt": Date.now() - 2_000 });
  await env.fromTab(4, { type: "pc.hello", href: AMZ, loaded_at: Date.now() + 1 });
  await env.settle();
  assert.equal(srv.count(/\/heartbeat$/), n0);
});

test("「重新读取」：哪一页都没读到身份（没有东西可报）也同步一次心跳；有可报的结论就报它（返回即最新状态）；控制台改了浏览器的名字，本地跟着改", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  env.addTab(3, AMZ, () => ({ ok: true }));
  const n0 = srv.count(/\/heartbeat$/);
  const r0 = await env.fromTab(3, { type: "pc.identity", identity: null, force: true });
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1, "没有东西可报：拉一次心跳");
  assert.deepEqual(r0, { login_state: "unknown", conflict: false });
  // 有一份新鲜的结论：强制重报它，不用再补心跳
  await env.fromTab(3, { type: "pc.identity", identity: reading(CID), force: false });
  const id0 = srv.count(/\/identity$/);
  const r1 = await env.fromTab(3, { type: "pc.identity", identity: null, force: true });
  assert.equal(srv.count(/\/identity$/), id0 + 1);
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1);
  assert.deepEqual(r1, { login_state: "ready", conflict: false });
  srv.state.device.name = "办公室-环境 1";
  await env.fireAlarm("pc.heartbeat");
  const st = await state(env);
  assert.equal(st.device.device_name, "办公室-环境 1");
  assert.equal((await get(env.local, "pc.device")).device_name, "办公室-环境 1");
});

// ── 开始执行：当前页重读 → 对账 → 上报 → 申请批次 ─────────────────────────

test("开始执行前（pc.prepareStart）：记下发起页的读数、其余标签页重读、对账、POST /identity；之后申请批次带当前页读到的买家号 ID", async () => {
  const { srv, env } = await boot();
  const other = reading(CID, Date.now() - 30_000);
  env.addTab(4, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: other } : { ok: true }));
  const id0 = srv.state.identityReports.filter((r) => r.via === "identity").length;
  const hb0 = srv.count(/\/heartbeat$/);
  const sent0 = env.sentToTabs.length;
  const prep = await env.fromTab(3, { type: "pc.prepareStart", identity: reading(CID) });
  assert.deepEqual(prep, { ok: true, conflict: false, customer_id: CID, login_state: "ready", code: null, message: null });
  const asked = env.sentToTabs.slice(sent0).filter((x) => x.msg.type === "pc.readIdentity").map((x) => x.tabId);
  assert.deepEqual(asked, [4], "发起的那一页自己已经读过了，只问其余标签页");
  assert.equal(srv.state.identityReports.filter((r) => r.via === "identity").length, id0 + 1, "先 POST /identity 上报");
  assert.equal(srv.count(/\/heartbeat$/), hb0, "不另发心跳：身份上报的返回就是最新状态");
  const n0 = srv.calls.length;
  const r = await startRun(env, 3);
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.deepEqual(srv.calls[n0].body, { customer_id: CID, trigger: "manual", simulated: true });
  assert.deepEqual(r.data.account, { id: 3, name: "环境-DEMO" });
  const ar = await get(env.session, "pc.activeRun");
  assert.deepEqual([ar.run_id, ar.customer_id, ar.account_name, ar.tab_id], [9, CID, "环境-DEMO", 3], "记下批次的买家号 ID（对账的基准）");
  const st = await state(env);
  assert.deepEqual(st.active_run, { run_id: 9, account_name: "环境-DEMO", started_at: ar.started_at, orphaned: false });
});

test("各标签页显示的买家号不一致：不开始；带 conflict 的结论照样报给服务端（服务端登录状态变 conflict）；直接申请批次也被本地拒绝，不发请求", async () => {
  const { srv, env } = await boot();
  env.addTab(4, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: reading(OTHER, Date.now() - 5_000) } : { ok: true }));
  const prep = await env.fromTab(3, { type: "pc.prepareStart", identity: reading(CID) });
  assert.equal(prep.ok, false);
  assert.equal(prep.conflict, true);
  assert.equal(prep.code, "IDENTITY_CONFLICT");
  assert.equal(prep.message, "开始执行失败：这个浏览器的亚马逊标签页显示的买家号不一致。请刷新或关闭较早打开的亚马逊标签页。");
  assert.equal(prep.login_state, "conflict");
  const last = srv.state.identityReports.at(-1);
  assert.deepEqual([last.via, last.conflict, last.customer_id], ["identity", true, OTHER], "上报体带 conflict: true；没有批次在跑，报加载最新的强证据");
  assert.equal(srv.loginState(), "conflict", "服务端不解析账号");
  const st = await state(env);
  assert.equal(st.config.heartbeat.account, null);
  assert.equal(panelStatus(st).text, "这个浏览器的亚马逊标签页显示的买家号不一致。请刷新或关闭较早打开的亚马逊标签页。");
  assert.deepEqual(panelStatus(st).actions, ["reread"]);
  assert.equal(startGate(st).ok, false);
  const n0 = srv.calls.length;
  const r = await startRun(env, 3);
  assert.deepEqual([r.ok, r.error.code], [false, "IDENTITY_CONFLICT"]);
  assert.equal(srv.calls.length, n0, "没有发 /runs");
  assert.ok(env.logs().some((t) => /亚马逊标签页显示的买家号不一致（其中一页：买家号 ID A9OT…00）/.test(t)));
  // 较早打开的那一页刷新之后（浏览器其实登着 OTHER）：分歧消除，状态跟着变
  env.setTabHandler(3, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: reading(OTHER, Date.now() + 1) } : { ok: true }));
  const again = await env.fromTab(3, { type: "pc.refreshIdentity" });
  assert.deepEqual(again, { reported: true, conflict: false, login_state: "unregistered" });
});

test("申请批次：买家号 ID 形状不对、不是后台对账出的那一个、这个浏览器没登录 → 本地拒绝，不发请求", async () => {
  const { srv, env } = await boot();
  const n0 = srv.calls.length;
  for (const [args, code] of [
    [["manual"], "NO_CUSTOMER_ID"],
    [["manual", "not-an-id"], "NO_CUSTOMER_ID"],
    [["manual", OTHER], "IDENTITY_MISMATCH"],
    [["scheduled", CID], "VALIDATION"],
    [["scheduled", CID, "25:00"], "VALIDATION"],
  ]) {
    const r = await startRun(env, 3, args);
    assert.deepEqual([r.ok, r.error.code, r.error.status], [false, code, null], JSON.stringify(args));
  }
  assert.equal(srv.calls.length, n0, "一个请求都没发");
  await env.fromTab(3, { type: "pc.identity", identity: reading(null), force: true });
  const out = await startRun(env, 3);
  assert.equal(out.error.code, "SIGNED_OUT");
  assert.equal(paths(srv, /\/runs$/).length, 0);
});

test("本地还有进行中的批次时不发请求：执行页面的引擎还在 → 本地答 DEVICE_BUSY；后台在替它收尾（回执保管期）也一样", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await boot();
    env.addTab(3, AMZ, (m) => (m.type === "pc.ping" ? { running: true, run_id: 9 } : m.type === "pc.readIdentity" ? { ok: true, identity: reading(CID) } : { ok: true }));
    const { runId, attempt, body } = await driveToSubmitted(env, 3);
    env.addTab(4, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: reading(CID) } : { ok: true }));
    const runs0 = paths(srv, /\/runs$/).length;
    const prep = await env.fromTab(4, { type: "pc.prepareStart", identity: reading(CID) });
    assert.deepEqual([prep.ok, prep.code, prep.message], [false, "DEVICE_BUSY", "开始执行失败：这个浏览器上一个批次还没有结束。"]);
    const r = await startRun(env, 4);
    assert.deepEqual([r.ok, r.error.code, r.error.status], [false, "DEVICE_BUSY", null]);
    assert.deepEqual([r.error.detail.run_id, r.error.detail.account_name], [runId, "环境-DEMO"]);
    assert.equal(paths(srv, /\/runs$/).length, runs0, "没有发 /runs");
    // 引擎因结果送不到中断 → 后台接管（回执保管期）
    const orig = globalThis.fetch;
    globalThis.fetch = async (url, init) => (/\/result$|\/heartbeat$|\/finish$/.test(new URL(url).pathname) ? Promise.reject(new TypeError("fetch failed")) : orig(url, init));
    env.setTabHandler(3, () => ({ running: false, run_id: null }));
    await ticking(env, env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] }));
    await ticking(env, env.fromTab(3, { type: "pc.runView", view: view(runId), active: false }));
    const st = await state(env);
    assert.equal(st.active_run.orphaned, true);
    assert.equal(startGate(st).ok, false, "面板上「开始执行」不能点");
    const r2 = await ticking(env, startRun(env, 4));
    assert.equal(r2.error.code, "DEVICE_BUSY");
    assert.equal(paths(srv, /\/runs$/).length, runs0);
    globalThis.fetch = orig;
  } finally {
    mock.timers.reset();
  }
});

test("服务端说这个浏览器上一个批次还没结束（409 DEVICE_BUSY，例如刷新前的批次租约还没到期）：原样交给引擎说明", async () => {
  const { srv, env } = await boot();
  srv.state.runs.set(1, { id: 1, account_id: 3, device_id: 5, started_at: srv.now() - 60_000, lease_until: srv.now() + 120_000, ended: false, stop: false });
  const r = await startRun(env, 3);
  assert.deepEqual([r.error.status, r.error.code, r.error.detail.run_id, r.error.detail.account_name], [409, "DEVICE_BUSY", 1, "环境-DEMO"]);
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  await env.fireAlarm("pc.heartbeat");
  const st = await state(env);
  assert.deepEqual(st.config.heartbeat.device_run, { id: 1, account_id: 3, account_name: "环境-DEMO", started_at: new Date(srv.now() - 60_000).toISOString() });
  assert.equal(startGate(st).ok, true, "服务端说有批次而本地没有：不禁用，点了看服务端的答复");
});

// ── 对账的基准：进行中批次的买家号 ID ────────────────────────────────────

test("没有批次在跑：两张标签页两个买家号 → 报加载最新的那个、带 conflict，反复重读也不来回翻；较新的页面关掉后仍是证据；旧页面刷新后恢复", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  const now = Date.now();
  let tab1 = reading(CID, now - 3_600_000);
  env.addTab(1, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: tab1 } : { ok: true }));
  env.addTab(2, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: reading(OTHER, now) } : { ok: true }));
  await env.fromTab(1, { type: "pc.identity", identity: tab1, force: false });
  assert.equal(srv.state.device.identity.customer_id, CID);
  await env.fromTab(2, { type: "pc.identity", identity: reading(OTHER, now), force: false });
  assert.deepEqual([srv.state.device.identity.customer_id, srv.state.device.identity.conflict], [OTHER, true], "新加载的页面显示另一个买家号");
  for (let i = 0; i < 4; i++) {
    await env.fromTab(1, { type: "pc.identity", identity: { ...tab1, at: Date.now() }, force: true });
    await env.fromTab(2, { type: "pc.identity", identity: reading(OTHER, now), force: true });
    assert.equal(srv.state.device.identity.customer_id, OTHER, "旧页面（加载时刻没变）反复强制重报，上报不翻回去");
  }
  const reported = srv.state.identityReports.filter((r) => r.conflict).map((r) => r.customer_id);
  assert.ok(reported.length >= 8 && reported.every((c) => c === OTHER), "有分歧的每一次上报都是同一个买家号");
  assert.equal(srv.loginState(), "conflict");
  assert.equal((await state(env)).identity.conflict, true);

  // 另一个标签页关掉之后，它仍是最新加载的证据
  await env.removeTab(2);
  assert.equal((await state(env)).identity.customer_id, OTHER);
  // 旧标签页刷新：浏览器其实已经换回原来的买家号
  tab1 = reading(CID, Date.now() + 1);
  await env.fromTab(1, { type: "pc.identity", identity: tab1, force: true });
  const st2 = await state(env);
  assert.deepEqual([st2.identity.customer_id, st2.identity.conflict], [CID, false]);
  assert.deepEqual([srv.state.device.identity.customer_id, srv.state.device.identity.conflict], [CID, false]);
  assert.equal(st2.config.heartbeat.login.state, "ready");
});

test("有批次在跑（基准 = 批次的买家号）：执行中另一张标签页显示别的买家号 → 上报绝不是批次的买家号，带 conflict；服务端据此让批次停下", async () => {
  const { srv, env } = await boot();
  const start = await startRun(env, 3);
  assert.equal(start.ok, true);
  // 执行中：较早加载的一张旧标签页还停在另一个买家号（它的读数比执行页面旧）
  env.addTab(4, AMZ, () => ({ ok: true }));
  await env.fromTab(4, { type: "pc.identity", identity: reading(OTHER, Date.now() - 7_200_000), force: false });
  const last = srv.state.identityReports.at(-1);
  assert.deepEqual([last.customer_id, last.conflict], [OTHER, true], "加载最新的是批次的买家号，但有基准时绝不报它");
  const beat = await env.fromTab(3, { type: "pc.api", op: "runHeartbeat", args: [start.data.run_id] });
  assert.deepEqual([beat.data.stop, beat.data.stop_reason], [true, "identity_mismatch"]);
  // 对照：批次结束之后（没有基准），同样的读数报加载最新的强证据
  await env.fromTab(3, { type: "pc.api", op: "finishRun", args: [start.data.run_id, "stopped"] });
  await env.fromTab(4, { type: "pc.identity", identity: reading(OTHER, Date.now() - 7_200_000), force: true });
  const after = srv.state.identityReports.at(-1);
  assert.deepEqual([after.customer_id, after.conflict], [CID, true]);
});

test("显示另一个买家号的标签页跳到一张读不出 ID 的新页面（bye → hello → 身份）：另一个买家号仍是证据，开始前对账有分歧", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  const now = Date.now();
  const tab1 = reading(CID, now - 3_600_000);
  let tab2 = reading(OTHER, now - 60_000);
  env.addTab(1, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: tab1 } : { ok: true }));
  env.addTab(2, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: tab2 } : { ok: true }));
  await env.fromTab(1, { type: "pc.identity", identity: tab1, force: false });
  await env.fromTab(2, { type: "pc.identity", identity: tab2, force: false });
  assert.equal(srv.state.device.identity.customer_id, OTHER);
  const n0 = srv.state.identityReports.length;
  // 标签页 2 跳到新页面：旧页面 bye，新页面 hello，新页面读到「已登录」但读不出 customerId
  await env.fromTab(2, { type: "pc.bye", loaded_at: tab2.loaded_at, run_id: null });
  await env.settle();
  const loaded = Date.now();
  tab2 = reading(null, loaded, true);
  await env.fromTab(2, { type: "pc.hello", href: AMZ, loaded_at: loaded });
  await env.settle();
  await env.fromTab(2, { type: "pc.identity", identity: tab2, force: false });
  const st = await state(env);
  assert.deepEqual([st.identity.conflict, st.identity.customer_id], [true, OTHER]);
  const prep = await env.fromTab(1, { type: "pc.prepareStart", identity: tab1 });
  assert.deepEqual([prep.ok, prep.conflict], [false, true]);
  const later = srv.state.identityReports.slice(n0);
  assert.ok(later.length > 0, "开始前强制上报过");
  assert.ok(later.every((r) => r.conflict === true), "之后的每一次上报都带着 conflict");
  assert.equal(srv.loginState(), "conflict");
});

test("身份读数：标签页跳到别的网站（不在亚马逊页面上了）就不再算开着的", async () => {
  const { env } = await boot({ login: NO_PAGE });
  const now = Date.now();
  env.addTab(1, AMZ, () => ({ ok: true }));
  env.addTab(2, AMZ, () => ({ ok: true }));
  await env.fromTab(1, { type: "pc.identity", identity: reading(CID, now), force: false });
  await env.fromTab(2, { type: "pc.identity", identity: reading(OTHER, now - 10_000), force: false });
  assert.equal((await state(env)).identity.conflict, true);
  env.tabs.get(2).url = "https://www.example.com/";
  await env.fromTab(1, { type: "pc.identity", identity: reading(CID, now), force: false });
  const st = await state(env);
  assert.equal(st.identity.conflict, false, "跳走的那一页比现在开着的旧，作废");
  assert.equal(st.identity.customer_id, CID);
});

test("内容脚本送来的买家号 ID 形状不对：当作没读到（已登录、没有 ID），不原样报给服务端", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  env.addTab(1, AMZ, () => ({ ok: true }));
  await env.fromTab(1, { type: "pc.identity", identity: { ...reading("not-an-id"), customer_id: "<script>" }, force: false });
  assert.deepEqual([srv.state.device.identity.signed_in, srv.state.device.identity.customer_id], [true, null]);
  assert.equal((await state(env)).config.heartbeat.login.state, "no_id");
});

test("执行中重报身份（pc.refreshIdentity）：每个亚马逊标签页重读、强制上报 POST /identity，不发心跳", async () => {
  const { srv, env } = await boot();
  const now = Date.now();
  const r1 = reading(CID, now - 60_000);
  env.addTab(1, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: r1 } : { ok: true }));
  env.addTab(2, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: { ...r1, loaded_at: now - 30_000 } } : { ok: true }));
  // 刚报过同一份（60 秒去重之内）也照发：这是给提交登记用的
  await env.fromTab(1, { type: "pc.identity", identity: r1, force: false });
  const hb0 = srv.count(/\/heartbeat$/);
  const id0 = srv.state.identityReports.filter((r) => r.via === "identity").length;
  const r = await env.fromTab(1, { type: "pc.refreshIdentity" });
  assert.deepEqual(r, { reported: true, conflict: false, login_state: "ready" });
  assert.equal(srv.state.identityReports.filter((x) => x.via === "identity").length, id0 + 1);
  assert.equal(srv.state.identityReports.at(-1).customer_id, CID);
  assert.equal(srv.count(/\/heartbeat$/), hb0, "不发心跳");
  assert.equal(env.sentToTabs.filter((m) => m.msg.type === "pc.readIdentity").length >= 3, true, "每个标签页都重读了");
});

test("身份结论已经过时（几分钟前读的、这一轮哪个标签页都没读到）：不当成刚读到的再报给服务端", async () => {
  const { srv, env } = await boot({ login: NO_PAGE });
  const old = Date.now() - 6 * 60_000;
  env.addTab(1, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: null } : { ok: true }));
  await env.session.set({ "pc.identityObs": { live: { 1: { tab_id: 1, signed_in: true, customer_id: CID, page_url: AMZ, loaded_at: old - 60_000, read_at: old } }, newest_gone: null } });
  const n0 = srv.state.identityReports.length;
  const r = await env.fromTab(1, { type: "pc.refreshIdentity" });
  assert.equal(r.reported, false);
  assert.equal(srv.state.identityReports.length, n0, "服务端没有收到一份「新」身份");
});

// ── 执行页面没了（D6-01）────────────────────────────────────────────────

test("执行页面刷新（同一标签页来了新页面的 hello）：后台接管、立即结束批次、清掉「正在执行」、放开租约、写日志", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: false, run_id: null }));
  await env.fromTab(3, { type: "pc.lease", busy: true });
  const start = await startRun(env, 3);
  const runId = start.data.run_id;
  await env.fromTab(3, { type: "pc.runView", view: view(runId), active: true });
  await env.fromTab(3, { type: "pc.api", op: "next", args: [runId] });
  let st = await state(env);
  assert.equal(startGate(st).ok, false, "执行中");

  await env.fromTab(3, { type: "pc.hello", href: AMZ, loaded_at: Date.now() + 1_000 });
  await env.settle();
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  assert.equal(await get(env.session, "pc.tabLease"), undefined, "标签页租约放开");
  assert.equal((await get(env.local, "pc.runView")).active, false);
  const run = srv.state.runs.get(runId);
  assert.equal(run.ended, true, "立即通知服务端结束批次，不等租约到期");
  assert.deepEqual([run.end_reason, run.finish_detail], ["finished", "page_reloaded"]);
  assert.equal(srv.state.attempts.get(7).status, "aborted", "未登记提交的在途尝试中止");
  assert.equal(srv.state.tasks[0].status, "assigned");
  assert.ok(env.logs().some((t) => /执行页面已刷新或跳转：批次 #9 中断/.test(t)));
  await env.fromTab(3, { type: "pc.identity", identity: reading(CID, Date.now()), force: true });
  st = await state(env);
  assert.equal(startGate(st).ok, true, "「开始执行」恢复可点");
  assert.equal(st.active_run, null);
});

test("开始批次那一页自己的 hello（加载时刻早于批次开始）迟到了：不当成刷新", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const start = await startRun(env, 3);
  await env.fromTab(3, { type: "pc.hello", href: AMZ, loaded_at: Date.now() - 60_000 });
  await env.settle();
  assert.equal((await get(env.session, "pc.activeRun")).run_id, start.data.run_id);
  assert.equal(srv.state.runs.get(start.data.run_id).ended, false);
});

test("执行页面离开（pagehide → pc.bye，带着它正在跑的批次）：后台接管并结束批次", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const start = await startRun(env, 3);
  await env.fromTab(3, { type: "pc.bye", loaded_at: Date.now() - 60_000, run_id: start.data.run_id });
  await env.settle();
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  assert.equal(srv.state.runs.get(start.data.run_id).finish_detail, "page_left");
});

test("批次闹钟：执行页面对 ping 说没在跑（或收不到）→ 接管并结束；还在跑 → 照常兜底续租；服务端说执行权没了 → 本地也清掉", async () => {
  const { srv, env } = await boot();
  let reply = { running: true, run_id: 9 };
  env.setTabHandler(3, (m) => (m.type === "pc.ping" ? reply : { ok: true }));
  const start = await startRun(env, 3);
  const runId = start.data.run_id;
  const beats0 = srv.state.runs.get(runId).beats;
  await env.fireAlarm("pc.runBeat");
  assert.equal(srv.state.runs.get(runId).beats, beats0 + 1, "引擎还在：兜底续租");

  reply = { running: false, run_id: null };
  await env.fireAlarm("pc.runBeat");
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  assert.equal(srv.state.runs.get(runId).finish_detail, "engine_gone");
  assert.equal(srv.state.runs.get(runId).beats, beats0 + 1, "不替已经没了的引擎续租");

  // 服务端已经收回执行权：本地不再认，并告诉页面停下
  reply = { running: true, run_id: 10 };
  const s2 = await startRun(env, 3);
  srv.state.runs.get(s2.data.run_id).ended = true;
  await env.fireAlarm("pc.runBeat");
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  assert.ok(env.sentToTabs.some((x) => x.msg.type === "pc.stopRun"));
});

test("执行中的标签页被关掉：立即通知服务端结束批次", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const start = await startRun(env, 3);
  await env.removeTab(3);
  assert.equal(srv.state.runs.get(start.data.run_id).finish_detail, "tab_closed");
  assert.ok(env.logs().some((t) => /执行中的标签页已关闭：批次 #9 中断/.test(t)));
});

test("插件更新（storage.session 清空）后留下的旧「正在执行」：onInstalled 清掉", async () => {
  const { env } = await boot();
  await env.local.set({ "pc.runView": { active: true, tab_id: 3, view: null, updated_at: Date.now() } });
  env.clearSession();
  await env.startWorker();
  await env.fireInstalled("update");
  assert.equal((await get(env.local, "pc.runView")).active, false);
  assert.equal(env.openedOptions, 0);
});

// ── 定时：按买家号 ID 分键，带时间点，去重的权威在服务端 ──────────────────

const AT_1359 = Date.parse("2026-11-05T05:59:00Z"); // 北京 13:59
const AT_1400 = Date.parse("2026-11-05T06:00:05Z"); // 北京 14:00:05

/** 像内容脚本里的执行宿主（content/runner.ts）那样答复定时触发，用的是它用的那几张对照表（core/start.ts）：
 *  当前页重读身份 → 这一页开始不了就答 next_tab → 交后台对账、上报 → 申请批次 → 把开始那一步的结论翻成答复。
 *  cid 为 null = 已登录，但这一页读不到买家号 ID。 */
function runnerTab(env, tabId, cid, seen = []) {
  // 这一页的加载时刻固定（同一页重读，加载时刻不变）。
  const loaded = Date.now() - 60_000 + tabId;
  const page = () => reading(cid, loaded, true);
  return async (m) => {
    if (m.type === "pc.readIdentity") return { ok: true, identity: page() };
    if (m.type === "pc.ping") return { running: false, run_id: null };
    if (m.type !== "pc.startScheduled") return { ok: true };
    seen.push(m);
    const problem = pageStartProblem(page(), m.customer_id);
    if (problem) return replyForPage(problem, true);
    const refused = replyFromPrepare(await env.fromTab(tabId, { type: "pc.prepareStart", identity: page() }), cid);
    if (refused) return refused;
    const r = await env.fromTab(tabId, { type: "pc.api", op: "startRun", args: ["scheduled", cid, m.slot] });
    if (r.ok) return replyFromNotice({ started: true, run_id: r.data.run_id, status: null, error: null, message: `执行批次 #${r.data.run_id} 已开始`, busy_mine: false });
    // 引擎开始失败时的那一句（core/engine.describeStartError）
    const d = describeStartError(r.error, 5);
    const status = r.error.code === "ACCOUNT_BUSY" || r.error.code === "DEVICE_BUSY" ? "busy" : "rejected";
    return replyFromNotice({ started: false, run_id: null, status, error: r.error, message: d.message, busy_mine: d.mine });
  };
}

const schedBook = async (env) => (await get(env.local, "pc.schedule")) ?? {};

test("定时到点：请登着这个买家号的标签页开始，消息带时间点与买家号 ID；POST /runs 带 trigger=scheduled 与 slot；当天记录按买家号 ID 记", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1359 } });
  const seen = [];
  env.setTabHandler(3, runnerTab(env, 3, CID, seen));
  assert.ok(env.alarms.has("pc.sched:14:00"), "按账号的执行时间排了闹钟");
  srv.setNow(AT_1400);
  await env.fireAlarm("pc.sched:14:00");
  await env.settle(150);
  assert.deepEqual(seen.map((m) => [m.slot, m.slots, m.customer_id]), [["14:00", ["14:00"], CID]]);
  const run = paths(srv, /\/runs$/).at(-1);
  assert.deepEqual(run.body, { customer_id: CID, trigger: "scheduled", slot: "14:00", simulated: true });
  assert.equal(srv.state.runs.get(9).scheduled_slot, "14:00");
  const book = await schedBook(env);
  assert.deepEqual(Object.keys(book), [CID], "按买家号 ID 分键");
  assert.deepEqual(book[CID].done, ["14:00"]);
  assert.ok(env.logs().some((t) => /定时 14:00：执行批次 #9 已开始/.test(t)));
  assert.deepEqual((await state(env)).schedule.done, ["14:00"]);
});

test("定时闹钟：先同步心跳再判断 —— 缓存说暂停、而服务端刚恢复，这一次照常开始", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1359, account: { execution_paused: true, pause_reason: "人工检查" } } });
  assert.equal((await state(env)).config.heartbeat.account.execution_paused, true);
  env.setTabHandler(3, runnerTab(env, 3, CID));
  // 所有者在闹钟响之前一两秒恢复了执行；本地缓存还是暂停，时钟已过 14:00
  srv.state.account.execution_paused = false;
  srv.setNow(AT_1400);
  const hb0 = srv.count(/\/heartbeat$/);
  await env.fireAlarm("pc.sched:14:00");
  await env.settle(150);
  assert.ok(srv.count(/\/heartbeat$/) >= hb0 + 1, "到点先发心跳");
  assert.equal(srv.state.runs.size, 1, "开始了定时批次");
  assert.ok(!env.logs().some((t) => /账号已暂停执行，今天这次不执行/.test(t)));
});

test("定时到点、账号仍是暂停：今天这次不执行，记为处理过（不发 /runs）", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400, account: { execution_paused: true, pause_reason: "人工检查" } } });
  env.setTabHandler(3, runnerTab(env, 3, CID));
  await env.fireAlarm("pc.sched:14:00");
  await env.settle(150);
  assert.equal(paths(srv, /\/runs$/).length, 0);
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"]);
  assert.ok(env.logs().some((t) => /定时 14:00：账号已暂停执行，今天这次不执行/.test(t)));
});

test("这个时间点今天已经执行过（409 SLOT_DONE，例如另一个登着同一买家号的浏览器准点跑过了）：记为处理过，不再问", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400 } });
  env.setTabHandler(3, runnerTab(env, 3, CID));
  // 另一个浏览器的定时批次已经为 14:00 跑完
  srv.state.runs.set(1, { id: 1, account_id: 3, device_id: 77, trigger: "scheduled", scheduled_slot: "14:00", bj_date: "2026-11-05", started_at: srv.now() - 5_000, lease_until: srv.now(), ended: true, end_reason: "finished" });
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.equal(paths(srv, /\/runs$/).length, 1);
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"], "SLOT_DONE → 记已处理");
  assert.ok(env.logs().some((t) => /定时 14:00：开始执行失败：这个时间点今天已经执行过/.test(t)));
  await env.fireAlarm("pc.heartbeat");
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.equal(paths(srv, /\/runs$/).length, 1, "之后不再问");
});

test("这个买家号正在别的浏览器里执行（ACCOUNT_BUSY）或本浏览器上一个批次还没结束（DEVICE_BUSY）：不记，下一分钟再问；日志只写一次", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400, foreignRun: { device_id: 13, device_name: "浏览器 13", since: "2026-11-05T05:58:00Z" } } });
  env.setTabHandler(3, runnerTab(env, 3, CID));
  for (let i = 0; i < 3; i++) {
    await env.fireAlarm("pc.heartbeat");
    await env.settle(150);
  }
  assert.equal(paths(srv, /\/runs$/).length, 3, "每分钟都再问一次");
  assert.deepEqual((await schedBook(env))[CID].done, [], "ACCOUNT_BUSY 不算处理过");
  assert.equal(env.logs().filter((t) => /定时 14:00：暂未开始/.test(t)).length, 1, "同一个时间点只写一次，不刷屏");
  assert.ok(env.logs().some((t) => /定时 14:00：暂未开始（开始执行失败：这个买家号正在浏览器「浏览器 13」里执行（13:58 开始）。），稍后自动重试/.test(t)), env.logs().join(" | "));
  // 那个浏览器跑完了：下一分钟这边开始（服务端放行）
  srv.state.foreignRun = null;
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"]);
  assert.equal(srv.state.runs.get(9).trigger, "scheduled");

  // DEVICE_BUSY：服务端说这个浏览器上一个批次还没结束（本地没有记录，例如刷新前的批次租约还没到期）
  const b = await boot({ manual: false, server: { now: AT_1400 } });
  b.env.setTabHandler(3, runnerTab(b.env, 3, CID));
  b.srv.state.runs.set(1, { id: 1, account_id: 3, device_id: 5, trigger: "manual", started_at: b.srv.now() - 60_000, lease_until: b.srv.now() + 120_000, ended: false, stop: false });
  await b.env.fireAlarm("pc.heartbeat");
  await b.env.settle(150);
  assert.deepEqual((await schedBook(b.env))[CID].done, [], "DEVICE_BUSY 不算处理过");
  assert.equal(paths(b.srv, /\/runs$/).at(-1).body.slot, "14:00");
  b.srv.state.runs.get(1).ended = true;
  await b.env.fireAlarm("pc.heartbeat");
  await b.env.settle(150);
  assert.deepEqual((await schedBook(b.env))[CID].done, ["14:00"]);
});

test("本地记录说这个浏览器还在执行：引擎还在 → 不发请求、不记、等它结束；引擎已经没了 → 先结束旧批次再开始定时批次", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1359 } });
  let ping = { running: true, run_id: 9 };
  const inner = runnerTab(env, 3, CID);
  env.setTabHandler(3, (m) => (m.type === "pc.ping" ? ping : inner(m)));
  const start = await startRun(env, 3);
  srv.setNow(AT_1400);
  await env.fireAlarm("pc.sched:14:00");
  await env.settle(150);
  assert.equal(paths(srv, /\/runs$/).length, 1, "引擎还在：不发 /runs");
  assert.deepEqual((await schedBook(env))[CID]?.done ?? [], [], "不算处理过（不再「并入当前批次」）");
  assert.ok(env.logs().some((t) => /定时 14:00：暂未开始（这个浏览器上一个批次还没有结束）/.test(t)));
  ping = { running: false, run_id: null }; // 页面刷新过，新页面里没有引擎
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.equal(srv.state.runs.get(start.data.run_id).ended, true, "旧批次已结束");
  assert.equal(srv.state.runs.get(10).trigger, "scheduled", "开始了定时批次");
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"]);
});

test("服务端说时间点不对（422：账号刚改成手动、时间点删了）：刷新配置，这个时间点记为处理过", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400 } });
  env.setTabHandler(3, runnerTab(env, 3, CID));
  // 缓存里还是 14:00；所有者刚把执行时间改成 18:00。让「到点先同步」的那次心跳失败一次，后台只能按旧缓存判断
  srv.state.account.schedule_times = ["18:00"];
  const real = globalThis.fetch;
  let drop = 1;
  globalThis.fetch = async (url, init) => (new URL(url).pathname.endsWith("/heartbeat") && drop-- > 0
    ? new Response(JSON.stringify({ code: "INTERNAL", message: "服务器内部错误", detail: {} }), { status: 500 })
    : real(url, init));
  try {
    await env.fromTab(3, { type: "pc.hello", href: AMZ, loaded_at: Date.now() - 120_000 });
    await env.settle(200);
  } finally {
    globalThis.fetch = real;
  }
  const runs = paths(srv, /\/runs$/);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].body.slot, "14:00");
  assert.equal(srv.state.runs.size, 0, "服务端 422，没有开始");
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"], "记为处理过：今天这个时间点不再触发");
  assert.ok(env.logs().some((t) => /定时 14:00：开始执行失败：时间点 14:00 不在账号 环境-DEMO 的执行时间里（已刷新配置，今天这个时间点不再触发）/.test(t)));
  assert.deepEqual((await state(env)).config.heartbeat.account.schedule_times, ["18:00"], "配置已刷新");
  assert.ok(env.alarms.has("pc.sched:18:00"));
  assert.equal(env.alarms.has("pc.sched:14:00"), false);
});

test("没说上话、登录状态对不上：不记，稍后再问", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400 } });
  env.setTabHandler(3, async (m) => (m.type === "pc.startScheduled" ? { result: "network", code: "NETWORK", message: "开始执行失败：无法连接服务端" } : m.type === "pc.ping" ? { running: false, run_id: null } : { ok: true }));
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.deepEqual((await schedBook(env))[CID].done, []);
  assert.ok(env.logs().some((t) => /定时 14:00：暂未开始（开始执行失败：无法连接服务端），稍后自动重试/.test(t)));
  env.setTabHandler(3, async (m) => (m.type === "pc.startScheduled" ? { result: "identity", code: "IDENTITY_CONFLICT", message: "开始执行失败：这个浏览器的亚马逊标签页显示的买家号不一致。" } : { ok: true }));
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.deepEqual((await schedBook(env))[CID].done, []);
  void srv;
});

test("到点时第一个标签页登的不是这个买家号（答 next_tab）：换下一个标签页；一个能用的都没有 → 只记一次「等待环境」，不算处理过", async () => {
  const { srv, env } = await boot({ manual: false, login: NO_PAGE, server: { now: AT_1400 } });
  const seenA = [];
  const seenB = [];
  // 3 号标签页最近用过、排在前面，但它这一页读不到买家号 ID；7 号标签页登着这个买家号
  env.addTab(3, AMZ, runnerTab(env, 3, null, seenA), { active: true });
  env.addTab(7, AMZ, runnerTab(env, 7, CID, seenB));
  await env.fromTab(7, { type: "pc.refreshIdentity" });
  assert.equal((await state(env)).config.heartbeat.login.state, "ready", "读不到 ID 的那一页是弱证据，不制造分歧");
  await env.fireAlarm("pc.heartbeat");
  await env.settle(200);
  assert.equal(seenA.length, 1, "先问了 3 号");
  assert.equal(seenB.length, 1, "3 号答「换下一个」，再问 7 号");
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"]);
  assert.equal(srv.state.runs.get(9).trigger, "scheduled");

  // 没有一个能用的标签页：登着这个买家号的那一页（3 号）现在答「这一页开始不了」
  const b = await boot({ manual: false, server: { now: AT_1400 } });
  b.env.setTabHandler(3, runnerTab(b.env, 3, null));
  for (let i = 0; i < 3; i++) await b.env.fireAlarm("pc.heartbeat");
  await b.env.settle(150);
  const day = (await schedBook(b.env))[CID];
  assert.deepEqual([day.done, day.waiting_logged], [[], ["14:00"]]);
  assert.equal(b.env.logs().filter((t) => /定时 14:00：已到点：打开的亚马逊页面登的不是这个买家号/.test(t)).length, 1);
  assert.equal(paths(b.srv, /\/runs$/).length, 0);

  // 没有打开的亚马逊页面
  const c = await boot({ manual: false, server: { now: AT_1400 } });
  c.env.tabs.clear();
  await c.env.fireAlarm("pc.heartbeat");
  await c.env.settle(150);
  assert.ok(c.env.logs().some((t) => /定时 14:00：已到点：没有打开的亚马逊页面，等待环境/.test(t)));
  assert.deepEqual((await schedBook(c.env))[CID].done, []);
});

test("同一个浏览器当天换了买家号：上一个账号处理过的时间点不算到新账号头上；定时消息带的是新账号的买家号 ID", async () => {
  const B = "A2TEST00000";
  const accountB = { id: 4, name: "环境-乙", amazon_customer_id: B, min_amount: "20.00", max_amount: "50.00", daily_limit: 20, max_delivery_days: 10, expected_card_last4: null, purchase_enabled: true, execution_paused: false, pause_reason: null, archived: false, purchase_mode: "scheduled", schedule_times: ["14:00"] };
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400, accounts: [accountB], tasks: [...TASKS(), { upstream_order_no: "PO-DEMO-0009", asin: "B0DEMO0009", unit_price: "30.00", price_cap: "40.00", account_id: 4 }] } });
  // 上午登着 A：跑了账号 A 的 14:00
  const seen = [];
  env.setTabHandler(3, runnerTab(env, 3, CID, seen));
  await env.fireAlarm("pc.heartbeat");
  await env.settle(200);
  assert.deepEqual((await schedBook(env))[CID].done, ["14:00"]);
  await env.fromTab(3, { type: "pc.api", op: "finishRun", args: [9, "completed"] });
  // 换登 B（这一页刷新后显示 B）：账号 B 也是 14:00
  env.setTabHandler(3, runnerTab(env, 3, B, seen));
  await env.fromTab(3, { type: "pc.identity", identity: reading(B, Date.now() + 1), force: true });
  let st = await state(env);
  assert.deepEqual([st.config.heartbeat.login.state, st.config.heartbeat.account.name], ["ready", "环境-乙"]);
  assert.equal(st.schedule.done.length, 0, "界面上看到的是 B 今天的记录");
  await env.fireAlarm("pc.heartbeat");
  await env.settle(200);
  assert.deepEqual(seen.map((m) => m.customer_id), [CID, B], "第二次带的是 B 的买家号 ID");
  const book = await schedBook(env);
  assert.deepEqual([book[CID].done, book[B].done], [["14:00"], ["14:00"]], "两个买家号各记各的");
  const runs = paths(srv, /\/runs$/).map((c) => [c.body.customer_id, c.body.slot]);
  assert.deepEqual(runs, [[CID, "14:00"], [B, "14:00"]]);
  assert.deepEqual([...srv.state.runs.values()].map((r) => r.account_id), [3, 4]);
});

test("今天错过了几个时间点：合成一次，向服务端申请最晚到期的那个；几个都记为处理过", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: Date.parse("2026-11-05T07:20:00Z"), account: { schedule_times: ["09:00", "14:00", "18:00"] } } });
  env.setTabHandler(3, runnerTab(env, 3, CID));
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  assert.deepEqual(paths(srv, /\/runs$/).map((c) => c.body.slot), ["14:00"]);
  assert.deepEqual((await schedBook(env))[CID].done, ["09:00", "14:00"]);
});

// ── 结果回执（D2-06 / D6-03）────────────────────────────────────────────

test("结果送不到（服务端重启中）：后台发之前先存回执；后台重启之后补交同一份结果，任务已采购，不是结果待核实", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await boot();
    env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
    const { attempt, body } = await driveToSubmitted(env, 3);
    const faults = { match: /\/result$/, times: 3 };
    // 用一次性的故障：客户端三次都没说上话
    const orig = globalThis.fetch;
    globalThis.fetch = async (url, init) => (faults.match.test(new URL(url).pathname) && faults.times-- > 0 ? Promise.reject(new TypeError("fetch failed")) : orig(url, init));
    const r = await ticking(env, env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] }));
    assert.equal(r.ok, false);
    assert.equal(r.error.kind, "network");
    const saved = await get(env.local, "pc.receipts");
    assert.deepEqual(saved[String(attempt.attempt_id)].body, body, "回执里是同一份结果");
    let st = await state(env);
    assert.equal(st.receipts.length, 1, "界面上看得到待补交的回执");

    await env.startWorker(); // 后台被回收后重新起来
    await env.local.set({ "pc.receipts": { [attempt.attempt_id]: { ...saved[String(attempt.attempt_id)], next_at: Date.now() - 1 } } });
    await env.fireAlarm("pc.receipts");
    await settleTicking(env, 10);
    assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {}, "送达后删掉");
    assert.equal(srv.state.attempts.get(attempt.attempt_id).status, "succeeded");
    assert.equal(srv.state.tasks[0].status, "purchased");
    assert.equal(srv.state.account.execution_paused, false);
    assert.ok(env.logs().some((t) => /结果回执已补交/.test(t)));
    st = await state(env);
    assert.equal(st.receipts.length, 0);
    globalThis.fetch = orig;
  } finally {
    mock.timers.reset();
  }
});

test("引擎因结果送不到而中断：后台接管批次 —— 续租、补交，送达之后才结束批次；结束不会把成功判成结果待核实", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await boot();
    env.setTabHandler(3, () => ({ running: false, run_id: null }));
    const { runId, attempt, body } = await driveToSubmitted(env, 3);
    const orig = globalThis.fetch;
    let down = true;
    globalThis.fetch = async (url, init) => (down && /\/result$|\/heartbeat$/.test(new URL(url).pathname) ? Promise.reject(new TypeError("fetch failed")) : orig(url, init));
    await ticking(env, env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] }));
    // 引擎在租约预算内没送到，中断：告诉后台它不跑了
    await ticking(env, env.fromTab(3, { type: "pc.runView", view: { ...view(runId), phase: "ended" }, active: false }));
    const ar = await get(env.session, "pc.activeRun");
    assert.ok(ar && ar.orphaned_at !== null, "后台接管，没有清掉批次");
    assert.equal(srv.state.runs.get(runId).ended, false, "回执没送到之前不结束批次");
    assert.equal((await get(env.local, "pc.runView")).active, false, "界面上不再显示正在执行");

    const key = String(attempt.attempt_id);
    assert.equal((await get(env.local, "pc.receipts"))[key].tries, 1, "接管时立即补交过一次（没送到）");

    down = false; // 服务端恢复
    const beats0 = srv.state.runs.get(runId).beats;
    // 补交按退避：下一次补交的时间还没到，这一次批次闹钟只续租
    await env.fireAlarm("pc.runBeat");
    await settleTicking(env, 10);
    assert.equal(srv.state.runs.get(runId).beats, beats0 + 1, "续租");
    assert.equal(srv.state.runs.get(runId).ended, false, "回执没送到之前不结束批次");
    assert.equal(paths(srv, /\/result$/).length, 0, "退避没到不补交");
    // 退避时间到了
    const rcs = await get(env.local, "pc.receipts");
    await env.local.set({ "pc.receipts": { ...rcs, [key]: { ...rcs[key], next_at: Date.now() - 1 } } });
    await env.fireAlarm("pc.runBeat");
    await settleTicking(env, 10);
    assert.equal(srv.state.attempts.get(attempt.attempt_id).status, "succeeded");
    assert.equal(srv.state.runs.get(runId).ended, true, "送达之后结束批次");
    assert.equal(srv.state.runs.get(runId).finish_detail, "engine_ended");
    assert.equal(srv.state.account.execution_paused, false, "没有被判成结果待核实");
    assert.equal(await get(env.session, "pc.activeRun"), undefined);
    assert.ok(srv.state.runs.get(runId).beats >= beats0);
    globalThis.fetch = orig;
  } finally {
    mock.timers.reset();
  }
});

test("接管的批次在租约预算内一直送不到：放手（不再续租），回执保留", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await boot();
    env.setTabHandler(3, () => ({ running: false, run_id: null }));
    const { runId, attempt, body } = await driveToSubmitted(env, 3);
    const orig = globalThis.fetch;
    globalThis.fetch = async (url, init) => (/\/result$|\/heartbeat$|\/finish$/.test(new URL(url).pathname) ? Promise.reject(new TypeError("fetch failed")) : orig(url, init));
    await ticking(env, env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] }));
    await ticking(env, env.fromTab(3, { type: "pc.runView", view: view(runId), active: false }));
    const ar = await get(env.session, "pc.activeRun");
    await env.session.set({ "pc.activeRun": { ...ar, last_beat_ok_at: Date.now() - 200_000 } });
    await env.fireAlarm("pc.runBeat");
    await settleTicking(env);
    assert.equal(await get(env.session, "pc.activeRun"), undefined, "放手");
    assert.equal(Object.keys(await get(env.local, "pc.receipts")).length, 1, "回执保留");
    assert.ok(env.logs().some((t) => /不再续租/.test(t)));
    globalThis.fetch = orig;
    void srv;
  } finally {
    mock.timers.reset();
  }
});

test("接管的批次：续租一直成功、结果接口一直 5xx —— 接管满 10 分钟就放手（不再续租、不替它结束批次），回执保留；补交按退避，不是每 30 秒都发", async () => {
  const T = Date.parse("2026-11-05T06:00:00Z");
  mock.timers.enable({ apis: ["setTimeout", "Date"], now: T });
  let orig = null;
  try {
    const { srv, env } = await boot();
    orig = globalThis.fetch; // 假服务端（boot 装上的）
    env.setTabHandler(3, () => ({ running: false, run_id: null }));
    const { runId, attempt, body } = await driveToSubmitted(env, 3);
    let posts = 0;
    const fetch5xx = async (url, init) => {
      if (/\/result$/.test(new URL(url).pathname)) {
        posts++;
        return new Response(JSON.stringify({ code: "INTERNAL", message: "服务器内部错误", detail: {} }), { status: 500, headers: { "content-type": "application/json" } });
      }
      return orig(url, init);
    };
    globalThis.fetch = fetch5xx;
    await ticking(env, env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] }));
    await ticking(env, env.fromTab(3, { type: "pc.runView", view: { ...view(runId), phase: "ended" }, active: false }));
    const ar0 = await get(env.session, "pc.activeRun");
    assert.ok(ar0 && ar0.orphaned_at !== null, "后台接管");
    const postsAtOrphan = posts;
    const beats0 = srv.state.runs.get(runId).beats;

    // 批次闹钟每 30 秒一次（假时钟）；服务端的批次心跳一直成功
    let k = 0;
    for (; k < 60 && (await get(env.session, "pc.activeRun")); k++) {
      const fired = env.fireAlarm("pc.runBeat");
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setImmediate(r));
        mock.timers.tick(1_000);
      }
      await fired;
      await env.settle();
    }
    const elapsed = Date.now() - ar0.orphaned_at;
    assert.equal(await get(env.session, "pc.activeRun"), undefined, "放手：不再续租");
    assert.ok(elapsed > 10 * 60_000 && elapsed < 13 * 60_000, `接管满 10 分钟后不久放手（实际 ${Math.round(elapsed / 1000)} 秒）`);
    assert.ok(srv.state.runs.get(runId).beats > beats0, "放手之前照常续租");
    assert.equal(srv.state.runs.get(runId).ended, false, "不替它结束批次：已登记的尝试交给服务端清扫");
    assert.equal(Object.keys(await get(env.local, "pc.receipts")).length, 1, "回执保留，之后继续补交");
    const custodyPosts = posts - postsAtOrphan;
    // 每轮补交客户端对 5xx 最多发 3 次；按 30 / 60 / 120 / 300 秒退避，10 分钟里只有 4 – 5 轮，
    // 而不是每次批次闹钟（20 次）都发
    assert.ok(k >= 20, `批次闹钟响了 ${k} 次`);
    assert.ok(custodyPosts >= 6, `接管期间确实在补交（${custodyPosts} 次）`);
    assert.ok(custodyPosts <= 3 * 6, `补交按退避（${custodyPosts} 次 /result，${k} 次批次闹钟）`);
    assert.ok(env.logs().some((t) => /10 分钟内没有送到服务端.*不再续租/.test(t)), env.logs().slice(-3).join(" | "));

    // 放手之后这个浏览器不再占着批次：之后的批次闹钟什么都不发
    const callsAfter = srv.calls.length;
    await ticking(env, env.fireAlarm("pc.runBeat"));
    assert.equal(paths({ calls: srv.calls.slice(callsAfter) }, /\/runs\/\d+\/heartbeat$/).length, 0);
  } finally {
    if (orig) globalThis.fetch = orig;
    mock.timers.reset();
  }
});

/** 把一份「后台已经补交过、正在退避」的回执直接放进存储。 */
async function heldReceipt(env, attempt, runId, body, extra = {}) {
  const now = Date.now();
  await env.local.set({
    "pc.receipts": { [attempt.attempt_id]: { attempt_id: attempt.attempt_id, run_id: runId, body, created_at: now - 120_000, tries: 2, next_at: now + 240_000, last_error: "无法连接服务端", ...extra } },
  });
}

test("回执补交：后台（重新）启动时不等退避，立即补交同一份结果", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const { runId, attempt, body } = await driveToSubmitted(env, 3);
  await heldReceipt(env, attempt, runId, body);
  await env.startWorker();
  await env.settle();
  assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {}, "送达后删掉");
  assert.equal(srv.state.attempts.get(attempt.attempt_id).status, "succeeded");
  assert.equal(paths(srv, /\/submitting$/).length, 1, "登记从不补发");
});

test("回执补交：每一次心跳成功都不等退避补交；引擎自己还在送的（没被后台补交过、宽限未到）不抢着发", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const { runId, attempt, body } = await driveToSubmitted(env, 3);
  await heldReceipt(env, attempt, runId, body, { tries: 0, next_at: Date.now() + 25_000, created_at: Date.now() - 5_000 });
  await env.fireAlarm("pc.heartbeat");
  await env.settle();
  assert.equal(Object.keys(await get(env.local, "pc.receipts")).length, 1, "引擎那一路还在送：心跳不抢");
  assert.equal(paths(srv, /\/result$/).length, 0);
  await heldReceipt(env, attempt, runId, body);
  await env.fireAlarm("pc.heartbeat");
  await env.settle();
  assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {});
  assert.equal(srv.state.attempts.get(attempt.attempt_id).status, "succeeded");
  assert.ok(env.logs().some((t) => /结果回执已补交/.test(t)));
});

test("回执补交：服务端说同一份已收过（200 duplicate）算送达；尝试已是结果待核实、这份不同（409 迟到回执）也算送达，都不再发", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const { runId, attempt, body } = await driveToSubmitted(env, 3);
  const first = await env.fromTab(3, { type: "pc.api", op: "result", args: [attempt.attempt_id, body] });
  assert.equal(first.ok, true);
  await heldReceipt(env, attempt, runId, body);
  await env.fireAlarm("pc.heartbeat");
  await env.settle();
  assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {}, "duplicate：送达");
  // 另一笔：服务端已判为结果待核实，迟到的成功回执被记下
  const a2 = srv.state.attempts.get(attempt.attempt_id);
  a2.status = "unknown";
  a2.amazon_order_no = null;
  await heldReceipt(env, attempt, runId, body);
  await env.fireAlarm("pc.heartbeat");
  await env.settle();
  assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {}, "迟到回执已记下：送达");
  assert.equal(srv.state.lateReceipts.length, 1);
  assert.ok(env.logs().some((t) => /作为迟到回执记下/.test(t)));
});

test("回执与 401：服务端暂时不认识这个浏览器、重新登记又没成功时，回执留着不丢；之后重新登记上了再补交", async () => {
  const { srv, env } = await boot();
  env.setTabHandler(3, () => ({ running: true, run_id: 9 }));
  const { runId, attempt, body } = await driveToSubmitted(env, 3);
  await heldReceipt(env, attempt, runId, body, { next_at: Date.now() - 1 });
  const real = globalThis.fetch;
  // 结果接口回 401，而重新登记连不上
  globalThis.fetch = async (url, init) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/register")) throw new TypeError("fetch failed");
    if (path.endsWith("/result")) return new Response(JSON.stringify({ code: "UNAUTHORIZED", message: "这个浏览器还没有登记，插件会自动重新登记", detail: {} }), { status: 401 });
    return real(url, init);
  };
  try {
    await env.fireAlarm("pc.receipts");
    await env.settle(150);
    const kept = await get(env.local, "pc.receipts");
    assert.equal(Object.keys(kept).length, 1, "401 不删回执");
    assert.equal(kept[String(attempt.attempt_id)].tries, 3, "记了一次补交失败，按退避再来");
  } finally {
    globalThis.fetch = real;
  }
  // 服务端恢复：同一份回执补交成功
  await env.session.set({ "pc.reregisteredAt": 0 });
  const rcs = await get(env.local, "pc.receipts");
  const key = String(attempt.attempt_id);
  await env.local.set({ "pc.receipts": { [key]: { ...rcs[key], next_at: Date.now() - 1 } } });
  await env.fromPage({ type: "pc.reregister" });
  await env.fireAlarm("pc.receipts");
  await env.settle(150);
  assert.deepEqual((await get(env.local, "pc.receipts")) ?? {}, {});
  assert.equal(srv.state.attempts.get(attempt.attempt_id).status, "succeeded");
});

test("模拟结局的日志写中文名，不写代码", async () => {
  const { env } = await boot();
  await env.fromPage({ type: "pc.saveSettings", patch: { simulationOutcome: "price_over_cap" } });
  assert.ok(env.logs().includes("模拟结局改为：结算总额超过限价"));
});

test("后台的日志里不出现「配对」「绑定」「设备」", async () => {
  const { srv, env } = await boot({ manual: false, server: { now: AT_1400 } });
  env.setTabHandler(3, runnerTab(env, 3, CID));
  await env.fireAlarm("pc.heartbeat");
  await env.settle(150);
  srv.forgetDevices();
  await env.fireAlarm("pc.heartbeat");
  await env.fromTab(3, { type: "pc.identity", identity: reading(OTHER, Date.now() + 5), force: true });
  await env.settle(150);
  assert.ok(env.logs().length >= 5);
  for (const t of env.logs()) assert.doesNotMatch(t, /配对|绑定|设备/, t);
});
