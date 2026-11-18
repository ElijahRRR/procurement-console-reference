/** 后台对当前批次的裁决（core/custody.ts）、「开始执行」能不能点（core/startgate.ts）、错误码与停止原因的中文说明。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CUSTODY_MARGIN_MS, CUSTODY_MAX_MS, custodyOverBudget, custodyStep, helloEndsRun, pingSaysAlive } from "../build/core/custody.js";
import { runViewActive, startGate } from "../build/core/startgate.js";
import {
  ACCOUNT_LEVEL_CODES,
  ATTEMPT_CODE_LABEL,
  BUYER_CHANGED_TEXT,
  CANCEL_CODE_LABEL,
  GUARD_CODE_LABEL,
  LOCAL_CODE_LABEL,
  SERVER_CODE_LABEL,
  STOP_REASON_LABEL,
  abortCodeFor,
  cancelLabel,
  codeLabel,
  stageLabel,
  stopReasonLabel,
} from "../build/core/codes.js";
import { REPORT_RETRY_MAX_MS, beatInterval, claimWaitMs, leaseMinutes } from "../build/core/engine.js";

const NOW = Date.parse("2026-11-05T06:00:00Z");
const ar = (extra = {}) => ({ run_id: 9, tab_id: 3, trigger: "manual", started_at: NOW, touched_at: NOW, lease_ttl_s: 180, last_beat_ok_at: NOW, orphaned_at: null, orphan_reason: null, customer_id: "A1DEMO00000", account_name: "环境-A", ...extra });

test("同一标签页来了一张批次开始之后加载的新页面：原来的引擎已经没了；开始批次那一页自己迟到的 hello 不算", () => {
  assert.equal(helloEndsRun(ar(), 3, NOW + 5_000), true);
  assert.equal(helloEndsRun(ar(), 3, NOW - 60_000), false, "这一页在批次开始之前就加载了：就是跑批次的那一页");
  assert.equal(helloEndsRun(ar(), 4, NOW + 5_000), false, "别的标签页");
  assert.equal(helloEndsRun(ar(), 3, null), true, "没带加载时刻：hello 每次页面加载只发一次");
  assert.equal(helloEndsRun(null, 3, NOW), false);
});

test("ping 的答复：在跑这个批次（或正在申请批次）才算活着", () => {
  assert.equal(pingSaysAlive({ running: true, run_id: 9 }, 9), true);
  assert.equal(pingSaysAlive({ running: true, run_id: null }, 9), true);
  assert.equal(pingSaysAlive({ running: true, run_id: 10 }, 9), false);
  assert.equal(pingSaysAlive({ running: false, run_id: null }, 9), false, "刷新后的新页面：没在执行");
  assert.equal(pingSaysAlive(undefined, 9), false);
});

test("接管的批次：没有待送的回执就结束；有就续租等着；租约预算用完就放手", () => {
  assert.equal(custodyStep(ar(), NOW + 1_000, 0), "finish");
  assert.equal(custodyStep(ar(), NOW + 1_000, 1), "beat");
  assert.equal(custodyStep(ar(), NOW + 180_000 - CUSTODY_MARGIN_MS + 1, 1), "release");
  assert.equal(custodyStep(ar({ lease_ttl_s: 30 }), NOW + 25_000, 1), "release", "按服务端给的有效期算");
  assert.equal(custodyStep(ar({ lease_ttl_s: null }), NOW + 100_000, 1), "beat", "不知道有效期按 180 秒");
});

test("接管的批次：续租一直成功、回执却一直送不到 —— 接管满 10 分钟（引擎重发结果的上限）就放手", () => {
  assert.equal(CUSTODY_MAX_MS, 10 * 60_000);
  assert.equal(CUSTODY_MAX_MS, REPORT_RETRY_MAX_MS, "和引擎自己重发结果的总时长上限一致");
  const t = NOW + 11 * 60_000;
  assert.equal(custodyStep(ar({ orphaned_at: NOW, last_beat_ok_at: t }), t + 1, 1), "release", "刚续租成功也放手");
  assert.equal(custodyStep(ar({ orphaned_at: NOW, last_beat_ok_at: NOW + CUSTODY_MAX_MS }), NOW + CUSTODY_MAX_MS, 1), "beat", "正好 10 分钟还在续");
  assert.equal(custodyStep(ar({ orphaned_at: NOW, last_beat_ok_at: t }), t + 1, 0), "finish", "回执都送到了就结束批次");
  assert.equal(custodyOverBudget(ar({ orphaned_at: null }), NOW + 3_600_000), false, "没接管（引擎还在）不算");
  assert.equal(custodyOverBudget(ar({ orphaned_at: NOW }), NOW + CUSTODY_MAX_MS + 1), true);
});

const ACCOUNT = {
  id: 3, name: "环境-A", amazon_customer_id: "A1DEMO00000", purchase_enabled: true, execution_paused: false, pause_reason: null,
  purchase_mode: "manual", schedule_times: [], effective_max_delivery_days: 7, quota_today: { limit: 20, used: 1, planned: 4, available: 19 },
  executable_today: 4, future_assigned: 2, active_run: null,
};
const hb = (extra = {}) => ({
  device: { id: 12, name: "浏览器 12" },
  login: { state: "ready", customer_id: "A1DEMO00000", at: "2026-11-05T05:59:00Z", stale: false },
  account: ACCOUNT, device_run: null, server_time: null, bj_date: null, simulation_only: true,
  ...extra,
});
const snap = (extra = {}) => ({
  settings: { serverUrl: "http://127.0.0.1:8790", simulationOutcome: "pass", workspaceId: null },
  device: { registered: true, device_id: 12, device_name: "浏览器 12", workspace_id: 1, workspace_name: null, registered_at: NOW },
  registration: { ok: true, at: NOW, fails: 0, next_at: null, halted: false, code: null, message: null },
  config: { received_at: NOW, clock_offset_ms: 0, unregistered_since: null, heartbeat: hb() },
  connection: { ok: true, at: NOW, kind: null, code: null, message: null },
  identity: null, receipts: [], logs: [], schedule: null, run: null, active_run: null, ext_version: "0.2.0",
  ...extra,
});
const withHb = (extra) => snap({ config: { ...snap().config, heartbeat: hb(extra) } });

test("「开始执行」：本地的「正在执行」被更新的服务端心跳否定就作废 —— 看的是 device_run（这个浏览器自己的批次），不是账号的批次", () => {
  assert.equal(startGate(snap()).ok, true);
  const running = { active: true, tab_id: 3, view: null, updated_at: NOW - 1 };
  const old = snap({ run: running });
  assert.equal(runViewActive(old), false, "心跳（NOW）比本地记录（NOW-1）新，且服务端说这个浏览器没有批次");
  assert.equal(startGate(old).ok, true);
  const fresh = snap({ run: { ...running, updated_at: NOW + 1 } });
  assert.equal(startGate(fresh).ok, false);
  assert.equal(startGate(fresh).reason, "本浏览器正在执行");
  // 换登了另一个买家号：account（及它的 active_run）跟着变成别的账号，但这个浏览器上一个买家号的批次还在
  const switched = snap({ run: running, config: { ...snap().config, heartbeat: hb({ account: { ...ACCOUNT, id: 4, name: "环境-B", active_run: null }, device_run: { id: 9, account_id: 3, account_name: "环境-A", started_at: null } }) } });
  assert.equal(runViewActive(switched), true, "device_run 还在：本地的「正在执行」不作废");
  assert.equal(startGate(switched).ok, false);
  // 反过来：服务端说这个浏览器有批次而本地没有 → 不禁用（点了看 DEVICE_BUSY）
  assert.equal(startGate(withHb({ device_run: { id: 9, account_id: 3, account_name: "环境-A", started_at: null } })).ok, true);
  // 账号正在别的浏览器里执行（account.active_run 不是自己的）→ 也不禁用（数字可能过时，点了看 ACCOUNT_BUSY）
  assert.equal(startGate(withHb({ account: { ...ACCOUNT, active_run: { id: 8, device_id: 13, device_name: "浏览器 13", started_at: null, is_mine: false } } })).ok, true);
});

test("「开始执行」：只有买家号已登记（ready）且账号没暂停才能点；今天可执行 0 笔不禁用；后台还在替上一个批次收尾时不能点", () => {
  for (const state of ["unknown", "signed_out", "no_id", "conflict", "unregistered", "ignored"]) {
    const g = startGate(withHb({ login: { state, customer_id: "A1DEMO00000", at: null, stale: false }, account: null }));
    assert.equal(g.ok, false, state);
    assert.ok(g.reason, state);
  }
  assert.equal(startGate(withHb({ account: { ...ACCOUNT, executable_today: 0, future_assigned: 0 } })).ok, true, "数字可能过时，点了以服务端答复为准");
  const paused = startGate(withHb({ account: { ...ACCOUNT, execution_paused: true, pause_reason: "结果待核实" } }));
  assert.deepEqual(paused, { ok: false, reason: "账号已暂停执行" });
  assert.equal(startGate(withHb({ account: { ...ACCOUNT, purchase_enabled: false } })).ok, true, "采购开关关闭：已分配的任务照常执行");
  assert.equal(startGate(snap({ device: { ...snap().device, registered: false }, config: null, connection: null })).ok, false, "还没连上服务端");
  assert.equal(startGate(snap({ connection: { ok: false, at: NOW, kind: "network", code: "NETWORK", message: "无法连接服务端" } })).ok, false, "连不上服务端");
  const custody = startGate(snap({ active_run: { run_id: 9, account_name: "环境-A", started_at: NOW, orphaned: true } }));
  assert.equal(custody.ok, false);
  assert.match(custody.reason, /上一个批次还在收尾/);
  assert.equal(startGate(snap({ identity: { signed_in: true, customer_id: "A9OTHER00000", site: "US", page_url: null, conflict: true, at: NOW, loaded_at: NOW, source_tab: 2, sources: 2 } })).ok, false, "本地刚对出分歧");
});

test("中止码：只有上游取消才报 ABORTED_UPSTREAM_CANCELLED；尝试已结束不报；旧服务端没给原因码时退回它认得的那个", () => {
  assert.equal(abortCodeFor("UPSTREAM_CANCELLED"), "ABORTED_UPSTREAM_CANCELLED");
  for (const c of ["ACCOUNT_PAUSED", "ACCOUNT_ARCHIVED", "IDENTITY_MISMATCH", "IDENTITY_STALE", "SOURCE_CHANGED", "SOURCE_HOLD"]) {
    assert.equal(abortCodeFor(c), "ABORTED_BY_SERVER", c);
  }
  assert.equal(abortCodeFor("ATTEMPT_FINISHED"), null);
  assert.equal(abortCodeFor(null), "ABORTED_UPSTREAM_CANCELLED");
  assert.match(codeLabel("ABORTED_BY_SERVER"), /服务端要求中止/);
});

test("停止原因：服务端的三个值都有中文（不再有「解除绑定」）；插件上报的码都有中文；PLUGIN_SIMULATION_ONLY 是账号级", () => {
  for (const r of ["account_paused", "account_archived", "identity_mismatch"]) {
    assert.notEqual(stopReasonLabel(r), r, r);
    assert.doesNotMatch(stopReasonLabel(r), /[a-z_]{4,}/, r);
  }
  assert.equal("device_unbound" in STOP_REASON_LABEL, false);
  assert.equal(stopReasonLabel("device_unbound"), "服务端要求停止", "已经不存在的原因按认不出处理");
  assert.match(stopReasonLabel("identity_mismatch"), /买家号变了/);
  for (const c of ["SUBMIT_REJECTED", "PLUGIN_SIMULATION_ONLY", "GUARD_BLOCKED", "ABORTED_UPSTREAM_CANCELLED", "OUT_OF_STOCK", "PAGE_LOAD_FAILED", "CAPTCHA_ENCOUNTERED"]) {
    assert.notEqual(codeLabel(c), c, c);
  }
  assert.equal(ACCOUNT_LEVEL_CODES.has("PLUGIN_SIMULATION_ONLY"), true);
});

test("IDENTITY_MISMATCH 不再是账号级：执行中换了买家号不暂停账号（中止类）；说明里没有「绑定」", () => {
  assert.equal(ACCOUNT_LEVEL_CODES.has("IDENTITY_MISMATCH"), false);
  assert.deepEqual([...ACCOUNT_LEVEL_CODES].sort(), ["ACCOUNT_RESTRICTED", "CAPTCHA_ENCOUNTERED", "CARD_DECLINED", "LOGIN_REQUIRED", "PLUGIN_SIMULATION_ONLY"]);
  assert.equal(abortCodeFor("IDENTITY_MISMATCH"), "ABORTED_BY_SERVER");
  assert.equal(cancelLabel("IDENTITY_MISMATCH"), "浏览器登录的买家号变了");
  assert.equal(BUYER_CHANGED_TEXT, "浏览器登录的买家号变了，这一笔在提交前中止");
  const all = [SERVER_CODE_LABEL, GUARD_CODE_LABEL, ATTEMPT_CODE_LABEL, LOCAL_CODE_LABEL, CANCEL_CODE_LABEL, STOP_REASON_LABEL];
  for (const table of all) {
    for (const [c, t] of Object.entries(table)) assert.doesNotMatch(t, /配对|绑定|设备/, c);
  }
});

test("续租节奏 = min(上限, 有效期 / 3)；「约 N 分钟」取自有效期", () => {
  assert.equal(beatInterval(35_000, 180), 35_000);
  assert.equal(beatInterval(35_000, 30), 10_000);
  assert.equal(beatInterval(35_000, null), 35_000);
  assert.equal(leaseMinutes(180), 3);
  assert.equal(leaseMinutes(20), 1);
  assert.equal(leaseMinutes(null), 3);
});

test("统一错误码表与规格 §2.8（契约 §8、web 的 API_ERROR_CODES）一致（含顺序），每个都有中文；没有 NOT_BOUND", () => {
  assert.deepEqual(Object.keys(SERVER_CODE_LABEL), [
    "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "VALIDATION", "CONFLICT", "ACCOUNT_BUSY", "DEVICE_BUSY", "ATTEMPT_IN_FLIGHT",
    "IDENTITY_MISMATCH", "ACCOUNT_NOT_REGISTERED", "ACCOUNT_PAUSED", "REAL_PURCHASE_DISABLED", "LEASE_LOST", "SLOT_DONE",
    "WORKSPACE_REQUIRED", "PLUGIN_OUTDATED", "FEISHU_NOT_CONFIGURED", "FEISHU_ERROR", "INVALID_TRANSITION", "SOURCE_CHANGED",
    "GUARD_BLOCKED", "RATE_LIMITED", "METHOD_NOT_ALLOWED", "INTERNAL",
  ]);
  assert.equal("NOT_BOUND" in SERVER_CODE_LABEL, false);
  assert.match(codeLabel("ACCOUNT_NOT_REGISTERED"), /还没有登记/);
  assert.equal(codeLabel("SLOT_DONE"), "这个时间点今天已经执行过");
  assert.match(codeLabel("WORKSPACE_REQUIRED"), /工作区 ID/);
  assert.match(codeLabel("PLUGIN_OUTDATED"), /插件版本过旧/);
  assert.match(codeLabel("UNAUTHORIZED"), /自动重新登记/);
  for (const [c, t] of Object.entries(SERVER_CODE_LABEL)) assert.doesNotMatch(t, /[A-Z_]{4,}/, c);
  assert.equal(codeLabel("SOURCE_CHANGED"), "领取后来源有变化，提交前中止");
  assert.match(codeLabel("GUARD_BLOCKED"), /护栏/);
  assert.match(codeLabel("RATE_LIMITED"), /尝试次数过多/);
  assert.match(codeLabel("ABORTED_BY_SERVER"), /账号暂停或归档、浏览器登录的买家号变了或登录状态过期、来源有变化/);
  assert.equal(codeLabel("HTTP_502"), "服务端出错（状态码 502）");
});

test("进度的 cancel_code 都有中文；认不出的码、停止原因与阶段名也不原样显示", () => {
  const codes = ["UPSTREAM_CANCELLED", "ACCOUNT_ARCHIVED", "ACCOUNT_PAUSED", "IDENTITY_MISMATCH", "IDENTITY_STALE", "SOURCE_HOLD", "SOURCE_CHANGED", "ATTEMPT_FINISHED"];
  assert.deepEqual(Object.keys(CANCEL_CODE_LABEL), codes);
  for (const c of codes) {
    assert.doesNotMatch(cancelLabel(c), /[A-Za-z_]{4,}/, c);
  }
  assert.equal(cancelLabel("SOURCE_CHANGED"), "领取后来源有变化，提交前中止");
  assert.equal(cancelLabel("SOMETHING_NEW"), "服务端要求中止");
  assert.equal(cancelLabel(null), "服务端要求中止");
  assert.equal(stopReasonLabel("something_new"), "服务端要求停止");
  assert.equal(stageLabel("something_new"), "执行中");
  assert.doesNotMatch(codeLabel("SOMETHING_NEW"), /SOMETHING/);
});

test("稍后再领的等待：夹在 1 – 30 秒之间", () => {
  assert.equal(claimWaitMs(5), 5_000);
  assert.equal(claimWaitMs(2.5), 2_500);
  assert.equal(claimWaitMs(0), 1_000);
  assert.equal(claimWaitMs(90), 30_000);
});

// ── 特权消息的来源检查（core/messages.isPrivilegedSender）──────────────────

test("特权消息只认扩展自己的页面：sender.tab 为空且地址在扩展源下；内容脚本一律拒绝；开在标签页里的设置页是唯一带 tab 的例外", async () => {
  const { PRIVILEGED_TYPES, isPrivilegedSender } = await import("../build/core/messages.js");
  const ROOT = "chrome-extension://pcdemoextensionid0000000000000000/";
  const ORIGIN = ROOT.slice(0, -1);
  assert.deepEqual([...PRIVILEGED_TYPES].sort(), ["pc.clearLogs", "pc.reregister", "pc.saveSettings"]);
  // 弹出页、内嵌的设置页：没有 tab
  assert.equal(isPrivilegedSender({ url: ROOT + "popup.html", origin: ORIGIN }, ROOT), true);
  assert.equal(isPrivilegedSender({ url: ROOT + "options.html" }, ROOT), true);
  // 设置页开在标签页里（清单 options_ui.open_in_tab = true）：带着 tab，源是扩展的源
  assert.equal(isPrivilegedSender({ tab: { id: 9 }, url: ROOT + "options.html", origin: ORIGIN }, ROOT), true);
  assert.equal(isPrivilegedSender({ tab: { id: 9 }, url: ROOT + "options.html?x=1#y", origin: ORIGIN }, ROOT), true);
  // 内容脚本：带着 tab，地址与源都是网页的
  assert.equal(isPrivilegedSender({ tab: { id: 3 }, url: "https://www.amazon.com/gp/css/order-history", origin: "https://www.amazon.com" }, ROOT), false);
  // 带着 tab 的别的情况一律拒绝：不是设置页、没有源、源对不上
  assert.equal(isPrivilegedSender({ tab: { id: 3 }, url: ROOT + "popup.html", origin: ORIGIN }, ROOT), false);
  assert.equal(isPrivilegedSender({ tab: { id: 3 }, url: ROOT + "options.html" }, ROOT), false);
  assert.equal(isPrivilegedSender({ tab: { id: 3 }, url: ROOT + "options.html", origin: "https://www.amazon.com" }, ROOT), false);
  assert.equal(isPrivilegedSender({ tab: { id: 3 }, url: ROOT + "options.htmlx", origin: ORIGIN }, ROOT), false);
  // 没有 tab 但地址不在扩展源下、源对不上、是别的扩展、什么都没有
  assert.equal(isPrivilegedSender({ url: "https://www.amazon.com/" }, ROOT), false);
  assert.equal(isPrivilegedSender({ url: ROOT + "popup.html", origin: "https://www.amazon.com" }, ROOT), false);
  assert.equal(isPrivilegedSender({ url: "chrome-extension://someoneelse0000000000000000000000/options.html" }, ROOT), false);
  assert.equal(isPrivilegedSender({ url: ROOT.slice(0, -1) + "x/options.html" }, ROOT), false, "根地址要带着末尾的斜杠比");
  assert.equal(isPrivilegedSender({}, ROOT), false);
  assert.equal(isPrivilegedSender(null, ROOT), false);
  assert.equal(isPrivilegedSender({ url: ROOT + "options.html" }, ""), false, "不知道扩展的根地址时谁都不认");
});
