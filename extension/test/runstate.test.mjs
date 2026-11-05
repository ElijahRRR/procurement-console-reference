/** 后台对当前批次的裁决（core/custody.ts）、「开始执行」能不能点（core/startgate.ts）、错误码与停止原因的中文说明。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CUSTODY_MARGIN_MS, custodyStep, helloEndsRun, pingSaysAlive } from "../build/core/custody.js";
import { runViewActive, startGate } from "../build/core/startgate.js";
import { ACCOUNT_LEVEL_CODES, abortCodeFor, codeLabel, stopReasonLabel } from "../build/core/codes.js";
import { beatInterval, leaseMinutes } from "../build/core/engine.js";

const NOW = Date.parse("2026-11-05T06:00:00Z");
const ar = (extra = {}) => ({ run_id: 9, tab_id: 3, trigger: "manual", started_at: NOW, touched_at: NOW, lease_ttl_s: 180, last_beat_ok_at: NOW, orphaned_at: null, orphan_reason: null, ...extra });

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

const snap = (extra = {}) => ({
  settings: { serverUrl: "http://127.0.0.1:8790", simulationOutcome: "pass" },
  device: { paired: true, device_id: 5, device_name: "D", browser: "chrome", workspace_name: null, server_url: "http://127.0.0.1:8790", paired_at: NOW },
  config: { received_at: NOW, clock_offset_ms: 0, heartbeat: { device: { id: 5, name: "D", bound_account_id: 3 }, account: { id: 3, name: "A" }, identity_status: "match", server_time: null, bj_date: null, simulation_only: true, active_run: null } },
  connection: null, identity: null, identity_status: "match", receipts: [], logs: [], schedule: null, run: null, ext_version: "0.1.0",
  ...extra,
});

test("「开始执行」：本地的「正在执行」被更新的服务端心跳否定就作废；服务端说有批次而本地没有，不禁用（点了看 ACCOUNT_BUSY）", () => {
  assert.equal(startGate(snap()).ok, true);
  const running = { active: true, tab_id: 3, view: null, updated_at: NOW - 1 };
  const old = snap({ run: running });
  assert.equal(runViewActive(old), false, "心跳（NOW）比本地记录（NOW-1）新，且服务端说没有批次");
  assert.equal(startGate(old).ok, true);
  const fresh = snap({ run: { ...running, updated_at: NOW + 1 } });
  assert.equal(startGate(fresh).ok, false);
  assert.equal(startGate(fresh).reason, "本浏览器正在执行");
  const serverBusy = snap({ config: { ...snap().config, heartbeat: { ...snap().config.heartbeat, active_run: { id: 9, device_id: 5, device_name: "D", is_mine: true } } } });
  assert.equal(startGate(serverBusy).ok, true);
  const unbound = snap({ config: { ...snap().config, heartbeat: { ...snap().config.heartbeat, account: null } } });
  assert.match(startGate(unbound).reason, /还没有绑定买家账号/);
  assert.equal(startGate(snap({ device: { ...snap().device, paired: false } })).ok, false);
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

test("停止原因：服务端的四个值都有中文；插件上报的码都有中文；PLUGIN_SIMULATION_ONLY 是账号级", () => {
  for (const r of ["account_paused", "account_archived", "device_unbound", "identity_mismatch"]) {
    assert.notEqual(stopReasonLabel(r), r, r);
    assert.doesNotMatch(stopReasonLabel(r), /[a-z_]{4,}/, r);
  }
  for (const c of ["SUBMIT_REJECTED", "PLUGIN_SIMULATION_ONLY", "GUARD_BLOCKED", "ABORTED_UPSTREAM_CANCELLED", "OUT_OF_STOCK", "PAGE_LOAD_FAILED", "CAPTCHA_ENCOUNTERED"]) {
    assert.notEqual(codeLabel(c), c, c);
  }
  assert.equal(ACCOUNT_LEVEL_CODES.has("PLUGIN_SIMULATION_ONLY"), true);
});

test("续租节奏 = min(上限, 有效期 / 3)；「约 N 分钟」取自有效期", () => {
  assert.equal(beatInterval(35_000, 180), 35_000);
  assert.equal(beatInterval(35_000, 30), 10_000);
  assert.equal(beatInterval(35_000, null), 35_000);
  assert.equal(leaseMinutes(180), 3);
  assert.equal(leaseMinutes(20), 1);
  assert.equal(leaseMinutes(null), 3);
});
