/** 状态 → 文案与按钮（core/status.ts），逐行对照规格 §5 的状态文案表；快同步的条件。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ACTION_LABEL,
  FAST_SYNC_MAX_MS,
  busyElsewhereText,
  fastSyncWanted,
  loginLabel,
  panelStatus,
  readyLine,
  unregisteredSince,
} from "../build/core/status.js";

const NOW = Date.parse("2026-11-18T06:00:00Z");
const CID = "A1DEMO00000";
const ACCOUNT = {
  id: 3, name: "环境-A", amazon_customer_id: CID, purchase_enabled: true, execution_paused: false, pause_reason: null,
  purchase_mode: "manual", schedule_times: [], effective_max_delivery_days: 7, quota_today: { limit: 20, used: 1, planned: 4, available: 19 },
  executable_today: 4, future_assigned: 2, active_run: null,
};
const hb = (login, extra = {}) => ({
  device: { id: 12, name: "浏览器 12" },
  login: { customer_id: null, at: "2026-11-18T05:59:00Z", stale: false, ...login },
  account: null, device_run: null, server_time: null, bj_date: "2026-11-18", simulation_only: true,
  ...extra,
});
const snap = (heartbeat, extra = {}) => ({
  settings: { serverUrl: "http://127.0.0.1:8790", simulationOutcome: "pass", workspaceId: null },
  device: { registered: true, device_id: 12, device_name: "浏览器 12", workspace_id: 1, workspace_name: "工作区-DEMO", registered_at: NOW },
  registration: { ok: true, at: NOW, fails: 0, next_at: null, halted: false, code: null, message: null },
  config: heartbeat ? { heartbeat, received_at: NOW, clock_offset_ms: 0, unregistered_since: null } : null,
  connection: { ok: true, at: NOW, kind: null, code: null, message: null },
  identity: null, receipts: [], logs: [], schedule: null, run: null, active_run: null, ext_version: "0.2.0",
  ...extra,
});
const of = (state, extra = {}, login = {}) => panelStatus(snap(hb({ state, ...login }, extra)));

test("连不上服务端：写明地址、去哪里改、会自动重试；按钮「重试」「打开设置」", () => {
  const st = panelStatus(snap(null, {
    device: { registered: false, device_id: null, device_name: null, workspace_id: null, workspace_name: null, registered_at: null },
    registration: { ok: false, at: NOW, fails: 1, next_at: NOW + 5_000, halted: false, code: "NETWORK", message: "无法连接服务端" },
    connection: { ok: false, at: NOW, kind: "network", code: "NETWORK", message: "无法连接服务端（请确认服务端已启动、地址与端口正确）" },
  }));
  assert.equal(st.kind, "offline");
  assert.equal(st.text, "连接失败：连不上服务端 http://127.0.0.1:8790。请确认服务端已启动；地址不同时到设置里修改。插件会自动重试。");
  assert.deepEqual(st.actions, ["retry", "open_options"]);
  assert.deepEqual(st.actions.map((a) => ACTION_LABEL[a]), ["重试", "打开设置"]);
  // 登记过、后来心跳连不上：同一句
  const later = panelStatus(snap(hb({ state: "ready", customer_id: CID }, { account: ACCOUNT }), { connection: { ok: false, at: NOW, kind: "network", code: "TIMEOUT", message: "请求超时" } }));
  assert.equal(later.kind, "offline");
});

test("插件过旧：连接失败：插件版本过旧，请重新构建并加载新版插件。", () => {
  const st = panelStatus(snap(null, { connection: { ok: false, at: NOW, kind: "http", code: "PLUGIN_OUTDATED", message: "插件版本过旧：请重新构建并加载新版插件（需要 0.2.0 及以上）" } }));
  assert.equal(st.kind, "outdated");
  assert.equal(st.text, "连接失败：插件版本过旧，请重新构建并加载新版插件。");
  assert.deepEqual(st.actions, []);
});

test("服务端明确拒绝：原话接在「连接失败：」后面；要填工作区 ID 时先给「打开设置」", () => {
  const ws = panelStatus(snap(null, {
    registration: { ok: false, at: NOW, fails: 1, next_at: null, halted: true, code: "WORKSPACE_REQUIRED", message: "x" },
    connection: { ok: false, at: NOW, kind: "http", code: "WORKSPACE_REQUIRED", message: "系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）" },
  }));
  assert.equal(ws.kind, "refused");
  assert.equal(ws.text, "连接失败：系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）。处理后点「重试」。");
  assert.deepEqual(ws.actions, ["open_options", "retry"]);
  const gate = panelStatus(snap(null, { connection: { ok: false, at: NOW, kind: "http", code: "FORBIDDEN", message: "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持" } }));
  assert.equal(gate.text, "连接失败：插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持。");
  const limited = panelStatus(snap(null, {
    registration: { ok: false, at: NOW, fails: 1, next_at: NOW + 42_000, halted: false, code: "RATE_LIMITED", message: "x" },
    connection: { ok: false, at: NOW, kind: "http", code: "RATE_LIMITED", message: "登记的浏览器太多：请稍后再试" },
  }));
  assert.equal(limited.text, "连接失败：登记的浏览器太多：请稍后再试。插件稍后会自动重试。");
});

test("还没有任何结果：正在连接服务端 <地址>（没有按钮，不要求所有者做什么）", () => {
  const st = panelStatus(snap(null, { device: { ...snap(null).device, registered: false }, registration: null, connection: null }));
  assert.equal(st.kind, "connecting");
  assert.equal(st.text, "正在连接服务端 http://127.0.0.1:8790。");
  assert.equal(st.needs_owner, false);
});

test("unknown / signed_out / no_id / conflict：文案与按钮照规格 §5", () => {
  assert.equal(of("unknown").text, "还没有读到登录状态。打开或刷新亚马逊页面后自动读取。");
  assert.deepEqual(of("unknown").actions, []);
  assert.equal(of("signed_out").text, "未登录亚马逊。请在这个浏览器登录要用来采购的买家号，登录后这里自动更新。");
  assert.deepEqual(of("signed_out").actions, []);
  assert.equal(of("no_id").text, "这一页没有读到买家号 ID。打开订单页后插件会自动读取。");
  assert.deepEqual(of("no_id").actions.map((a) => ACTION_LABEL[a]), ["打开订单页"]);
  assert.equal(of("conflict").text, "这个浏览器的亚马逊标签页显示的买家号不一致。请刷新或关闭较早打开的亚马逊标签页。");
  assert.deepEqual(of("conflict").actions.map((a) => ACTION_LABEL[a]), ["重新读取"]);
  for (const k of ["signed_out", "no_id", "conflict"]) assert.equal(of(k).needs_owner, true, `${k} 要所有者动手`);
});

test("本地刚对出分歧（服务端还说 ready）：也按分歧显示，并写明其中一页显示的是谁（完整的买家号 ID）", () => {
  const st = panelStatus(snap(hb({ state: "ready", customer_id: CID }, { account: ACCOUNT }), {
    identity: { signed_in: true, customer_id: "A9OTHER00000", site: "US", page_url: null, conflict: true, at: NOW, loaded_at: NOW, source_tab: 2, sources: 2 },
  }));
  assert.equal(st.kind, "conflict");
  assert.deepEqual(st.extra, ["其中一页显示：买家号 ID A9OTHER00000。"]);
  assert.equal(st.account, null, "有分歧时不显示账号，也没有开始按钮");
});

test("unregistered：买家号 ID 完整显示，可复制、可打开控制台；ignored：说明已忽略", () => {
  const u = of("unregistered", {}, { customer_id: "A2TEST00000" });
  assert.equal(u.text, "买家号 A2TEST00000 还没有登记。在控制台「账号」页的待登记列表里点「登记」，之后这里自动更新。");
  assert.equal(u.customer_id, "A2TEST00000", "面板上完整显示（不遮掩）");
  assert.deepEqual(u.actions.map((a) => ACTION_LABEL[a]), ["复制买家号 ID", "打开控制台"]);
  const i = of("ignored", {}, { customer_id: "A2TEST00000" });
  assert.equal(i.text, "买家号 A2TEST00000 已在控制台忽略，不会分到任务。要使用它，在控制台「账号」页恢复后登记。");
  assert.equal(i.customer_id, "A2TEST00000");
});

test("ready：账号名、今天可执行笔数（另有几笔在未来）、额度；数字读不到写「—」不写 0", () => {
  const st = of("ready", { account: ACCOUNT }, { customer_id: CID });
  assert.equal(st.kind, "ready");
  assert.equal(st.text, "账号「环境-A」· 今天可执行 4 笔（另有 2 笔建议采购日期在未来）· 额度 已用 1 / 上限 20。");
  assert.deepEqual(st.extra, []);
  assert.equal(st.customer_id, CID);
  assert.equal(st.account.id, 3);
  assert.equal(readyLine({ ...ACCOUNT, future_assigned: 0 }), "账号「环境-A」· 今天可执行 4 笔 · 额度 已用 1 / 上限 20。");
  assert.equal(readyLine({ ...ACCOUNT, executable_today: 0, future_assigned: 3 }), "账号「环境-A」· 今天可执行 0 笔（另有 3 笔建议采购日期在未来）· 额度 已用 1 / 上限 20。");
  assert.equal(readyLine({ ...ACCOUNT, executable_today: null, future_assigned: null, quota_today: null }), "账号「环境-A」· 今天可执行 — 笔 · 额度 已用 — / 上限 —。", "没读到不是 0");
  assert.equal(
    readyLine({ ...ACCOUNT, executable_today: 0, future_assigned: 0 }),
    "账号「环境-A」· 还没有任务分给这个买家号，可在控制台「待采购」里查看原因 · 额度 已用 1 / 上限 20。",
  );
});

test("ready，采购开关关闭：追加一句；已暂停：换成暂停的说明", () => {
  const off = of("ready", { account: { ...ACCOUNT, purchase_enabled: false } }, { customer_id: CID });
  assert.match(off.text, /^账号「环境-A」· 今天可执行 4 笔/);
  assert.deepEqual(off.extra, ["控制台里这个账号的采购开关是关闭的，不会分到新任务；已分配的任务照常执行。"]);
  const paused = of("ready", { account: { ...ACCOUNT, execution_paused: true, pause_reason: "结果待核实" } }, { customer_id: CID });
  assert.equal(paused.text, "已暂停执行：结果待核实。处理后在控制台「账号」页恢复执行。");
  assert.equal(paused.needs_owner, true);
  // 服务端说 ready 却没给账号：不往 ready 猜
  assert.equal(of("ready", { account: null }, { customer_id: CID }).kind, "unknown");
});

test("这个买家号正在别的浏览器里执行：ready 状态下追加一句（北京时间 HH:MM）", () => {
  const busy = of("ready", { account: { ...ACCOUNT, active_run: { id: 8, device_id: 13, device_name: "浏览器 13", started_at: "2026-11-18T05:58:00Z", is_mine: false } } }, { customer_id: CID });
  assert.deepEqual(busy.extra, ["这个买家号正在浏览器「浏览器 13」里执行（13:58 开始）。"]);
  const mine = of("ready", { account: { ...ACCOUNT, active_run: { id: 8, device_id: 12, device_name: "浏览器 12", started_at: null, is_mine: true } } }, { customer_id: CID });
  assert.deepEqual(mine.extra, [], "自己的批次不算「别的浏览器」");
  assert.equal(busyElsewhereText(null, null), "这个买家号正在另一个浏览器里执行");
  assert.equal(busyElsewhereText("浏览器 13", "2026-11-18T05:58:00Z"), "这个买家号正在浏览器「浏览器 13」里执行（13:58 开始）");
});

test("界面文案里不出现「设备」「绑定」「配对」，不用感叹号", () => {
  const texts = [];
  for (const state of ["unknown", "signed_out", "no_id", "conflict", "unregistered", "ignored"]) {
    const st = of(state, {}, { customer_id: CID });
    texts.push(st.text, st.short, ...st.extra);
  }
  const ready = of("ready", { account: { ...ACCOUNT, purchase_enabled: false } }, { customer_id: CID });
  texts.push(ready.text, ready.short, ...ready.extra, ...Object.values(ACTION_LABEL));
  for (const state of ["unknown", "signed_out", "no_id", "conflict", "unregistered", "ignored", "ready"]) texts.push(loginLabel(state, CID, "环境-A", true));
  for (const t of texts) {
    assert.doesNotMatch(t, /设备|绑定|配对/, t);
    assert.doesNotMatch(t, /[!！]/, t);
  }
});

test("「当前登录」一栏的说法与控制台「浏览器」页签一致（规格 §5 表右栏）", () => {
  assert.equal(loginLabel("unknown", null, null), "还没有上报");
  assert.equal(loginLabel("signed_out", null, null), "未登录亚马逊");
  assert.equal(loginLabel("no_id", null, null), "已登录，没读到买家号 ID");
  assert.equal(loginLabel("conflict", null, null), "登录状态不一致");
  assert.equal(loginLabel("unregistered", CID, null), "未登记的买家号 A1DEMO00000");
  assert.equal(loginLabel("ignored", CID, null), "已忽略的买家号 A1DEMO00000");
  assert.equal(loginLabel("ready", CID, "环境-A"), "环境-A");
  assert.equal(loginLabel("ready", CID, "环境-A", true), "环境-A（已暂停）");
});

test("快同步（10 秒）只在 unregistered 时开，而且自这个状态出现起最多 10 分钟；其余状态一律不开", () => {
  const cfg = (state, since) => ({ heartbeat: hb({ state, customer_id: CID }), received_at: NOW, clock_offset_ms: 0, unregistered_since: since });
  assert.equal(FAST_SYNC_MAX_MS, 600_000);
  assert.equal(fastSyncWanted(cfg("unregistered", NOW), NOW), true);
  assert.equal(fastSyncWanted(cfg("unregistered", NOW), NOW + FAST_SYNC_MAX_MS - 1), true);
  assert.equal(fastSyncWanted(cfg("unregistered", NOW), NOW + FAST_SYNC_MAX_MS), false, "满 10 分钟就停");
  assert.equal(fastSyncWanted(cfg("unregistered", null), NOW), false, "不知道从什么时候开始的不开");
  for (const state of ["unknown", "signed_out", "no_id", "conflict", "ignored", "ready"]) {
    assert.equal(fastSyncWanted(cfg(state, NOW), NOW), false, state);
  }
  assert.equal(fastSyncWanted(null, NOW), false);
});

test("unregistered 的起点：刚变成这个状态记现在；一直是它（同一个买家号）沿用原来的；换了买家号重新计；不是这个状态为 null", () => {
  const prev = (state, cid, since) => ({ heartbeat: hb({ state, customer_id: cid }), received_at: NOW, clock_offset_ms: 0, unregistered_since: since });
  assert.equal(unregisteredSince(null, { state: "unregistered", customer_id: CID }, NOW), NOW);
  assert.equal(unregisteredSince(prev("signed_out", null, null), { state: "unregistered", customer_id: CID }, NOW), NOW);
  assert.equal(unregisteredSince(prev("unregistered", CID, NOW - 60_000), { state: "unregistered", customer_id: CID }, NOW), NOW - 60_000, "沿用起点：10 分钟的上限不会被每次心跳往后推");
  assert.equal(unregisteredSince(prev("unregistered", "A2TEST00000", NOW - 60_000), { state: "unregistered", customer_id: CID }, NOW), NOW, "换了买家号");
  assert.equal(unregisteredSince(prev("unregistered", CID, NOW - 60_000), { state: "ready", customer_id: CID }, NOW), null);
});
