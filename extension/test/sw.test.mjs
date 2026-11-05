/** 后台（真正的 build/background/service-worker.js）对着假 chrome 环境与内存假服务端跑：
 *  绑定后立即同步、执行页面刷新 / 关闭 / 不再答复时接管批次、结果回执的保存与补交、
 *  定时到点先拿新配置、身份按页面加载先后对账、设备令牌不放 storage.local。 */

import { test, mock } from "node:test";
import assert from "node:assert/strict";

import { createFakeServer } from "./fake-server.mjs";
import { installFakeChrome } from "./fake-chrome.mjs";
import { buildCheckoutFacts, successResult, simOrderNo } from "../build/core/simulate.js";
import { parseAttempt } from "../build/core/client.js";
import { startGate } from "../build/core/startgate.js";

const AMZ = "https://www.amazon.com/gp/css/order-history";
const BOUND = "A1DEMO0000";
const OTHER = "A9OTHER0000";
const TASKS = () => [
  { upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 1, unit_price: "41.97", price_cap: "78.69" },
  { upstream_order_no: "PO-DEMO-0002", asin: "B0DEMO0002", quantity: 2, unit_price: "12.50", price_cap: "25.00" },
];

async function paired({ server = {}, bound = true, manual = true } = {}) {
  const srv = createFakeServer({ tasks: TASKS(), ...server, account: { ...(manual ? { purchase_mode: "manual" } : {}), ...(server.account ?? {}) } });
  if (!bound) srv.state.device.bound_account_id = null;
  const env = installFakeChrome({ fetch: srv.fetch });
  await env.startWorker();
  const r = await env.fromPage({ type: "pc.pair", serverUrl: "http://127.0.0.1:8790", code: "DEMO2345", deviceName: "Mac-DEMO", browser: "chrome" });
  assert.equal(r.ok, true, r.message);
  await env.settle();
  return { srv, env };
}

const get = async (areaObj, key) => (await areaObj.get(key))[key];
const view = (runId) => ({ phase: "running", trigger: "manual", run_id: runId, attempt: null, stage: null, stage_label: null, counts: { attempts: 0, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 0 }, started_at: Date.now(), message: null });

/** 像引擎那样经后台走到「已登记提交」：开始批次 → 领取 → 进度 → 护栏 → 登记。 */
async function driveToSubmitted(env, tabId) {
  await env.fromTab(tabId, { type: "pc.lease", busy: true });
  const start = await env.fromTab(tabId, { type: "pc.api", op: "startRun", args: ["manual"] });
  assert.equal(start.ok, true, JSON.stringify(start));
  const runId = start.data.run_id;
  await env.fromTab(tabId, { type: "pc.runView", view: view(runId), active: true });
  const n = await env.fromTab(tabId, { type: "pc.api", op: "next", args: [runId] });
  const a = parseAttempt(n.data.attempt.raw ?? n.data.attempt);
  const facts = buildCheckoutFacts(a, "pass", Date.parse("2026-11-05T06:00:00Z"));
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

// ── 绑定之后立即同步（K20）──────────────────────────────────────────────

test("已配对还没绑定账号：开快心跳；控制台一绑定，下一次快心跳就拿到账号并关掉快心跳", async () => {
  const { srv, env } = await paired({ bound: false });
  assert.ok(env.alarms.has("pc.heartbeatFast"), "没绑定时开快心跳");
  assert.ok(env.alarms.get("pc.heartbeatFast").periodInMinutes <= 10 / 60 + 1e-9, "请求 10 秒一次（打包安装时浏览器最短给 30 秒）");
  let st = await env.fromPage({ type: "pc.getState" });
  assert.equal(st.config.heartbeat.account, null);
  assert.equal(startGate(st).ok, false);
  srv.state.device.bound_account_id = 3;
  await env.fireAlarm("pc.heartbeatFast");
  st = await env.fromPage({ type: "pc.getState" });
  assert.equal(st.config.heartbeat.account.id, 3);
  assert.equal(startGate(st).ok, true, "「开始执行」马上可以点");
  assert.equal(env.alarms.has("pc.heartbeatFast"), false, "绑定之后关掉快心跳");
  assert.ok(env.logs().some((t) => /绑定账号：环境-DEMO/.test(t)));
});

test("界面打开 / 回到前台（pc.sync）与页面加载（pc.hello）：上一次心跳早于间隔就马上同步，间隔内不重复", async () => {
  const { srv, env } = await paired({ bound: false });
  const n0 = srv.count(/\/heartbeat$/);
  srv.state.device.bound_account_id = 3;
  const st = await env.fromTab(3, { type: "pc.sync", reason: "打开亚马逊页面", minAgeMs: 0 });
  assert.equal(st.config.heartbeat.account.id, 3, "答复里就是绑定之后的配置");
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1);
  await env.fromTab(3, { type: "pc.sync", reason: "回到亚马逊页面", minAgeMs: 60_000 });
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1, "间隔内不重复");
  await env.session.set({ "pc.heartbeatTriedAt": Date.now() - 60_000 });
  env.addTab(4, AMZ, () => undefined);
  await env.fromTab(4, { type: "pc.hello", href: AMZ, loaded_at: Date.now() });
  await env.settle();
  assert.equal(srv.count(/\/heartbeat$/), n0 + 2, "页面加载时同步了一次");
});

test("刚配对完（缓存说没绑定）就在控制台绑定、随即打开亚马逊页面：几秒前那次心跳不挡这次同步，面板马上有账号", async () => {
  const { srv, env } = await paired({ bound: false });
  srv.state.device.bound_account_id = 3;
  await env.session.set({ "pc.heartbeatTriedAt": Date.now() - 100 }); // 0.1 秒前刚发过一次（配对时，绑定之前）
  env.addTab(4, AMZ, () => undefined);
  await env.fromTab(4, { type: "pc.hello", href: AMZ, loaded_at: Date.now() });
  await env.settle();
  const st = await env.fromPage({ type: "pc.getState" });
  assert.equal(st.config.heartbeat.account.id, 3);
  assert.equal(startGate(st).ok, true);
});

test("「刷新身份」：这一页没读到身份也同步一次心跳", async () => {
  const { srv, env } = await paired();
  const n0 = srv.count(/\/heartbeat$/);
  await env.fromTab(3, { type: "pc.identity", identity: null, force: true });
  assert.equal(srv.count(/\/heartbeat$/), n0 + 1);
});

// ── 执行页面没了（D6-01）────────────────────────────────────────────────

test("执行页面刷新（同一标签页来了新页面的 hello）：后台接管、立即结束批次、清掉「正在执行」、放开租约、写日志", async () => {
  const { srv, env } = await paired();
  env.addTab(3, AMZ, () => ({ running: false, run_id: null }));
  await env.fromTab(3, { type: "pc.lease", busy: true });
  const start = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
  const runId = start.data.run_id;
  await env.fromTab(3, { type: "pc.runView", view: view(runId), active: true });
  await env.fromTab(3, { type: "pc.api", op: "next", args: [runId] });
  let st = await env.fromPage({ type: "pc.getState" });
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
  st = await env.fromPage({ type: "pc.getState" });
  assert.equal(startGate(st).ok, true, "「开始执行」恢复可点");
  const un = await env.fromPage({ type: "pc.unpair" });
  assert.equal(un.ok, true, "解除配对不再被旧记录挡住");
});

test("开始批次那一页自己的 hello（加载时刻早于批次开始）迟到了：不当成刷新", async () => {
  const { srv, env } = await paired();
  env.addTab(3, AMZ, () => ({ running: true, run_id: 9 }));
  const start = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
  await env.fromTab(3, { type: "pc.hello", href: AMZ, loaded_at: Date.now() - 60_000 });
  await env.settle();
  assert.equal((await get(env.session, "pc.activeRun")).run_id, start.data.run_id);
  assert.equal(srv.state.runs.get(start.data.run_id).ended, false);
});

test("执行页面离开（pagehide → pc.bye，带着它正在跑的批次）：后台接管并结束批次", async () => {
  const { srv, env } = await paired();
  env.addTab(3, AMZ, () => ({ running: true, run_id: 9 }));
  const start = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
  await env.fromTab(3, { type: "pc.bye", loaded_at: Date.now() - 60_000, run_id: start.data.run_id });
  await env.settle();
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  assert.equal(srv.state.runs.get(start.data.run_id).finish_detail, "page_left");
});

test("批次闹钟：执行页面对 ping 说没在跑（或收不到）→ 接管并结束；还在跑 → 照常兜底续租；服务端说执行权没了 → 本地也清掉", async () => {
  const { srv, env } = await paired();
  let reply = { running: true, run_id: 9 };
  env.addTab(3, AMZ, (m) => (m.type === "pc.ping" ? reply : { ok: true }));
  const start = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
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
  const s2 = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
  srv.state.runs.get(s2.data.run_id).ended = true;
  await env.fireAlarm("pc.runBeat");
  assert.equal(await get(env.session, "pc.activeRun"), undefined);
  assert.ok(env.sentToTabs.some((x) => x.msg.type === "pc.stopRun"));
});

test("执行中的标签页被关掉：立即通知服务端结束批次", async () => {
  const { srv, env } = await paired();
  env.addTab(3, AMZ, () => ({ running: true, run_id: 9 }));
  const start = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
  await env.removeTab(3);
  assert.equal(srv.state.runs.get(start.data.run_id).finish_detail, "tab_closed");
  assert.ok(env.logs().some((t) => /执行中的标签页已关闭：批次 #9 中断/.test(t)));
});

test("插件更新（storage.session 清空）后留下的旧「正在执行」：onInstalled 清掉", async () => {
  const { env } = await paired();
  await env.local.set({ "pc.runView": { active: true, tab_id: 3, view: null, updated_at: Date.now() } });
  env.clearSession();
  await env.startWorker();
  await env.fireInstalled("update");
  assert.equal((await get(env.local, "pc.runView")).active, false);
});

test("定时到点：本地记录说在执行、但执行页面的引擎已经没了 → 不再「并入当前批次」，先结束旧批次再开始定时批次", async () => {
  const { srv, env } = await paired({ manual: false, server: { now: Date.parse("2026-11-05T05:59:00Z") } });
  let ping = { running: true, run_id: 9 };
  const tabMsgs = [];
  env.addTab(3, AMZ, (m) => {
    tabMsgs.push(m.type);
    if (m.type === "pc.ping") return ping;
    if (m.type === "pc.startScheduled") return { result: "started", message: "执行批次 #10 已开始" };
    return { ok: true };
  });
  const start = await env.fromTab(3, { type: "pc.api", op: "startRun", args: ["manual"] });
  ping = { running: false, run_id: null }; // 页面刷新过，新页面里没有引擎
  srv.setNow(Date.parse("2026-11-05T06:00:05Z"));
  await env.fireAlarm("pc.sched:14:00");
  assert.equal(srv.state.runs.get(start.data.run_id).ended, true, "旧批次已结束");
  assert.ok(tabMsgs.includes("pc.startScheduled"), "开始了定时批次");
  const sched = await get(env.local, "pc.schedule");
  assert.deepEqual(sched.done, ["14:00"]);
  assert.ok(env.logs().some((t) => /定时 14:00：执行批次 #10 已开始/.test(t)));
  assert.ok(!env.logs().some((t) => /并入当前批次/.test(t)));
});

// ── 定时到点先拿新配置（D6-06）──────────────────────────────────────────

test("定时闹钟：先同步心跳再判断 —— 缓存说暂停、而服务端刚恢复，这一次照常开始", async () => {
  const { srv, env } = await paired({ manual: false, server: { now: Date.parse("2026-11-05T05:59:58Z"), account: { execution_paused: true, pause_reason: "人工检查" } } });
  assert.equal((await env.fromPage({ type: "pc.getState" })).config.heartbeat.account.execution_paused, true);
  const tabMsgs = [];
  env.addTab(3, AMZ, (m) => {
    tabMsgs.push(m.type);
    return m.type === "pc.startScheduled" ? { result: "started", message: "执行批次 #9 已开始" } : { ok: true };
  });
  // 所有者在闹钟响之前一两秒恢复了执行；本地缓存还是暂停，时钟已过 14:00
  srv.state.account.execution_paused = false;
  srv.setNow(Date.parse("2026-11-05T06:00:01Z"));
  const cfg = await get(env.local, "pc.config");
  await env.local.set({ "pc.config": { ...cfg, clock_offset_ms: cfg.clock_offset_ms + 5_000 } });
  const hb0 = srv.count(/\/heartbeat$/);
  await env.fireAlarm("pc.sched:14:00");
  assert.equal(srv.count(/\/heartbeat$/), hb0 + 1, "到点先发心跳");
  assert.ok(tabMsgs.includes("pc.startScheduled"));
  assert.ok(!env.logs().some((t) => /账号已暂停执行，今天这次不执行/.test(t)));
});

test("定时到点、本设备上一次的批次租约还没到期（ACCOUNT_BUSY 持有者是自己）：稍后重试，不算处理过", async () => {
  const { srv, env } = await paired({ manual: false, server: { now: Date.parse("2026-11-05T06:00:05Z") } });
  env.addTab(3, AMZ, (m) => (m.type === "pc.startScheduled" ? { result: "retry", message: "开始执行失败：本设备已有一个未结束的执行批次" } : { ok: true }));
  await env.fireAlarm("pc.sched:14:00");
  const sched = await get(env.local, "pc.schedule");
  assert.deepEqual(sched.done, [], "不算处理过");
  assert.ok(env.logs().some((t) => /暂未开始.*稍后自动重试/.test(t)));
  void srv;
});

// ── 结果回执（D2-06 / D6-03）────────────────────────────────────────────

test("结果送不到（服务端重启中）：后台发之前先存回执；后台重启之后补交同一份结果，任务已采购，不是结果待核实", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await paired();
    env.addTab(3, AMZ, () => ({ running: true, run_id: 9 }));
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
    let st = await env.fromPage({ type: "pc.getState" });
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
    st = await env.fromPage({ type: "pc.getState" });
    assert.equal(st.receipts.length, 0);
    globalThis.fetch = orig;
  } finally {
    mock.timers.reset();
  }
});

test("引擎因结果送不到而中断：后台接管批次 —— 续租、补交，送达之后才结束批次；结束不会把成功判成结果待核实", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await paired();
    env.addTab(3, AMZ, () => ({ running: false, run_id: null }));
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

    down = false; // 服务端恢复
    const beats0 = srv.state.runs.get(runId).beats;
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

test("接管的批次在租约预算内一直送不到：放手（不再续租），回执保留；有回执没送到时不能解除配对", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { srv, env } = await paired();
    env.addTab(3, AMZ, () => ({ running: false, run_id: null }));
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
    const un = await ticking(env, env.fromPage({ type: "pc.unpair" }));
    assert.equal(un.ok, false);
    assert.match(un.message, /还有 1 条结果回执没送到服务端/);
    globalThis.fetch = orig;
    void srv;
  } finally {
    mock.timers.reset();
  }
});

// ── 身份按页面加载先后对账（D6-02）──────────────────────────────────────

test("两个标签页、两个买家号：旧标签页重读绑定账号也不能把服务端翻回「一致」；开始前对账有分歧就不开始；旧页面刷新后恢复", async () => {
  const { srv, env } = await paired();
  const now = Date.now();
  const id = (cid, loaded) => ({ signed_in: true, customer_id: cid, site: "US", page_url: AMZ, at: now, loaded_at: loaded });
  let tab1 = id(BOUND, now - 3_600_000);
  env.addTab(1, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: tab1 } : { ok: true }));
  env.addTab(2, AMZ, (m) => (m.type === "pc.readIdentity" ? { ok: true, identity: id(OTHER, now) } : { ok: true }));
  await env.fromTab(1, { type: "pc.identity", identity: tab1, force: false });
  assert.equal(srv.state.identity.customer_id, BOUND);
  await env.fromTab(2, { type: "pc.identity", identity: id(OTHER, now), force: false });
  assert.equal(srv.state.identity.customer_id, OTHER, "新加载的页面显示另一个买家号");
  await env.fromTab(1, { type: "pc.identity", identity: { ...tab1, at: Date.now() }, force: true });
  assert.equal(srv.state.identity.customer_id, OTHER, "旧页面（加载时刻没变）强制重报，也不翻回绑定账号");
  const st = await env.fromPage({ type: "pc.getState" });
  assert.equal(st.identity.conflict, true);

  const prep = await env.fromTab(1, { type: "pc.prepareStart" });
  assert.equal(prep.conflict, true);
  assert.match(prep.message, /请刷新或关闭较早打开的亚马逊标签页/);

  // 另一个标签页关掉之后，它仍是最新加载的证据
  await env.removeTab(2);
  assert.equal((await env.fromPage({ type: "pc.getState" })).identity.customer_id, OTHER);
  // 旧标签页刷新：浏览器其实已经换回绑定账号
  tab1 = id(BOUND, Date.now() + 1);
  await env.fromTab(1, { type: "pc.identity", identity: tab1, force: true });
  const st2 = await env.fromPage({ type: "pc.getState" });
  assert.equal(st2.identity.customer_id, BOUND);
  assert.equal(st2.identity.conflict, false);
  assert.equal(srv.state.identity.customer_id, BOUND);
});

test("身份读数：标签页跳到别的网站（不在亚马逊页面上了）就不再算开着的", async () => {
  const { env } = await paired();
  const now = Date.now();
  env.addTab(1, AMZ, () => ({ ok: true }));
  env.addTab(2, AMZ, () => ({ ok: true }));
  await env.fromTab(1, { type: "pc.identity", identity: { signed_in: true, customer_id: BOUND, site: "US", page_url: AMZ, at: now, loaded_at: now }, force: false });
  await env.fromTab(2, { type: "pc.identity", identity: { signed_in: true, customer_id: OTHER, site: "US", page_url: AMZ, at: now, loaded_at: now - 10_000 }, force: false });
  assert.equal((await env.fromPage({ type: "pc.getState" })).identity.conflict, true);
  env.tabs.get(2).url = "https://www.example.com/";
  await env.fromTab(1, { type: "pc.identity", identity: { signed_in: true, customer_id: BOUND, site: "US", page_url: AMZ, at: now, loaded_at: now }, force: false });
  const st = await env.fromPage({ type: "pc.getState" });
  assert.equal(st.identity.conflict, false, "跳走的那一页比现在开着的旧，作废");
  assert.equal(st.identity.customer_id, BOUND);
});

// ── 设备令牌（D6-13）────────────────────────────────────────────────────

test("设备令牌不放 chrome.storage.local（内容脚本读得到那里）：存在扩展源下的 IndexedDB", async () => {
  const { srv, env } = await paired();
  const rec = await get(env.local, "pc.device");
  assert.equal(rec.device_id, 5);
  assert.equal("token" in rec, false);
  assert.equal(JSON.stringify(env.local._all()).includes("tok-demo-1"), false, "storage.local 里任何地方都没有令牌");
  assert.equal(globalThis.indexedDB._dbs.get("pc-secrets").stores.get("kv").get("device_token"), "tok-demo-1");
  assert.ok(srv.calls.filter((c) => c.path === "/plugin/v1/heartbeat").every((c) => c.auth === "Bearer tok-demo-1"));
  const un = await env.fromPage({ type: "pc.unpair" });
  assert.equal(un.ok, true);
  assert.equal(globalThis.indexedDB._dbs.get("pc-secrets").stores.get("kv").has("device_token"), false, "解除配对删掉令牌");
});

test("早期版本把令牌写在 storage.local：后台读到时搬进 IndexedDB 并从记录里删掉，配对状态不变", async () => {
  const srv = createFakeServer({ tasks: TASKS(), account: { purchase_mode: "manual" } });
  const env = installFakeChrome({ fetch: srv.fetch });
  await env.local.set({
    "pc.device": { token: "tok-demo-1", device_id: 5, device_name: "Mac-DEMO", browser: "chrome", workspace_name: null, server_url: "http://127.0.0.1:8790", paired_at: Date.now() },
  });
  await env.startWorker();
  const st = await env.fromPage({ type: "pc.heartbeatNow" });
  assert.equal(st.device.paired, true);
  assert.equal(st.connection.ok, true);
  assert.equal("token" in (await get(env.local, "pc.device")), false);
  assert.equal(globalThis.indexedDB._dbs.get("pc-secrets").stores.get("kv").get("device_token"), "tok-demo-1");
});

test("模拟结局的日志写中文名，不写代码", async () => {
  const { env } = await paired();
  await env.fromPage({ type: "pc.saveSettings", patch: { simulationOutcome: "price_over_cap" } });
  assert.ok(env.logs().includes("模拟结局改为：结算总额超过限价"));
});
