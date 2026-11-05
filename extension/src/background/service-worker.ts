/** MV3 后台：配对、心跳、身份上报、标签页租约、定时触发、协议请求转发。
 *
 * **这里不跑任何页面动作，也不跑执行引擎。** 引擎在亚马逊标签页的内容脚本里跑
 * （后续真实驱动需要页面），它的每一次协议请求经由这里转发：设备令牌只在后台使用。
 *
 * 服务端工作的生命周期教训（移植自早期实现）：
 *   · 后台空闲约 30 秒被回收、模块级变量归零 → 租约、当前批次、去重记录全放 chrome.storage；
 *     模块级只留串行闸（同一个后台实例里只有一条事件循环，闸只需管住这一实例）。
 *   · 闹钟可能被浏览器重启清掉 → 每次唤醒都 ensureAlarms()；定时「到没到期」不依赖闹钟准时，
 *     由 schedule.dueSlots 根据当天记录判定，每分钟的心跳也会顺带检查一次。
 *   · 不申请 cookies 权限，不读也不上传 Cookie；身份只来自页面上读到的登录状态与 customerId。
 */

import { PluginClient, type ApiError, type ApiResult } from "../core/client.js";
import { codeLabel, stopReasonLabel } from "../core/codes.js";
import { Serial, withTimeout } from "../core/gates.js";
import {
  IDENTITY_DEDUPE_MS,
  IDENTITY_STATUS_TEXT,
  isFresh,
  maskCustomerId,
  sameIdentity,
  sanitizePageUrl,
  toIdentity,
  type IdentitySnapshot,
} from "../core/identity.js";
import { decideLease, releaseLease, type Lease } from "../core/lease.js";
import { appendLog, normalizeLevel, type LogLevel, type LogLine } from "../core/log.js";
import {
  KEYS,
  type ActiveRun,
  type ApiOp,
  type ConfigCache,
  type ConnectionState,
  type DeviceRecord,
  type DeviceView,
  type LeaseReply,
  type Msg,
  type RunViewRecord,
  type ScheduledReply,
  type StateSnapshot,
  type TabMsg,
} from "../core/messages.js";
import { dayState, dueSlots, markSlots, nextFirePlan, toLocalEpoch, type ScheduleDayState } from "../core/schedule.js";
import {
  isValidPairingCode,
  normalizeBrowser,
  normalizeDeviceName,
  normalizePairingCode,
  normalizeServerUrl,
  normalizeSettings,
  validateServerUrl,
  type Settings,
} from "../core/settings.js";
import { normalizeOutcome } from "../core/simulate.js";
import { parseIsoMs } from "../core/time.js";
import type { IdentityStatus, Trigger } from "../core/types.js";
import { lget, lremove, lset, sget, sremove, sset } from "./storage.js";

const EXT_VERSION = chrome.runtime.getManifest().version;

const ALARM_HEARTBEAT = "pc.heartbeat";
const ALARM_RUN_BEAT = "pc.runBeat";
const ALARM_SCHED_PREFIX = "pc.sched:";

/** 引擎超过这么久没有发过任何协议请求，后台就不再替它补发批次心跳（引擎可能卡死了，
 *  让租约自然到期、由服务端清扫，比替一个不动的引擎续命安全）。 */
const ENGINE_SILENT_MS = 150_000;
/** 定时触发等内容脚本答复的上界（开始批次那一步，含身份刷新）。 */
const SCHEDULE_REPLY_MS = 90_000;

const logGate = new Serial();
const leaseGate = new Serial();
const schedGate = new Serial();
const hbGate = new Serial();
const runGate = new Serial();

// ── 基础读写 ─────────────────────────────────────────────────────────────

/** 输入：级别与文本 → 输出：无。追加一行日志（最近 50 行），串行写入避免互相覆盖。 */
async function log(level: LogLevel, text: string): Promise<void> {
  await logGate.run(async () => {
    const lines = await lget<LogLine[]>(KEYS.logs);
    await lset(KEYS.logs, appendLog(lines, { at: Date.now(), level, text }));
  });
}

async function getSettings(): Promise<Settings> {
  return normalizeSettings(await lget(KEYS.settings));
}

async function getDevice(): Promise<DeviceRecord | null> {
  const d = await lget<DeviceRecord>(KEYS.device);
  return d && typeof d.token === "string" && d.token !== "" ? d : null;
}

function deviceView(d: DeviceRecord | null): DeviceView {
  if (!d) {
    return { paired: false, device_id: null, device_name: null, browser: null, workspace_name: null, server_url: null, paired_at: null };
  }
  return {
    paired: true,
    device_id: d.device_id,
    device_name: d.device_name,
    browser: d.browser,
    workspace_name: d.workspace_name,
    server_url: d.server_url,
    paired_at: d.paired_at,
  };
}

async function makeClient(): Promise<PluginClient | null> {
  const d = await getDevice();
  if (!d) return null;
  return new PluginClient({ baseUrl: d.server_url, token: d.token, extVersion: EXT_VERSION });
}

/** 输入：缓存的配置 → 输出：按服务端时钟校正后的「现在」。 */
function serverNow(cfg: ConfigCache | null | undefined): number {
  const off = cfg && Number.isFinite(cfg.clock_offset_ms) ? cfg.clock_offset_ms : 0;
  return Date.now() + off;
}

async function tabAlive(tabId: number): Promise<boolean> {
  try {
    return !!(await chrome.tabs.get(tabId));
  } catch {
    return false; // chrome.tabs.get 对不存在的标签页明确抛错，这是硬事实
  }
}

const localError = (code: string): ApiError => ({ kind: "http", status: null, code, message: codeLabel(code), detail: null });

async function snapshot(): Promise<StateSnapshot> {
  const [settings, device, config, connection, identity, logs, schedule, run] = await Promise.all([
    getSettings(),
    getDevice(),
    lget<ConfigCache>(KEYS.config),
    lget<ConnectionState>(KEYS.connection),
    lget<IdentitySnapshot>(KEYS.identity),
    lget<LogLine[]>(KEYS.logs),
    lget<ScheduleDayState>(KEYS.schedule),
    lget<RunViewRecord>(KEYS.runView),
  ]);
  return {
    settings,
    device: deviceView(device),
    config: device ? config ?? null : null,
    connection: device ? connection ?? null : null,
    identity: identity ?? null,
    identity_status: (device && config?.heartbeat.identity_status) || "unknown",
    logs: Array.isArray(logs) ? logs : [],
    schedule: schedule ?? null,
    run: run ?? null,
    ext_version: EXT_VERSION,
  };
}

// ── 闹钟 ─────────────────────────────────────────────────────────────────

async function ensureAlarms(): Promise<void> {
  try {
    const hb = await chrome.alarms.get(ALARM_HEARTBEAT);
    if (!hb) await chrome.alarms.create(ALARM_HEARTBEAT, { periodInMinutes: 1, delayInMinutes: 0.1 });
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (ar && !(await chrome.alarms.get(ALARM_RUN_BEAT))) {
      await chrome.alarms.create(ALARM_RUN_BEAT, { periodInMinutes: 0.5 });
    }
  } catch {
    // 闹钟接口不可用时，下一次唤醒再试
  }
}

/** 按当前配置给每个执行时间设一个一次性闹钟（北京时间换算成本机时刻）。只在变化时重建。 */
async function planSchedule(): Promise<void> {
  const cfg = await lget<ConfigCache>(KEYS.config);
  const acc = cfg?.heartbeat.account ?? null;
  const wanted = new Map<string, number>();
  if (acc && acc.purchase_mode === "scheduled") {
    for (const p of nextFirePlan(acc.schedule_times, serverNow(cfg))) {
      wanted.set(ALARM_SCHED_PREFIX + p.slot, toLocalEpoch(p.at, cfg?.clock_offset_ms ?? 0));
    }
  }
  const all = await chrome.alarms.getAll();
  for (const a of all) {
    if (a.name.startsWith(ALARM_SCHED_PREFIX) && !wanted.has(a.name)) await chrome.alarms.clear(a.name);
  }
  for (const [name, when] of wanted) {
    const ex = all.find((a) => a.name === name);
    if (!ex || Math.abs(ex.scheduledTime - when) > 30_000) await chrome.alarms.create(name, { when });
  }
}

// ── 心跳 ─────────────────────────────────────────────────────────────────

/** 输入：触发原因 → 输出：这次心跳是否成功。成功时缓存配置、校正时钟、重排定时闹钟。 */
async function heartbeat(reason: string): Promise<boolean> {
  return hbGate.run(async () => {
    const client = await makeClient();
    if (!client) return false;
    const ident = await lget<IdentitySnapshot>(KEYS.identity);
    // 只捎带新鲜的身份：几小时前读的快照不能每分钟被「重新上报」一次。
    const fresh = ident && isFresh(ident, Date.now()) ? toIdentity(ident) : null;
    const prevConn = await lget<ConnectionState>(KEYS.connection);
    const prevCfg = await lget<ConfigCache>(KEYS.config);
    const sentAt = Date.now();
    const r = await client.heartbeat(fresh);
    const now = Date.now();
    if (r.ok) {
      const serverMs = parseIsoMs(r.data.server_time);
      // 往返时间的一半近似为单程延迟。
      const offset = serverMs === null ? 0 : serverMs - Math.round((sentAt + now) / 2);
      await lset(KEYS.config, { heartbeat: r.data, received_at: now, clock_offset_ms: offset } satisfies ConfigCache);
      await lset(KEYS.connection, { ok: true, at: now, code: null, message: null } satisfies ConnectionState);
      if (!prevConn?.ok) await log("ok", `已连接服务端（${reason}）`);
      const prevAcc = prevCfg?.heartbeat.account ?? null;
      if ((prevAcc?.id ?? null) !== (r.data.account?.id ?? null)) {
        await log("info", r.data.account ? `绑定账号：${r.data.account.name}` : "本设备还没有绑定买家账号");
      }
      if (prevCfg?.heartbeat.identity_status !== r.data.identity_status) {
        const t = IDENTITY_STATUS_TEXT[r.data.identity_status];
        await log(r.data.identity_status === "match" ? "ok" : "warn", `身份状态：${t.short}（${t.long}）`);
      }
      if (prevAcc && r.data.account && prevAcc.execution_paused !== r.data.account.execution_paused) {
        await log(
          r.data.account.execution_paused ? "warn" : "ok",
          r.data.account.execution_paused ? `账号已暂停执行${r.data.account.pause_reason ? `：${r.data.account.pause_reason}` : ""}` : "账号已恢复执行",
        );
      }
      await planSchedule().catch(() => undefined);
      return true;
    }
    await lset(KEYS.connection, { ok: false, at: now, code: r.error.code, message: r.error.message } satisfies ConnectionState);
    if (prevConn?.ok !== false || prevConn.code !== r.error.code) await log("err", `心跳失败：${r.error.message}`);
    return false;
  });
}

async function heartbeatAndSchedule(reason: string): Promise<void> {
  if (await heartbeat(reason)) await checkSchedule(reason);
}

// ── 身份 ─────────────────────────────────────────────────────────────────

/** 输入：内容脚本读到的身份快照、是否强制上报 → 输出：服务端给的身份状态（拿不到为 null）。
 *  同一份身份 60 秒内不重复单独上报（心跳仍会每分钟捎带新鲜快照）。 */
async function onIdentity(raw: IdentitySnapshot | null, force: boolean): Promise<{ identity_status: IdentityStatus | null }> {
  if (!raw || typeof raw.signed_in !== "boolean") return { identity_status: null };
  const snap: IdentitySnapshot = {
    signed_in: raw.signed_in,
    customer_id: typeof raw.customer_id === "string" && raw.signed_in ? raw.customer_id : null,
    site: "US",
    page_url: sanitizePageUrl(raw.page_url),
    at: Date.now(),
  };
  const prev = await lget<IdentitySnapshot>(KEYS.identity);
  await lset(KEYS.identity, snap);
  if (!sameIdentity(prev, snap)) {
    await log(
      snap.signed_in ? "info" : "warn",
      snap.signed_in ? `识别到亚马逊登录（买家号 ID ${maskCustomerId(snap.customer_id)}）` : "本浏览器未登录亚马逊",
    );
  }
  const client = await makeClient();
  if (!client) return { identity_status: null };

  const sig = JSON.stringify([snap.signed_in, snap.customer_id]);
  const sent = await sget<{ sig: string; at: number }>(KEYS.identitySent);
  if (!force && sent && sent.sig === sig && Date.now() - sent.at < IDENTITY_DEDUPE_MS) {
    const cfg = await lget<ConfigCache>(KEYS.config);
    return { identity_status: cfg?.heartbeat.identity_status ?? null };
  }

  const r = await client.identity(toIdentity(snap));
  if (r.ok) {
    await sset(KEYS.identitySent, { sig, at: Date.now() });
    if (r.data.identity_status) await setIdentityStatus(r.data.identity_status);
  } else if (r.error.kind === "http" && (r.error.status === 404 || r.error.status === 405 || r.error.status === 422)) {
    // 服务端没有单独的身份接口：改由心跳捎带（契约 §5.2 的 identity?）。
    await sset(KEYS.identitySent, { sig, at: Date.now() });
    await heartbeat("身份上报");
  } else {
    await log("warn", `身份上报失败：${r.error.message}`);
  }
  // 强制刷新时再拉一次心跳，拿到服务端按最新身份算出的状态与配置。
  if (force) await heartbeat("刷新身份");
  const cfg = await lget<ConfigCache>(KEYS.config);
  return { identity_status: cfg?.heartbeat.identity_status ?? null };
}

async function setIdentityStatus(st: IdentityStatus): Promise<void> {
  const cfg = await lget<ConfigCache>(KEYS.config);
  if (!cfg || cfg.heartbeat.identity_status === st) return;
  await lset(KEYS.config, { ...cfg, heartbeat: { ...cfg.heartbeat, identity_status: st } });
  const t = IDENTITY_STATUS_TEXT[st];
  await log(st === "match" ? "ok" : "warn", `身份状态：${t.short}（${t.long}）`);
}

// ── 配对 ─────────────────────────────────────────────────────────────────

async function onPair(m: Extract<Msg, { type: "pc.pair" }>): Promise<{ ok: boolean; message: string }> {
  if (await getDevice()) return { ok: false, message: "配对失败：本设备已配对，请先解除配对" };
  const url = normalizeServerUrl(String(m.serverUrl ?? ""));
  const v = validateServerUrl(url);
  if (!v.ok) return { ok: false, message: `配对失败：${v.reason}` };
  const code = normalizePairingCode(String(m.code ?? ""));
  if (!isValidPairingCode(code)) return { ok: false, message: "配对失败：配对码应为 8 位字母或数字" };
  const name = normalizeDeviceName(String(m.deviceName ?? ""));
  if (!name) return { ok: false, message: "配对失败：请填写设备名称" };
  const browser = normalizeBrowser(m.browser);

  const client = new PluginClient({ baseUrl: url, extVersion: EXT_VERSION });
  const r = await client.pair(code, name, browser);
  if (!r.ok) {
    await log("err", `配对失败：${r.error.message}`);
    return { ok: false, message: `配对失败：${r.error.message}` };
  }
  const rec: DeviceRecord = {
    token: r.data.device_token,
    device_id: r.data.device_id,
    device_name: name,
    browser,
    workspace_name: r.data.workspace_name,
    server_url: url,
    paired_at: Date.now(),
  };
  await lset(KEYS.device, rec);
  const s = await getSettings();
  await lset(KEYS.settings, { ...s, serverUrl: url });
  await lremove([KEYS.config, KEYS.connection, KEYS.schedule]);
  await sremove(KEYS.identitySent);
  await log("ok", `配对成功：设备「${name}」${r.data.workspace_name ? `（工作区 ${r.data.workspace_name}）` : ""}`);
  await heartbeatAndSchedule("配对完成");
  return { ok: true, message: "配对成功" };
}

async function onUnpair(): Promise<{ ok: boolean; message: string }> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (ar && (await tabAlive(ar.tab_id))) return { ok: false, message: "解除配对失败：正在执行，结束后再解除" };
  await lremove([KEYS.device, KEYS.config, KEYS.connection, KEYS.schedule, KEYS.runView]);
  await sremove([KEYS.identitySent, KEYS.activeRun]);
  for (const a of await chrome.alarms.getAll()) {
    if (a.name.startsWith(ALARM_SCHED_PREFIX) || a.name === ALARM_RUN_BEAT) await chrome.alarms.clear(a.name);
  }
  await log("warn", "已解除配对，设备令牌已从本浏览器删除（如需让服务端也作废，请在控制台撤销该设备）");
  return { ok: true, message: "已解除配对" };
}

async function onSaveSettings(patch: Partial<Settings>): Promise<{ ok: boolean; message: string }> {
  const cur = await getSettings();
  const next: Settings = { ...cur };
  if (patch.serverUrl !== undefined) {
    const url = normalizeServerUrl(String(patch.serverUrl));
    const v = validateServerUrl(url);
    if (!v.ok) return { ok: false, message: `保存失败：${v.reason}` };
    const dev = await getDevice();
    if (dev && dev.server_url !== url) return { ok: false, message: "保存失败：已配对时不能修改服务端地址，请先解除配对" };
    next.serverUrl = url;
  }
  if (patch.simulationOutcome !== undefined) {
    next.simulationOutcome = normalizeOutcome(patch.simulationOutcome);
    if (next.simulationOutcome !== cur.simulationOutcome) await log("info", `模拟结局改为：${next.simulationOutcome}`);
  }
  await lset(KEYS.settings, next);
  return { ok: true, message: "已保存" };
}

// ── 标签页租约 ───────────────────────────────────────────────────────────

/** 输入：要租约的标签页、它是否在执行 → 输出：给不给。「读 → 裁决 → 写」整段串行。 */
async function onLease(tabId: number, busy: boolean): Promise<LeaseReply> {
  return leaseGate.run(async () => {
    const cur = (await sget<Lease>(KEYS.tabLease)) ?? null;
    // 只有「过期了、而持有者说自己在执行」那一格才需要问持有者还在不在。
    const alive = cur && cur.tabId !== tabId && cur.busy ? await tabAlive(cur.tabId) : true;
    const v = decideLease(cur, { tabId, busy, now: Date.now(), holderAlive: alive });
    if (v.granted && v.next) {
      await sset(KEYS.tabLease, v.next);
      if (v.reason === "taken-over" || v.reason === "holder-gone") {
        await log("info", `执行租约换到标签页 ${tabId}${v.reason === "holder-gone" ? "（原持有者的标签页已关闭）" : ""}`);
      }
    }
    return { granted: v.granted, held_by: v.granted ? null : cur?.tabId ?? null, reason: v.reason };
  });
}

async function onLeaseRelease(tabId: number): Promise<void> {
  await leaseGate.run(async () => {
    const cur = (await sget<Lease>(KEYS.tabLease)) ?? null;
    const next = releaseLease(cur, tabId);
    if (next !== cur) await sremove(KEYS.tabLease);
  });
}

// ── 执行批次跟踪（后台补发批次心跳）────────────────────────────────────────

async function clearActiveRun(): Promise<void> {
  await sremove(KEYS.activeRun);
  try {
    await chrome.alarms.clear(ALARM_RUN_BEAT);
  } catch {
    // 忽略
  }
}

/** 引擎走后台的每一次协议请求都更新 touched_at（10 秒内只写一次）。 */
async function touchRun(tabId: number | undefined): Promise<void> {
  await runGate.run(async () => {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (!ar || ar.tab_id !== tabId) return;
    if (Date.now() - ar.touched_at < 10_000) return;
    await sset(KEYS.activeRun, { ...ar, touched_at: Date.now() });
  });
}

/** 后台补发的批次心跳。主心跳由引擎自己发（35 秒一次）；这一路只兜底，
 *  而且只在执行标签页还在、引擎最近还在动时才发 —— 不替一个卡死的引擎续命。 */
async function onRunBeat(): Promise<void> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (!ar) {
    await chrome.alarms.clear(ALARM_RUN_BEAT);
    return;
  }
  if (!(await tabAlive(ar.tab_id))) {
    await clearActiveRun();
    await log("warn", `执行中的标签页已关闭：批次 #${ar.run_id} 不再续租，服务端会在租约到期后处理在途尝试`);
    return;
  }
  if (Date.now() - ar.touched_at > ENGINE_SILENT_MS) return;
  const client = await makeClient();
  if (!client) return;
  const r = await client.runHeartbeat(ar.run_id);
  let stop: string | null = null;
  if (r.ok && r.data.stop) stop = `服务端要求停止：${stopReasonLabel(r.data.stop_reason)}`;
  else if (!r.ok && r.error.kind === "http") stop = `执行权已失效：${r.error.message}`;
  if (stop) {
    const msg: TabMsg = { type: "pc.stopRun", reason: stop };
    await chrome.tabs.sendMessage(ar.tab_id, msg).catch(() => undefined);
  }
}

/** 输入：协议操作、参数、发起的标签页 → 输出：ApiResult（可序列化，原样回给引擎）。 */
async function onApi(op: ApiOp, args: unknown[], tabId: number | undefined): Promise<ApiResult<unknown>> {
  const client = await makeClient();
  if (!client) return { ok: false, error: localError("NOT_PAIRED") };
  const a = Array.isArray(args) ? args : [];
  const num = (i: number): number => Number(a[i]);
  let r: ApiResult<unknown>;
  switch (op) {
    case "startRun": {
      if (tabId === undefined) return { ok: false, error: localError("VALIDATION") };
      const trigger: Trigger = a[0] === "scheduled" ? "scheduled" : "manual";
      r = await client.startRun(trigger);
      if (r.ok) {
        const runId = (r.data as { run_id: number }).run_id;
        const now = Date.now();
        await sset(KEYS.activeRun, { run_id: runId, tab_id: tabId, trigger, started_at: now, touched_at: now } satisfies ActiveRun);
        await chrome.alarms.create(ALARM_RUN_BEAT, { periodInMinutes: 0.5 });
      }
      return r;
    }
    case "runHeartbeat":
      r = await client.runHeartbeat(num(0));
      break;
    case "next":
      r = await client.next(num(0));
      break;
    case "progress":
      r = await client.progress(num(0), String(a[1] ?? ""), (a[2] as Record<string, unknown>) ?? {});
      break;
    case "guard":
      r = await client.guard(num(0), a[1] as Parameters<PluginClient["guard"]>[1]);
      break;
    case "submitting":
      r = await client.submitting(num(0));
      break;
    case "verifying":
      r = await client.verifying(num(0), String(a[1] ?? ""));
      break;
    case "result":
      r = await client.result(num(0), a[1] as Parameters<PluginClient["result"]>[1]);
      break;
    case "finishRun": {
      r = await client.finishRun(num(0), String(a[1] ?? ""));
      const ar = await sget<ActiveRun>(KEYS.activeRun);
      if (ar && ar.run_id === num(0)) await clearActiveRun();
      return r;
    }
    default:
      return { ok: false, error: localError("VALIDATION") };
  }
  await touchRun(tabId);
  return r;
}

async function onRunView(m: Extract<Msg, { type: "pc.runView" }>, tabId: number | undefined): Promise<void> {
  const rec: RunViewRecord = { active: !!m.active, tab_id: tabId ?? null, view: m.view ?? null, updated_at: Date.now() };
  await lset(KEYS.runView, rec);
  if (!m.active) {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (ar && ar.tab_id === tabId) await clearActiveRun(); // 引擎已结束（含中断）：后台也不再补发心跳
  }
}

// ── 定时触发 ─────────────────────────────────────────────────────────────

async function amazonTabs(): Promise<chrome.tabs.Tab[]> {
  try {
    const tabs = await chrome.tabs.query({ url: "https://www.amazon.com/*" });
    return tabs
      .filter((t) => t.id !== undefined && !t.discarded)
      .sort((x, y) => Number(!!y.active) - Number(!!x.active) || (y.lastAccessed ?? 0) - (x.lastAccessed ?? 0));
  } catch {
    return [];
  }
}

/** 输入：触发原因 → 输出：无。检查今天有没有到点、还没处理的执行时间；有就交给一个亚马逊标签页执行。
 *
 *  错过的时间点当天内补执行一次（方案 v2 默认值 4）；没有打开的亚马逊页面时只记一次「等待环境」，
 *  之后任何一次检查（每分钟心跳、打开亚马逊页面）再补。 */
async function checkSchedule(reason: string): Promise<void> {
  await schedGate.run(async () => {
    if (!(await getDevice())) return;
    const cfg = await lget<ConfigCache>(KEYS.config);
    const acc = cfg?.heartbeat.account;
    if (!acc || acc.purchase_mode !== "scheduled") return;
    const now = serverNow(cfg);
    const st = dayState(await lget<ScheduleDayState>(KEYS.schedule), now);
    const due = dueSlots(acc.schedule_times, now, st);
    if (due.length === 0) {
      await lset(KEYS.schedule, st);
      return;
    }
    const label = `定时 ${due.join("、")}`;
    const done = async (level: LogLevel, text: string): Promise<void> => {
      await lset(KEYS.schedule, markSlots(st, due, now));
      await log(level, `${label}：${text}`);
    };

    if (acc.execution_paused) return done("warn", "账号已暂停执行，今天这次不执行");
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (ar && (await tabAlive(ar.tab_id))) return done("info", "本浏览器已在执行，并入当前批次");

    for (const t of await amazonTabs()) {
      let reply: ScheduledReply | null = null;
      try {
        const msg: TabMsg = { type: "pc.startScheduled", slots: due };
        reply = (await withTimeout(chrome.tabs.sendMessage(t.id!, msg), SCHEDULE_REPLY_MS, "等待页面答复超时")) as ScheduledReply | null;
      } catch {
        reply = null; // 这个标签页没有内容脚本（插件装好之前就打开了）或答复超时
      }
      if (!reply || typeof reply.result !== "string") continue;
      if (reply.result === "network" || reply.result === "not_ready") {
        await log("warn", `${label}：暂未开始（${reply.message}），稍后自动重试`);
        return;
      }
      const level: LogLevel = reply.result === "started" ? "ok" : "warn";
      return done(level, reply.message);
    }

    const fresh = due.filter((s) => !st.waiting_logged.includes(s));
    if (fresh.length) {
      await lset(KEYS.schedule, markSlots(st, fresh, now, "waiting_logged"));
      await log("warn", `定时 ${fresh.join("、")} 已到点：没有打开的亚马逊页面，等待环境（今天内打开亚马逊页面后自动补执行；触发：${reason}）`);
    }
  });
}

// ── 事件接线 ─────────────────────────────────────────────────────────────

async function handle(msg: Msg, sender: chrome.runtime.MessageSender): Promise<unknown> {
  const tabId = sender.tab?.id;
  switch (msg?.type) {
    case "pc.getState":
      return snapshot();
    case "pc.heartbeatNow":
      await heartbeatAndSchedule("手动刷新");
      return snapshot();
    case "pc.pair":
      return onPair(msg);
    case "pc.unpair":
      return onUnpair();
    case "pc.saveSettings":
      return onSaveSettings(msg.patch ?? {});
    case "pc.identity":
      return onIdentity(msg.identity, !!msg.force);
    case "pc.api":
      return onApi(msg.op, msg.args, tabId);
    case "pc.lease":
      if (tabId === undefined) return { granted: false, held_by: null, reason: "held" } satisfies LeaseReply;
      return onLease(tabId, !!msg.busy);
    case "pc.leaseRelease":
      if (tabId !== undefined) await onLeaseRelease(tabId);
      return { ok: true };
    case "pc.runView":
      await onRunView(msg, tabId);
      return { ok: true };
    case "pc.log":
      await log(normalizeLevel(msg.level), String(msg.text ?? "").slice(0, 500));
      return { ok: true };
    case "pc.openOptions":
      await chrome.runtime.openOptionsPage().catch(() => undefined);
      return { ok: true };
    case "pc.hello":
      // 亚马逊页面打开了 = 「环境上线」：顺带检查有没有今天错过的定时。
      void checkSchedule("打开亚马逊页面");
      return { ok: true };
    default:
      return { ok: false, message: "未知消息" };
  }
}

chrome.runtime.onMessage.addListener((msg: Msg, sender, respond) => {
  handle(msg, sender).then(respond, (e: unknown) => respond({ ok: false, message: e instanceof Error ? e.message : String(e) }));
  return true; // 异步答复：通道保持打开
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === ALARM_HEARTBEAT) void heartbeatAndSchedule("定时心跳");
  else if (a.name === ALARM_RUN_BEAT) void onRunBeat();
  else if (a.name.startsWith(ALARM_SCHED_PREFIX)) {
    void (async () => {
      await checkSchedule(`闹钟 ${a.name.slice(ALARM_SCHED_PREFIX.length)}`);
      await planSchedule(); // 一次性闹钟响过之后排下一天
    })();
  }
});

chrome.runtime.onInstalled.addListener((details) => {
  void ensureAlarms();
  if (details.reason === "install") void chrome.runtime.openOptionsPage().catch(() => undefined);
  void heartbeatAndSchedule("插件安装或更新");
});

chrome.runtime.onStartup.addListener(() => {
  // 浏览器重启：上一次会话里的执行不可能还活着。
  void lremove(KEYS.runView);
  void ensureAlarms();
  void heartbeatAndSchedule("浏览器启动");
});

// 拿着租约的标签页被关掉时立刻释放，不用等有效期到。
chrome.tabs.onRemoved.addListener((tabId) => {
  void onLeaseRelease(tabId);
  void (async () => {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (ar && ar.tab_id === tabId) {
      await clearActiveRun();
      const rv = await lget<RunViewRecord>(KEYS.runView);
      if (rv?.active && rv.tab_id === tabId) await lset(KEYS.runView, { ...rv, active: false, updated_at: Date.now() });
      await log("warn", `执行中的标签页已关闭：批次 #${ar.run_id} 不再续租，服务端会在租约到期（约 3 分钟）后处理在途尝试`);
    }
  })();
});

// 每次后台被唤醒都确认一次闹钟还在（浏览器重启可能清掉闹钟）。
void ensureAlarms();
