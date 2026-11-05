/** MV3 后台：配对、心跳、身份对账与上报、标签页租约、定时触发、协议请求转发、结果回执补交、
 *  接管执行页面已经没了的批次。
 *
 * **这里不跑任何页面动作，也不跑执行引擎。** 引擎在亚马逊标签页的内容脚本里跑
 * （后续真实驱动需要页面），它的每一次协议请求经由这里转发：设备令牌只在后台使用
 * （存在扩展源下的 IndexedDB，见 secrets.ts；内容脚本读得到 chrome.storage.local，读不到那里）。
 *
 * 服务端工作的生命周期教训（移植自早期实现）：
 *   · 后台空闲约 30 秒被回收、模块级变量归零 → 租约、当前批次、身份读数、回执全放 chrome.storage；
 *     模块级只留串行闸（同一个后台实例里只有一条事件循环，闸只需管住这一实例）。
 *   · 闹钟可能被浏览器重启清掉 → 每次唤醒都 ensureAlarms()；定时「到没到期」不依赖闹钟准时，
 *     由 schedule.dueSlots 根据当天记录判定，每分钟的心跳也会顺带检查一次。
 *   · 执行页面刷新、跳转、关闭时引擎随之消失、来不及说一声 → 新页面的 pc.hello、旧页面的 pc.bye、
 *     标签页关闭、对 pc.ping 不再答复，任何一个都让后台接管批次（core/custody.ts）。
 *   · 不申请 cookies 与 tabs 权限，不读也不上传 Cookie；身份只来自页面上读到的登录状态与 customerId，
 *     多个标签页的读数按页面加载先后对账（core/identity.reconcileIdentity）。
 */

import { PluginClient, pairFailureText, type ApiError, type ApiResult } from "../core/client.js";
import { codeLabel, stopReasonLabel } from "../core/codes.js";
import { CUSTODY_MAX_MS, custodyOverBudget, custodyStep, helloEndsRun, pingSaysAlive } from "../core/custody.js";
import { leaseMinutes } from "../core/engine.js";
import { Serial, withTimeout } from "../core/gates.js";
import {
  IDENTITY_DEDUPE_MS,
  IDENTITY_STATUS_TEXT,
  isFresh,
  maskCustomerId,
  normalizeIdentityStore,
  observeIdentity,
  reconcileIdentity,
  retireIdentityTab,
  sameIdentity,
  sanitizePageUrl,
  toIdentity,
  type IdentityObservation,
  type IdentitySnapshot,
  type IdentityStore,
  type ReconciledIdentity,
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
  type PrepareReply,
  type RefreshIdentityReply,
  type RunViewRecord,
  type ScheduledReply,
  type StateSnapshot,
  type TabMsg,
} from "../core/messages.js";
import {
  classifyResultReply,
  dropExpired,
  dueReceipts,
  isLateReceipt,
  pendingForRun,
  putReceipt,
  receiptViews,
  settleReceipt,
  type ReceiptStore,
  type ReplyVerdict,
} from "../core/receipts.js";
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
import { SIM_OUTCOMES, normalizeOutcome } from "../core/simulate.js";
import { parseIsoMs } from "../core/time.js";
import type { HeartbeatOut, IdentityStatus, ResultIn, Trigger } from "../core/types.js";
import { deleteToken, readToken, writeToken } from "./secrets.js";
import { lget, lremove, lset, sget, sremove, sset } from "./storage.js";

const EXT_VERSION = chrome.runtime.getManifest().version;

const ALARM_HEARTBEAT = "pc.heartbeat";
const ALARM_FAST_HEARTBEAT = "pc.heartbeatFast";
const ALARM_RUN_BEAT = "pc.runBeat";
const ALARM_RECEIPTS = "pc.receipts";
const ALARM_SCHED_PREFIX = "pc.sched:";

/** 引擎超过这么久没有发过任何协议请求，后台就不再替它补发批次心跳（引擎可能卡死了，
 *  让租约自然到期、由服务端清扫，比替一个不动的引擎续命安全）。 */
const ENGINE_SILENT_MS = 150_000;
/** 定时触发等内容脚本答复的上界（开始批次那一步，含身份刷新）。 */
const SCHEDULE_REPLY_MS = 90_000;
/** 已配对、还没绑定账号时的心跳间隔（分钟）：控制台里一绑定，插件这边很快就知道。
 *  chrome.alarms 对打包安装的扩展最短 30 秒一次（加载已解压的扩展时按这里的 10 秒）；
 *  看得见的面板、设置页另有 10 秒一次的同步。 */
const FAST_HEARTBEAT_MIN = 10 / 60;
/** 问执行页面「引擎还在吗」、请页面重读身份的答复上界。 */
const PING_TIMEOUT_MS = 5_000;
const PING_TIMEOUT_TEXT = "等待页面答复超时";
/** 打开或回到页面时，上一次心跳早于这个间隔才同步。 */
const SYNC_MIN_AGE_MS = 5_000;
/** 缓存的配置还说「没绑定账号」时不节流：绑定往往就发生在上一次心跳之后几百毫秒（配对完马上去控制台绑定），
 *  那次心跳的答复已经过时。心跳闸会把并发的几次排成一队，不会连发成一片。 */
const UNBOUND_SYNC_MIN_AGE_MS = 0;

const logGate = new Serial();
const leaseGate = new Serial();
const schedGate = new Serial();
const hbGate = new Serial();
const runGate = new Serial();
const receiptGate = new Serial();
const drainGate = new Serial();
const identityGate = new Serial();

type PairedDevice = DeviceRecord & { token: string };

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

/** 输入：无 → 输出：配对好的设备（含令牌）；没配对或令牌读不到为 null。
 *  早期版本把令牌写在 storage.local 的设备记录里：读到时搬进 IndexedDB 并从记录里删掉。 */
async function getDevice(): Promise<PairedDevice | null> {
  const rec = await lget<DeviceRecord>(KEYS.device);
  if (!rec || typeof rec !== "object" || rec.device_id === undefined || rec.device_id === null) return null;
  if (typeof rec.token === "string" && rec.token !== "") {
    const legacy = rec.token;
    if (await writeToken(legacy)) {
      const rest: DeviceRecord = { ...rec };
      delete rest.token;
      await lset(KEYS.device, rest);
    }
    return { ...rec, token: legacy };
  }
  const token = await readToken();
  return token ? { ...rec, token } : null;
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
  const [settings, device, config, connection, identity, logs, schedule, run, receipts] = await Promise.all([
    getSettings(),
    getDevice(),
    lget<ConfigCache>(KEYS.config),
    lget<ConnectionState>(KEYS.connection),
    lget<ReconciledIdentity>(KEYS.identity),
    lget<LogLine[]>(KEYS.logs),
    lget<ScheduleDayState>(KEYS.schedule),
    lget<RunViewRecord>(KEYS.runView),
    lget<ReceiptStore>(KEYS.receipts),
  ]);
  return {
    settings,
    device: deviceView(device),
    config: device ? config ?? null : null,
    connection: device ? connection ?? null : null,
    identity: identity ?? null,
    identity_status: (device && config?.heartbeat.identity_status) || "unknown",
    receipts: device ? receiptViews(receipts) : [],
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
    if (await sget<ActiveRun>(KEYS.activeRun)) await ensureRunBeatAlarm();
    if (Object.keys((await lget<ReceiptStore>(KEYS.receipts)) ?? {}).length) await ensureReceiptAlarm();
    const cfg = await lget<ConfigCache>(KEYS.config);
    if (await getDevice()) await syncFastHeartbeat(cfg?.heartbeat ?? null);
  } catch {
    // 闹钟接口不可用时，下一次唤醒再试
  }
}

async function ensureRunBeatAlarm(): Promise<void> {
  if (!(await chrome.alarms.get(ALARM_RUN_BEAT))) await chrome.alarms.create(ALARM_RUN_BEAT, { periodInMinutes: 0.5 });
}

async function ensureReceiptAlarm(): Promise<void> {
  if (!(await chrome.alarms.get(ALARM_RECEIPTS))) await chrome.alarms.create(ALARM_RECEIPTS, { periodInMinutes: 0.5 });
}

/** 已配对、还没绑定账号（心跳里 account 为 null）时开快心跳；绑定了就关掉。 */
async function syncFastHeartbeat(hb: HeartbeatOut | null): Promise<void> {
  const unbound = hb === null || hb.account === null;
  const ex = await chrome.alarms.get(ALARM_FAST_HEARTBEAT);
  if (unbound && !ex) {
    await chrome.alarms.create(ALARM_FAST_HEARTBEAT, { periodInMinutes: FAST_HEARTBEAT_MIN, delayInMinutes: FAST_HEARTBEAT_MIN });
  } else if (!unbound && ex) {
    await chrome.alarms.clear(ALARM_FAST_HEARTBEAT);
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

/** 输入：触发原因 → 输出：这次心跳是否成功。成功时缓存配置、校正时钟、重排定时闹钟、补交回执。 */
async function heartbeat(reason: string): Promise<boolean> {
  const ok = await hbGate.run(async () => {
    const client = await makeClient();
    if (!client) return false;
    await sset(KEYS.heartbeatTriedAt, Date.now());
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
        await log("info", r.data.account ? `绑定账号：${r.data.account.name}` : "本设备还没有绑定买家账号（在控制台绑定后几秒内自动同步）");
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
      await syncFastHeartbeat(r.data).catch(() => undefined);
      return true;
    }
    await lset(KEYS.connection, { ok: false, at: now, code: r.error.code, message: r.error.message } satisfies ConnectionState);
    if (prevConn?.ok !== false || prevConn.code !== r.error.code) await log("err", `心跳失败：${r.error.message}`);
    return false;
  });
  // 服务端连上了：顺带补交还没送到的回执（不等退避；不占心跳闸）。
  if (ok && Object.keys((await lget<ReceiptStore>(KEYS.receipts)) ?? {}).length) void drainReceipts({ ignoreBackoff: true });
  return ok;
}

async function heartbeatAndSchedule(reason: string): Promise<void> {
  if (await heartbeat(reason)) await checkSchedule(reason);
}

/** 输入：触发原因、间隔 → 输出：无。上一次心跳早于这个间隔就马上发一次（打开面板、回到页面、等待绑定）。
 *  缓存的配置还说没绑定账号时总是发：刚在控制台绑定完就打开或刷新页面，不能被之前那次心跳挡住。 */
async function heartbeatIfStale(reason: string, minAgeMs: number): Promise<void> {
  const cfg = await lget<ConfigCache>(KEYS.config);
  const unbound = !cfg || cfg.heartbeat.account === null;
  const age = unbound ? Math.min(minAgeMs, UNBOUND_SYNC_MIN_AGE_MS) : minAgeMs;
  const tried = await sget<number>(KEYS.heartbeatTriedAt);
  if (typeof tried === "number" && Date.now() - tried >= 0 && Date.now() - tried < age) return;
  await heartbeatAndSchedule(reason);
}

// ── 身份 ─────────────────────────────────────────────────────────────────

async function boundCustomerId(): Promise<string | null> {
  const cfg = await lget<ConfigCache>(KEYS.config);
  return cfg?.heartbeat.account?.amazon_customer_id ?? null;
}

/** 输入：内容脚本送来的读数、发送的标签页、当前时刻 → 输出：规范化后的一份读数；形状不对为 null。 */
function toObservation(raw: IdentitySnapshot | null | undefined, tabId: number, now: number): IdentityObservation | null {
  if (!raw || typeof raw.signed_in !== "boolean") return null;
  const loaded = typeof raw.loaded_at === "number" && Number.isFinite(raw.loaded_at) ? raw.loaded_at : now;
  return {
    tab_id: tabId,
    signed_in: raw.signed_in,
    customer_id: raw.signed_in && typeof raw.customer_id === "string" && raw.customer_id ? raw.customer_id : null,
    page_url: sanitizePageUrl(raw.page_url),
    // 页面的加载时刻不可能晚于现在：防止一个错乱的时钟让某一页永远「最新」。
    loaded_at: Math.min(loaded, now),
    read_at: now,
  };
}

async function editIdentityStore(fn: (s: IdentityStore) => IdentityStore): Promise<void> {
  const cur = normalizeIdentityStore(await sget(KEYS.identityObs));
  await sset(KEYS.identityObs, fn(cur));
}

/** 已经不在亚马逊页面上的标签页（跳到别的网站、被浏览器丢弃）：读数不再算「开着的」。
 *  查不了标签页时什么都不动 —— 「不知道」不能变成「都关了」。 */
async function retireGoneTabs(): Promise<void> {
  let tabs: chrome.tabs.Tab[];
  try {
    tabs = await chrome.tabs.query({ url: "https://www.amazon.com/*" });
  } catch {
    return;
  }
  const live = new Set(tabs.filter((t) => !t.discarded).map((t) => t.id));
  await editIdentityStore((s) => {
    let out = s;
    for (const k of Object.keys(s.live)) if (!live.has(Number(k))) out = retireIdentityTab(out, Number(k));
    return out;
  });
}

/** 对账、存下结论；返回结论（一份读数都没有时为 null，界面上保留上一次的结论）。 */
async function reconcileAndStore(): Promise<ReconciledIdentity | null> {
  await retireGoneTabs();
  const store = normalizeIdentityStore(await sget(KEYS.identityObs));
  const snap = reconcileIdentity(store, await boundCustomerId(), Date.now());
  if (!snap) return null;
  const prev = await lget<ReconciledIdentity>(KEYS.identity);
  await lset(KEYS.identity, snap);
  if (!sameIdentity(prev, snap) || !!prev?.conflict !== snap.conflict) {
    if (snap.conflict) {
      await log(
        "warn",
        `本浏览器的亚马逊标签页显示的登录状态不一致（按${snap.signed_in ? `买家号 ID ${maskCustomerId(snap.customer_id)}` : "未登录"}上报，服务端不会下发任务）：请刷新或关闭较早打开的亚马逊标签页`,
      );
    } else {
      await log(
        snap.signed_in ? "info" : "warn",
        snap.signed_in ? `识别到亚马逊登录（买家号 ID ${maskCustomerId(snap.customer_id)}）` : "本浏览器未登录亚马逊",
      );
    }
  }
  return snap;
}

/** 输入：对账结论、是否强制 → 输出：无。单独上报身份（同一份 60 秒内不重复，强制时照发）。 */
async function reportIdentity(snap: ReconciledIdentity, force: boolean): Promise<boolean> {
  const client = await makeClient();
  if (!client) return false;
  // 一份几分钟前读的结论（这一轮哪个标签页都没读到）不当成「刚读到」再报：服务端看到的「最近上报」得是真的。
  // 有分歧的结论例外 —— 它只会让服务端拒绝，不会放行。
  if (!snap.conflict && !isFresh(snap, Date.now())) return false;
  const sig = JSON.stringify([snap.signed_in, snap.customer_id]);
  const sent = await sget<{ sig: string; at: number }>(KEYS.identitySent);
  if (!force && sent && sent.sig === sig && Date.now() - sent.at < IDENTITY_DEDUPE_MS) return true;
  const r = await client.identity(toIdentity(snap));
  if (r.ok) {
    await sset(KEYS.identitySent, { sig, at: Date.now() });
    if (r.data.identity_status) await setIdentityStatus(r.data.identity_status);
    return true;
  }
  if (r.error.kind === "http" && (r.error.status === 404 || r.error.status === 405 || r.error.status === 422)) {
    // 服务端没有单独的身份接口：改由心跳捎带（契约 §5.2 的 identity?）。
    await sset(KEYS.identitySent, { sig, at: Date.now() });
    return heartbeat("身份上报");
  }
  await log("warn", `身份上报失败：${r.error.message}`);
  return false;
}

/** 输入：内容脚本读到的身份、是否强制上报、发送的标签页 → 输出：服务端给的身份状态（拿不到为 null）与有没有分歧。 */
async function onIdentity(raw: IdentitySnapshot | null, force: boolean, tabId: number | undefined): Promise<{ identity_status: IdentityStatus | null; conflict: boolean }> {
  const snap = await identityGate.run(async () => {
    if (tabId !== undefined) {
      const obs = toObservation(raw, tabId, Date.now());
      if (obs) await editIdentityStore((s) => observeIdentity(s, obs));
    }
    return reconcileAndStore();
  });
  if (snap) await reportIdentity(snap, force);
  // 强制刷新时再拉一次心跳，拿到服务端按最新身份算出的状态与配置（这一页没读到身份也拉）。
  if (force) await heartbeat("刷新身份");
  const cfg = await lget<ConfigCache>(KEYS.config);
  return { identity_status: cfg?.heartbeat.identity_status ?? null, conflict: snap?.conflict ?? false };
}

/** 标签页关闭、离开了那一页：它的读数退出「开着的」，重新对账，结论变了就上报。 */
async function onIdentityTabGone(tabId: number, loadedBefore?: number): Promise<void> {
  const snap = await identityGate.run(async () => {
    await editIdentityStore((s) => {
      const cur = s.live[String(tabId)];
      if (!cur) return s;
      if (loadedBefore !== undefined && cur.loaded_at >= loadedBefore) return s; // 已经是新页面的读数
      return retireIdentityTab(s, tabId);
    });
    return reconcileAndStore();
  });
  if (snap) await reportIdentity(snap, false);
}

async function setIdentityStatus(st: IdentityStatus): Promise<void> {
  const cfg = await lget<ConfigCache>(KEYS.config);
  if (!cfg || cfg.heartbeat.identity_status === st) return;
  await lset(KEYS.config, { ...cfg, heartbeat: { ...cfg.heartbeat, identity_status: st } });
  const t = IDENTITY_STATUS_TEXT[st];
  await log(st === "match" ? "ok" : "warn", `身份状态：${t.short}（${t.long}）`);
}

/** 请每个亚马逊标签页重读身份（不在亚马逊页面上的、没答复的不算），对账、强制上报。
 *  返回对账结论（一份读数都没有为 null）与这一次有没有报上去。 */
async function readAllTabsAndReport(): Promise<{ snap: ReconciledIdentity | null; reported: boolean }> {
  const tabs = await amazonTabs();
  const readings = await Promise.all(
    tabs.map(async (t) => {
      try {
        const msg: TabMsg = { type: "pc.readIdentity" };
        const r = (await withTimeout(chrome.tabs.sendMessage(t.id!, msg), PING_TIMEOUT_MS, PING_TIMEOUT_TEXT)) as { identity?: IdentitySnapshot | null } | undefined;
        return { tabId: t.id!, identity: r?.identity ?? null };
      } catch {
        return { tabId: t.id!, identity: null }; // 这个标签页没有内容脚本（插件装好之前就打开了）或没答复
      }
    }),
  );
  const snap = await identityGate.run(async () => {
    const now = Date.now();
    for (const r of readings) {
      const obs = toObservation(r.identity, r.tabId, now);
      if (obs) await editIdentityStore((s) => observeIdentity(s, obs));
    }
    return reconcileAndStore();
  });
  const reported = snap ? await reportIdentity(snap, true) : false;
  return { snap, reported };
}

/** 开始执行前（以及刚配对完）：每个亚马逊标签页重读身份、对账、强制上报，再同步心跳。 */
async function onPrepareStart(reason = "开始执行前"): Promise<PrepareReply> {
  const { snap } = await readAllTabsAndReport();
  await heartbeat(reason);
  const cfg = await lget<ConfigCache>(KEYS.config);
  const conflict = snap?.conflict ?? false;
  return {
    conflict,
    identity_status: cfg?.heartbeat.identity_status ?? null,
    message: conflict
      ? "开始执行失败：本浏览器的亚马逊标签页显示的登录账号不一致（较早打开的页面还停在之前的登录状态），请刷新或关闭较早打开的亚马逊标签页后再试"
      : null,
  };
}

/** 执行中（提交登记之前、以及每隔几分钟）：每个亚马逊标签页重读身份、对账、强制上报（不发心跳，快）。
 *  服务端在领取、进度、提交登记时都要求身份 10 分钟内上报过。 */
async function onRefreshIdentity(): Promise<RefreshIdentityReply> {
  const { snap, reported } = await readAllTabsAndReport();
  const cfg = await lget<ConfigCache>(KEYS.config);
  return { reported, conflict: snap?.conflict ?? false, identity_status: cfg?.heartbeat.identity_status ?? null };
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
    const message = pairFailureText(r.error, url);
    await log("err", message);
    return { ok: false, message };
  }
  if (!(await writeToken(r.data.device_token))) {
    await log("err", "配对失败：插件的密钥库写不进去（浏览器存储不可用）");
    return { ok: false, message: "配对失败：插件的密钥库写不进去（浏览器存储不可用），请重新加载插件后再试" };
  }
  const rec: DeviceRecord = {
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
  await lremove([KEYS.config, KEYS.connection, KEYS.schedule, KEYS.receipts]);
  await sremove([KEYS.identitySent, KEYS.heartbeatTriedAt]);
  await log("ok", `配对成功：设备「${name}」${r.data.workspace_name ? `（工作区 ${r.data.workspace_name}）` : ""}`);
  await heartbeatAndSchedule("配对完成");
  // 已经开着的亚马逊页面：马上把身份报上去，不等它们下一次重读。
  void onPrepareStart("配对完成").catch(() => undefined);
  return { ok: true, message: "配对成功" };
}

async function onUnpair(): Promise<{ ok: boolean; message: string }> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (ar && ar.orphaned_at === null) {
    const p = await probeEngine(ar);
    if (p !== "dead") return { ok: false, message: "解除配对失败：正在执行，结束后再解除" };
    await orphanRun(ar, "engine_gone", `执行页面上的引擎已经不在了：批次 #${ar.run_id} 中断`);
  }
  const pending = Object.keys(dropExpired(await lget<ReceiptStore>(KEYS.receipts), Date.now()).store).length;
  if (pending) {
    return { ok: false, message: `解除配对失败：还有 ${pending} 条结果回执没送到服务端，等服务端恢复、补交完成后再解除` };
  }
  if (await sget<ActiveRun>(KEYS.activeRun)) return { ok: false, message: "解除配对失败：上一个执行批次还在收尾，稍后再试" };
  await deleteToken();
  await lremove([KEYS.device, KEYS.config, KEYS.connection, KEYS.schedule, KEYS.runView, KEYS.receipts]);
  await sremove([KEYS.identitySent, KEYS.activeRun, KEYS.heartbeatTriedAt]);
  for (const a of await chrome.alarms.getAll()) {
    if (a.name.startsWith(ALARM_SCHED_PREFIX) || [ALARM_RUN_BEAT, ALARM_FAST_HEARTBEAT, ALARM_RECEIPTS].includes(a.name)) {
      await chrome.alarms.clear(a.name);
    }
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
    if (next.simulationOutcome !== cur.simulationOutcome) {
      const label = SIM_OUTCOMES.find((o) => o.value === next.simulationOutcome)?.label ?? next.simulationOutcome;
      await log("info", `模拟结局改为：${label}`);
    }
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

// ── 执行批次跟踪（补发批次心跳、接管引擎已经没了的批次）────────────────────

async function clearActiveRun(): Promise<void> {
  await sremove(KEYS.activeRun);
  try {
    await chrome.alarms.clear(ALARM_RUN_BEAT);
  } catch {
    // 忽略
  }
}

async function markRunViewInactive(tabId: number | null): Promise<void> {
  const rv = await lget<RunViewRecord>(KEYS.runView);
  if (rv?.active && (tabId === null || rv.tab_id === tabId)) await lset(KEYS.runView, { ...rv, active: false, updated_at: Date.now() });
}

/** 引擎走后台的每一次协议请求都更新 touched_at（10 秒内只写一次）；续租成功时记下 last_beat_ok_at。 */
async function touchRun(tabId: number | undefined, beatOk: boolean): Promise<void> {
  await runGate.run(async () => {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (!ar || ar.tab_id !== tabId) return;
    const now = Date.now();
    if (!beatOk && now - ar.touched_at < 10_000) return;
    await sset(KEYS.activeRun, { ...ar, touched_at: now, ...(beatOk ? { last_beat_ok_at: now } : {}) });
  });
}

/** 输入：当前批次 → 输出：执行页面上的引擎还在不在跑它（dead：确定不在；unknown：页面没答复）。 */
async function probeEngine(ar: ActiveRun): Promise<"alive" | "dead" | "unknown"> {
  if (!(await tabAlive(ar.tab_id))) return "dead";
  try {
    const msg: TabMsg = { type: "pc.ping" };
    const reply = await withTimeout(chrome.tabs.sendMessage(ar.tab_id, msg), PING_TIMEOUT_MS, PING_TIMEOUT_TEXT);
    return pingSaysAlive(reply, ar.run_id) ? "alive" : "dead";
  } catch (e) {
    // 「收不到」（页面上没有内容脚本：跳到了别的网站、刷新到一半）是硬事实；只是没按时答复则说不准。
    return e instanceof Error && e.message === PING_TIMEOUT_TEXT ? "unknown" : "dead";
  }
}

/** 引擎已经没了：后台接管这个批次（界面上不再显示「正在执行」，标签页租约放开），然后处理它。 */
async function orphanRun(ar: ActiveRun, reason: string, text: string): Promise<void> {
  const taken = await runGate.run(async () => {
    const cur = await sget<ActiveRun>(KEYS.activeRun);
    if (!cur || cur.run_id !== ar.run_id || cur.orphaned_at !== null) return false;
    await sset(KEYS.activeRun, { ...cur, orphaned_at: Date.now(), orphan_reason: reason } satisfies ActiveRun);
    return true;
  });
  if (!taken) return;
  await markRunViewInactive(ar.tab_id);
  await onLeaseRelease(ar.tab_id);
  await log("warn", text);
  await ensureRunBeatAlarm().catch(() => undefined);
  await settleOrphan();
}

/** 接管的批次：先补交它的回执（按退避）；都送到了就结束批次；还有没送到的就续租等着，
 *  租约预算用完、或接管满 CUSTODY_MAX_MS 还送不到（例如结果接口一直 5xx）则放手。 */
async function settleOrphan(): Promise<void> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (!ar || ar.orphaned_at === null) return;
  await drainReceipts({ runId: ar.run_id });
  const store = await lget<ReceiptStore>(KEYS.receipts);
  const pending = pendingForRun(store, ar.run_id);
  const client = await makeClient();
  if (!client) {
    await clearActiveRun();
    return;
  }
  const now = Date.now();
  const step = custodyStep(ar, now, pending);
  const minutes = leaseMinutes(ar.lease_ttl_s);
  if (step === "finish") {
    const r = await client.finishRun(ar.run_id, ar.orphan_reason ?? "engine_gone");
    await clearActiveRun();
    if (r.ok) {
      await log("info", `已通知服务端结束批次 #${ar.run_id}：在途尝试按租约过期处理（未登记提交的回到已分配，已登记提交的进结果待核实）`);
    } else if (r.error.kind === "http" && !(r.error.status !== null && r.error.status >= 500)) {
      await log("info", `批次 #${ar.run_id} 在服务端已经结束（${r.error.message}）`);
    } else {
      await log("warn", `结束批次 #${ar.run_id} 失败：${r.error.message}；服务端会在租约到期（约 ${minutes} 分钟）后处理在途尝试`);
    }
    return;
  }
  if (step === "release") {
    await clearActiveRun();
    if (custodyOverBudget(ar, now)) {
      // 续租一直成功、结果却一直送不到（服务端 5xx）：不能替没有引擎的批次一直占着账号。
      const lastErr = Object.values(store ?? {}).find((r) => r.run_id === ar.run_id && r.last_error)?.last_error ?? null;
      await log(
        "warn",
        `批次 #${ar.run_id} 还有 ${pending} 条结果回执在 ${Math.round(CUSTODY_MAX_MS / 60_000)} 分钟内没有送到服务端${lastErr ? `（${lastErr}）` : ""}：不再续租，服务端会在租约到期（约 ${minutes} 分钟）后处理在途尝试；回执保留，之后继续补交（记为迟到回执，供人工核实）`,
      );
    } else {
      await log(
        "warn",
        `批次 #${ar.run_id} 还有 ${pending} 条结果回执一直送不到服务端：不再续租，服务端会在租约到期后处理在途尝试；回执保留，连上后补交（记为迟到回执，供人工核实）`,
      );
    }
    return;
  }
  const r = await client.runHeartbeat(ar.run_id);
  if (r.ok) {
    await runGate.run(async () => {
      const cur = await sget<ActiveRun>(KEYS.activeRun);
      if (cur && cur.run_id === ar.run_id) await sset(KEYS.activeRun, { ...cur, last_beat_ok_at: Date.now() });
    });
  } else if (r.error.kind === "http" && !(r.error.status !== null && r.error.status >= 500)) {
    await clearActiveRun();
    await log("warn", `批次 #${ar.run_id} 的执行权已失效（${r.error.message}）；回执保留，连上后补交`);
  }
}

/** 后台的批次闹钟（30 秒一次）。主心跳由引擎自己发；这一路兜底，而且只在执行页面对 ping 有答复、
 *  引擎最近还在动时才发 —— 不替一个卡死或已经没了的引擎续命。接管中的批次走 settleOrphan。 */
async function onRunBeat(): Promise<void> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (!ar) {
    await chrome.alarms.clear(ALARM_RUN_BEAT);
    return;
  }
  if (ar.orphaned_at !== null) return settleOrphan();
  const probe = await probeEngine(ar);
  if (probe === "dead") {
    return orphanRun(ar, "engine_gone", `执行页面上的引擎已经不在了（页面已刷新、跳转或关闭）：批次 #${ar.run_id} 中断`);
  }
  if (probe === "unknown" || Date.now() - ar.touched_at > ENGINE_SILENT_MS) return;
  const client = await makeClient();
  if (!client) return;
  const r = await client.runHeartbeat(ar.run_id);
  let stop: string | null = null;
  if (r.ok) {
    await touchRun(ar.tab_id, true);
    if (r.data.stop) stop = `服务端要求停止：${stopReasonLabel(r.data.stop_reason)}`;
  } else if (r.error.kind === "http" && !(r.error.status !== null && r.error.status >= 500)) {
    // 执行权已经没了（LEASE_LOST / 401 / 404）：本地也不再认这个批次。引擎下一次请求会自己停下。
    stop = `执行权已失效：${r.error.message}`;
    await clearActiveRun();
    await markRunViewInactive(ar.tab_id);
    await log("warn", `批次 #${ar.run_id} ${stop}`);
  }
  if (stop) {
    const msg: TabMsg = { type: "pc.stopRun", reason: stop };
    await chrome.tabs.sendMessage(ar.tab_id, msg).catch(() => undefined);
  }
}

// ── 结果回执 ─────────────────────────────────────────────────────────────

async function saveReceipt(attemptId: number, runId: number | null, body: ResultIn): Promise<void> {
  await receiptGate.run(async () => {
    await lset(KEYS.receipts, putReceipt(await lget<ReceiptStore>(KEYS.receipts), attemptId, runId, body, Date.now()));
  });
  await ensureReceiptAlarm().catch(() => undefined);
}

async function settleReply(attemptId: number, r: ApiResult<unknown>, byDrain: boolean): Promise<ReplyVerdict> {
  const verdict = classifyResultReply(r);
  await receiptGate.run(async () => {
    const msg = r.ok ? null : r.error.message;
    await lset(KEYS.receipts, settleReceipt(await lget<ReceiptStore>(KEYS.receipts), attemptId, verdict, msg, Date.now(), byDrain));
  });
  return verdict;
}

/** 输入：只补交某个批次的回执（接管时：还没被后台补交过的立即发 —— 发起上报的引擎已经没了；补交过的按退避），
 *  或全部该补交的（ignoreBackoff：后台启动、心跳成功时不等退避）→ 输出：无。断网时停在第一条，下一轮再试。 */
async function drainReceipts(opts: { runId?: number; ignoreBackoff?: boolean } = {}): Promise<void> {
  const runId = opts.runId;
  await drainGate.run(async () => {
    const client = await makeClient();
    if (!client) return;
    const now = Date.now();
    const expired = await receiptGate.run(async () => {
      const out = dropExpired(await lget<ReceiptStore>(KEYS.receipts), now);
      if (out.expired.length) await lset(KEYS.receipts, out.store);
      return out.expired;
    });
    for (const rc of expired) await log("warn", `尝试 #${rc.attempt_id} 的结果回执超过 24 小时仍没送到，不再补交（请在控制台核实）`);
    const store = (await lget<ReceiptStore>(KEYS.receipts)) ?? {};
    const due =
      runId === undefined
        ? dueReceipts(store, now, { ignoreBackoff: opts.ignoreBackoff })
        : Object.values(store).filter((r) => r.run_id === runId && (r.tries === 0 || r.next_at <= now));
    for (const rc of due) {
      const r = await client.result(rc.attempt_id, rc.body);
      const v = await settleReply(rc.attempt_id, r, true);
      if (v === "delivered") {
        await log(
          "ok",
          !r.ok && isLateReceipt(r.error)
            ? `尝试 #${rc.attempt_id} 的结果回执已补交：服务端已把这一笔判为结果待核实，回执作为迟到回执记下`
            : `尝试 #${rc.attempt_id} 的结果回执已补交`,
        );
      } else if (v === "drop") {
        await log("warn", `尝试 #${rc.attempt_id} 的结果回执被服务端拒绝，不再补交：${r.ok ? "" : r.error.message}`);
      } else if (!r.ok && r.error.kind !== "http") {
        break; // 没连上服务端：后面的也不用试了
      }
    }
    if (Object.keys((await lget<ReceiptStore>(KEYS.receipts)) ?? {}).length === 0) {
      await chrome.alarms.clear(ALARM_RECEIPTS).catch(() => undefined);
    }
  });
}

// ── 协议转发 ─────────────────────────────────────────────────────────────

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
        const data = r.data as { run_id: number; lease_ttl_s: number | null };
        const now = Date.now();
        await sset(KEYS.activeRun, {
          run_id: data.run_id,
          tab_id: tabId,
          trigger,
          started_at: now,
          touched_at: now,
          lease_ttl_s: data.lease_ttl_s ?? null,
          last_beat_ok_at: now,
          orphaned_at: null,
          orphan_reason: null,
        } satisfies ActiveRun);
        await ensureRunBeatAlarm();
      }
      return r;
    }
    case "runHeartbeat":
      r = await client.runHeartbeat(num(0));
      await touchRun(tabId, r.ok);
      return r;
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
    case "result": {
      // 先存回执再发：这一路断了（后台被回收、服务端重启），回执还在，后台补交。
      const attemptId = num(0);
      const body = a[1] as ResultIn;
      const ar = await sget<ActiveRun>(KEYS.activeRun);
      await saveReceipt(attemptId, ar && ar.tab_id === tabId ? ar.run_id : null, body);
      r = await client.result(attemptId, body);
      await settleReply(attemptId, r, false);
      break;
    }
    case "finishRun": {
      r = await client.finishRun(num(0), String(a[1] ?? ""));
      const ar = await sget<ActiveRun>(KEYS.activeRun);
      if (ar && ar.run_id === num(0)) await clearActiveRun();
      return r;
    }
    default:
      return { ok: false, error: localError("VALIDATION") };
  }
  await touchRun(tabId, false);
  return r;
}

async function onRunView(m: Extract<Msg, { type: "pc.runView" }>, tabId: number | undefined): Promise<void> {
  const rec: RunViewRecord = { active: !!m.active, tab_id: tabId ?? null, view: m.view ?? null, updated_at: Date.now() };
  await lset(KEYS.runView, rec);
  if (!m.active) {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (!ar || ar.tab_id !== tabId || ar.orphaned_at !== null) return;
    const pending = pendingForRun(await lget<ReceiptStore>(KEYS.receipts), ar.run_id);
    if (pending > 0) {
      // 引擎已中断，但这个批次还有结果没送到：后台接着续租、补交，送达后结束批次（最多 CUSTODY_MAX_MS）。
      await orphanRun(
        ar,
        "engine_ended",
        `批次 #${ar.run_id} 还有 ${pending} 条结果回执没送到：后台继续续租并补交（最多 ${Math.round(CUSTODY_MAX_MS / 60_000)} 分钟），送达后结束批次`,
      );
    } else {
      await clearActiveRun(); // 引擎已结束（含中断）：后台也不再补发心跳
    }
  }
}

/** 亚马逊页面加载好了（内容脚本启动）。 */
async function onHello(tabId: number | undefined, loadedAt: number | null): Promise<void> {
  if (tabId !== undefined) {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (ar && ar.orphaned_at === null && helloEndsRun(ar, tabId, loadedAt)) {
      await orphanRun(ar, "page_reloaded", `执行页面已刷新或跳转：批次 #${ar.run_id} 中断`);
    } else if (!ar) {
      // 插件更新、后台存储被清过之后留下的旧「正在执行」：这一页刚加载，它不可能还在执行。
      await markRunViewInactive(tabId);
    }
    if (typeof loadedAt === "number") await onIdentityTabGone(tabId, loadedAt);
  }
  // 打开亚马逊页面 = 「环境上线」：同步一次（拿到刚在控制台改的绑定与配置），顺带检查今天错过的定时。
  await heartbeatIfStale("打开亚马逊页面", SYNC_MIN_AGE_MS);
  await checkSchedule("打开亚马逊页面");
}

/** 亚马逊页面要离开了（刷新、跳转、关闭）。 */
async function onBye(tabId: number | undefined, loadedAt: number | null, runId: number | null): Promise<void> {
  if (tabId === undefined) return;
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (ar && ar.orphaned_at === null && ar.tab_id === tabId && runId !== null && runId === ar.run_id) {
    await orphanRun(ar, "page_left", `执行页面已离开（刷新、跳转或关闭）：批次 #${ar.run_id} 中断`);
  }
  await onIdentityTabGone(tabId, typeof loadedAt === "number" ? loadedAt + 1 : undefined);
}

// ── 定时触发 ─────────────────────────────────────────────────────────────

async function amazonTabs(): Promise<chrome.tabs.Tab[]> {
  try {
    // 只按主机权限里的地址查（不申请 tabs 权限也查得到）。
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
    if (ar) {
      // 本地记录说在执行：先问执行页面引擎还在不在 —— 不信一个可能已经过时的标记。
      if (ar.orphaned_at === null) {
        const probe = await probeEngine(ar);
        if (probe !== "dead") return done("info", "本浏览器已在执行，并入当前批次");
        await orphanRun(ar, "engine_gone", `执行页面上的引擎已经不在了：批次 #${ar.run_id} 中断`);
      }
      // 还在接管（上一批次有回执没送到）：等它结束再开始，这次先不算处理过。
      if (await sget<ActiveRun>(KEYS.activeRun)) return;
    }

    for (const t of await amazonTabs()) {
      let reply: ScheduledReply | null = null;
      try {
        const msg: TabMsg = { type: "pc.startScheduled", slots: due };
        reply = (await withTimeout(chrome.tabs.sendMessage(t.id!, msg), SCHEDULE_REPLY_MS, "等待页面答复超时")) as ScheduledReply | null;
      } catch {
        reply = null; // 这个标签页没有内容脚本（插件装好之前就打开了）或答复超时
      }
      if (!reply || typeof reply.result !== "string") continue;
      if (reply.result === "network" || reply.result === "not_ready" || reply.result === "retry") {
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
    case "pc.sync":
      await heartbeatIfStale(String(msg.reason ?? "同步"), Number.isFinite(msg.minAgeMs) ? Number(msg.minAgeMs) : SYNC_MIN_AGE_MS);
      return snapshot();
    case "pc.prepareStart":
      return onPrepareStart();
    case "pc.refreshIdentity":
      return onRefreshIdentity();
    case "pc.pair":
      return onPair(msg);
    case "pc.unpair":
      return onUnpair();
    case "pc.saveSettings":
      return onSaveSettings(msg.patch ?? {});
    case "pc.identity":
      return onIdentity(msg.identity, !!msg.force, tabId);
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
      void onHello(tabId, typeof msg.loaded_at === "number" ? msg.loaded_at : null);
      return { ok: true };
    case "pc.bye":
      void onBye(tabId, typeof msg.loaded_at === "number" ? msg.loaded_at : null, typeof msg.run_id === "number" ? msg.run_id : null);
      return { ok: true };
    default:
      return { ok: false, message: "未知消息" };
  }
}

chrome.runtime.onMessage.addListener((msg: Msg, sender, respond) => {
  handle(msg, sender).then(respond, (e: unknown) => {
    console.error("采购助手后台处理消息出错", e); // 原始的英文报错只进控制台，不进界面
    respond({ ok: false, message: "插件后台处理失败，请重新加载插件后再试" });
  });
  return true; // 异步答复：通道保持打开
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === ALARM_HEARTBEAT) void heartbeatAndSchedule("定时心跳");
  else if (a.name === ALARM_FAST_HEARTBEAT) void heartbeatAndSchedule("等待绑定账号");
  else if (a.name === ALARM_RUN_BEAT) void onRunBeat();
  else if (a.name === ALARM_RECEIPTS) void drainReceipts();
  else if (a.name.startsWith(ALARM_SCHED_PREFIX)) {
    void (async () => {
      const slot = `闹钟 ${a.name.slice(ALARM_SCHED_PREFIX.length)}`;
      // 到点先拿一份新配置：缓存最多一分钟前的（例如刚在控制台恢复执行），不能据此放弃今天这一次。
      await heartbeat(slot);
      await checkSchedule(slot);
      await planSchedule(); // 一次性闹钟响过之后排下一天
    })();
  }
});

chrome.runtime.onInstalled.addListener((details) => {
  // 插件安装、更新、重新加载：storage.session（当前批次）已清空，旧页面里的引擎也已和后台断开，
  // 本地留下的「正在执行」不可能还成立。
  void markRunViewInactive(null);
  void ensureAlarms();
  if (details.reason === "install") void chrome.runtime.openOptionsPage().catch(() => undefined);
  void heartbeatAndSchedule("插件安装或更新");
});

chrome.runtime.onStartup.addListener(() => {
  // 浏览器重启：上一次会话里的执行不可能还活着。
  void markRunViewInactive(null);
  void ensureAlarms();
  void heartbeatAndSchedule("浏览器启动");
});

// 标签页关掉：放开它的执行租约；它在跑批次的话后台接管（立即通知服务端结束）；它的身份读数退出对账。
chrome.tabs.onRemoved.addListener((tabId) => {
  void onLeaseRelease(tabId);
  void (async () => {
    const ar = await sget<ActiveRun>(KEYS.activeRun);
    if (ar && ar.tab_id === tabId && ar.orphaned_at === null) {
      await orphanRun(ar, "tab_closed", `执行中的标签页已关闭：批次 #${ar.run_id} 中断`);
    }
    await onIdentityTabGone(tabId);
  })();
});

// 每次后台被唤醒都确认一次闹钟还在（浏览器重启可能清掉闹钟），并补交还没送到的结果回执（不等退避）。
void ensureAlarms();
void (async () => {
  if (Object.keys((await lget<ReceiptStore>(KEYS.receipts)) ?? {}).length) await drainReceipts({ ignoreBackoff: true });
})().catch(() => undefined);
