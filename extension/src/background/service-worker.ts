/** MV3 后台：自动登记、心跳、身份对账与上报、标签页租约、定时触发、协议请求转发、结果回执补交、
 *  接管执行页面已经没了的批次。
 *
 * **这里不跑任何页面动作，也不跑执行引擎。** 引擎在亚马逊标签页的内容脚本里跑
 * （后续真实驱动需要页面），它的每一次协议请求经由这里转发：设备令牌由后台生成、只在后台使用
 * （存在扩展源下的 IndexedDB，见 secrets.ts；内容脚本读得到 chrome.storage.local，读不到那里）。
 *
 * 执行入口（规格 v3）：没有配对码，也没有设备与账号的绑定。
 *   · 装好就自动登记：令牌首次需要时生成；没有登记成功过、或任何接口回 401，就带着同一个令牌调
 *     POST /register（同一时刻只发一个），成功后把原请求再发一次；失败按 5 / 15 / 60 秒退避。
 *   · 领谁的任务，看这个浏览器此刻登着哪个买家号：心跳与身份上报的返回里是服务端按上报的买家号 ID
 *     现算的登录状态与账号；开始批次时带上当前页面读到的买家号 ID。
 *   · 改设置、重新登记这类特权消息只接受扩展自己的页面；改服务端地址时换一个新令牌（旧令牌绝不发往新地址）。
 *
 * 服务端工作的生命周期教训（移植自早期实现）：
 *   · 后台空闲约 30 秒被回收、模块级变量归零 → 租约、当前批次、身份读数、回执、登记的结论全放 chrome.storage；
 *     模块级只留串行闸与单飞闸（同一个后台实例里只有一条事件循环，闸只需管住这一实例）。
 *   · 闹钟可能被浏览器重启清掉 → 每次唤醒都 ensureAlarms()；定时「到没到期」不依赖闹钟准时，
 *     由 schedule.dueSlots 根据当天记录判定，每分钟的心跳也会顺带检查一次。
 *   · 执行页面刷新、跳转、关闭时引擎随之消失、来不及说一声 → 新页面的 pc.hello、旧页面的 pc.bye、
 *     标签页关闭、对 pc.ping 不再答复，任何一个都让后台接管批次（core/custody.ts）。
 *   · 不申请 cookies 与 tabs 权限，不读也不上传 Cookie；身份只来自页面上读到的登录状态与 customerId，
 *     多个标签页的读数按页面加载先后对账（core/identity.reconcileIdentity）。
 */

import { PluginClient, type ApiError, type ApiResult } from "../core/client.js";
import { codeLabel, stopReasonLabel } from "../core/codes.js";
import { CUSTODY_MAX_MS, custodyOverBudget, custodyStep, helloEndsRun, pingSaysAlive } from "../core/custody.js";
import { leaseMinutes } from "../core/engine.js";
import { Serial, SharedFlight, withTimeout } from "../core/gates.js";
import {
  IDENTITY_DEDUPE_MS,
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
  PRIVILEGED_TYPES,
  isPrivilegedSender,
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
import {
  REREGISTER_MIN_GAP_MS,
  generateDeviceToken,
  isValidDeviceToken,
  registerAllowed,
  registerFailed,
  registerSucceeded,
  type RegisterMode,
  type RegisterStatus,
} from "../core/register.js";
import {
  bookDay,
  bookPut,
  dueSlots,
  markSlots,
  nextFirePlan,
  normalizeBook,
  parseSlot,
  slotEpoch,
  slotToClaim,
  toLocalEpoch,
  type ScheduleDayState,
} from "../core/schedule.js";
import {
  ORDER_HISTORY_URL,
  consoleAccountsUrl,
  normalizeServerUrl,
  normalizeSettings,
  normalizeWorkspaceId,
  validateServerUrl,
  type Settings,
} from "../core/settings.js";
import { SIM_OUTCOMES, normalizeOutcome } from "../core/simulate.js";
import { scheduleVerdict } from "../core/start.js";
import { fastSyncWanted, loginLabel, unregisteredSince } from "../core/status.js";
import { parseIsoMs } from "../core/time.js";
import type { HeartbeatOut, LoginStateName, ResultIn, Trigger } from "../core/types.js";
import { CUSTOMER_ID_RE } from "../dom/parse.js";
import { readSecret, writeSecret } from "./secrets.js";
import { lget, lremove, lset, sget, sremove, sset } from "./storage.js";

const EXT_VERSION = chrome.runtime.getManifest().version;
/** 扩展自己的根地址（chrome-extension://<id>/）：特权消息只认从这里发来的。 */
const EXT_ROOT = chrome.runtime.getURL("");

const ALARM_HEARTBEAT = "pc.heartbeat";
const ALARM_FAST_HEARTBEAT = "pc.heartbeatFast";
const ALARM_RUN_BEAT = "pc.runBeat";
const ALARM_RECEIPTS = "pc.receipts";
const ALARM_REGISTER = "pc.register";
const ALARM_SCHED_PREFIX = "pc.sched:";

/** 引擎超过这么久没有发过任何协议请求，后台就不再替它补发批次心跳（引擎可能卡死了，
 *  让租约自然到期、由服务端清扫，比替一个不动的引擎续命安全）。 */
const ENGINE_SILENT_MS = 150_000;
/** 定时触发等内容脚本答复的上界（开始批次那一步，含身份刷新）。 */
const SCHEDULE_REPLY_MS = 90_000;
/** 快心跳的间隔（分钟）：只在登录状态是「买家号还没有登记」时开，而且自这个状态出现起最多 10 分钟
 *  （core/status.fastSyncWanted）—— 所有者在控制台点了「登记」，插件这边很快就知道。其余一律 60 秒。
 *  chrome.alarms 对打包安装的扩展最短 30 秒一次（加载已解压的扩展时按这里的 10 秒）；
 *  看得见的面板、设置页另有 10 秒一次的同步。 */
const FAST_HEARTBEAT_MIN = 10 / 60;
/** 问执行页面「引擎还在吗」、请页面重读身份的答复上界。 */
const PING_TIMEOUT_MS = 5_000;
const PING_TIMEOUT_TEXT = "等待页面答复超时";
/** 打开或回到页面时，上一次心跳早于这个间隔才同步。 */
const SYNC_MIN_AGE_MS = 5_000;
/** 定时的时间点过了这么久才向服务端申请：服务端按分钟判断「到没到点」，早到一步会被当成参数不对（422），
 *  而 422 会让这个时间点今天不再触发。本机时钟按心跳校正，误差远小于这个余量。 */
const SLOT_MARGIN_MS = 2_000;

const logGate = new Serial();
const leaseGate = new Serial();
const schedGate = new Serial();
const hbGate = new Serial();
const runGate = new Serial();
const receiptGate = new Serial();
const drainGate = new Serial();
const identityGate = new Serial();
const secretGate = new Serial();
const cfgGate = new Serial();
/** 登记的单飞闸：同一时刻只发一个 POST /register，后到的等同一个结果。 */
const regFlight = new SharedFlight<boolean>();
/** 401 之后重新登记的单飞闸：几路请求同时收到 401 时只重新登记一次。 */
const reauthFlight = new SharedFlight<boolean>();
/** 这个后台实例里因为 401 重新登记成功过几次。一个请求发出之后它变了 = 别的一路已经重新登记过，
 *  这个请求直接再发一次就行。只在同一个后台实例里有意义（在途的请求也只活在这个实例里），所以不用落盘。 */
let reauthEpoch = 0;

/** 登记成功过的这个浏览器：登记记录 + 令牌 + 令牌所属的服务端地址。 */
interface Registered {
  rec: DeviceRecord;
  token: string;
  url: string;
}

// ── 基础读写 ─────────────────────────────────────────────────────────────

/** 输入：级别与文本 → 输出：无。追加一行日志（最近 50 行），串行写入避免互相覆盖。 */
async function log(level: LogLevel, text: string): Promise<void> {
  await logGate.run(async () => {
    const lines = await lget<LogLine[]>(KEYS.logs);
    await lset(KEYS.logs, appendLog(lines, { at: Date.now(), level, text }));
  });
}

/** 输入：无 → 输出：令牌与它所属的服务端地址；密钥库读不了或写不进去为 null（这时不登记，见 secrets.ts）。
 *
 *  首次需要时生成令牌，所属地址取当时设置里的服务端地址。旧版留下的东西原样沿用：
 *    · 早期版本把令牌写在 storage.local 的登记记录里 → 搬进 IndexedDB 并从记录里删掉；
 *    · 旧版配对得到的令牌没有记过所属地址 → 按旧的登记记录里的地址补上。
 *  整段串行：两路同时发现「没有令牌」不能各生成一个。 */
function ensureSecret(): Promise<{ token: string; url: string } | null> {
  return secretGate.run(async () => {
    const rec = await lget<DeviceRecord>(KEYS.device);
    const stored = normalizeSettings(await lget(KEYS.settings)).serverUrl;
    const recUrl = rec && typeof rec.server_url === "string" && validateServerUrl(rec.server_url).ok ? normalizeServerUrl(rec.server_url) : null;
    const read = await readSecret();
    let sec: { token: string; url: string | null } | null = read.ok ? read.secret : null;
    const legacy = rec && typeof rec.token === "string" && rec.token !== "" ? rec.token : null;
    // 库读不了：不能当成「没有令牌」去生成新的（会把库里原有的那个盖掉）。只有早期版本留在登记记录里的令牌还能先用着。
    if (!read.ok && !legacy) return null;
    if (!sec && legacy) {
      const url = recUrl ?? stored;
      if (await writeSecret(legacy, url)) {
        const rest: DeviceRecord = { ...rec! };
        delete rest.token;
        await lset(KEYS.device, rest);
      }
      // 搬不进去（密钥库暂时不可用）也先用着：下一次唤醒再搬。
      sec = { token: legacy, url };
    } else if (sec && rec && "token" in rec) {
      // 令牌已经在密钥库里了，登记记录里残留的那一份删掉（storage.local 内容脚本读得到）。
      const rest: DeviceRecord = { ...rec };
      delete rest.token;
      await lset(KEYS.device, rest);
    }
    if (sec && sec.url === null) {
      const url = recUrl ?? stored;
      if (!(await writeSecret(sec.token, url))) return null;
      sec = { token: sec.token, url };
    }
    if (sec && sec.url !== null && isValidDeviceToken(sec.token)) return { token: sec.token, url: sec.url };
    // 没有令牌（或存下来的不是服务端接受的形状）：生成一个新的。新令牌 = 一个还没登记过的浏览器。
    const url = sec?.url ?? stored;
    const token = generateDeviceToken();
    if (!(await writeSecret(token, url))) return null;
    if (rec) await lremove(KEYS.device);
    return { token, url };
  });
}

/** 输入：无 → 输出：设置。服务端地址以令牌所属的地址为准：chrome.storage.local 内容脚本也写得到，
 *  地址只能经设置页的「保存」（特权消息）连同令牌一起换；别处改了设置里的地址，这里把它改回来。 */
async function getSettings(): Promise<Settings> {
  const st = normalizeSettings(await lget(KEYS.settings));
  const read = await readSecret();
  const sec = read.ok ? read.secret : null;
  if (sec?.url && sec.url !== st.serverUrl && validateServerUrl(sec.url).ok) {
    const healed = { ...st, serverUrl: sec.url };
    await lset(KEYS.settings, healed);
    return healed;
  }
  return st;
}

/** 输入：无 → 输出：缓存的心跳；旧版形状（没有 login）当作没有 —— 插件更新后第一次心跳之前界面不拿旧缓存说话。 */
async function getConfig(): Promise<ConfigCache | null> {
  const c = await lget<ConfigCache>(KEYS.config);
  const hb = c?.heartbeat as Partial<HeartbeatOut> | undefined;
  if (!c || !hb || typeof hb !== "object" || !hb.login || typeof hb.login.state !== "string" || !hb.device) return null;
  return { ...c, unregistered_since: typeof c.unregistered_since === "number" ? c.unregistered_since : null };
}

/** 输入：无 → 输出：登记成功过的这个浏览器；没登记上（或登记记录是别的地址的）为 null。 */
async function registeredDevice(): Promise<Registered | null> {
  const sec = await ensureSecret();
  if (!sec) return null;
  const rec = await lget<DeviceRecord>(KEYS.device);
  if (!rec || typeof rec !== "object" || rec.device_id === undefined || rec.device_id === null) return null;
  if (normalizeServerUrl(String(rec.server_url ?? "")) !== sec.url) return null;
  return { rec, token: sec.token, url: sec.url };
}

function deviceView(d: Registered | null): DeviceView {
  if (!d) return { registered: false, device_id: null, device_name: null, workspace_id: null, workspace_name: null, registered_at: null };
  const r = d.rec;
  return {
    registered: true,
    device_id: r.device_id,
    device_name: r.device_name ?? null,
    workspace_id: typeof r.workspace_id === "number" ? r.workspace_id : null,
    workspace_name: r.workspace_name ?? null,
    registered_at: r.registered_at ?? r.paired_at ?? null,
  };
}

const clientFor = (d: { token: string; url: string }): PluginClient => new PluginClient({ baseUrl: d.url, token: d.token, extVersion: EXT_VERSION });

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

/** 插件本地的明确拒绝（请求没有发出去，结论已定）。 */
const localError = (code: string, message?: string, detail: unknown = null): ApiError => ({ kind: "http", status: null, code, message: message ?? codeLabel(code), detail });

/** 还没连上服务端：请求没有发出去，按「没说上话」算（回执留着补交，定时稍后再试）。原因取最近一次登记的结论。 */
async function notRegisteredError(): Promise<ApiError> {
  const st = await lget<RegisterStatus>(KEYS.registration);
  const why = st && !st.ok && st.message ? `还没有连上服务端：${st.message}` : codeLabel("NOT_REGISTERED");
  return { kind: "network", status: null, code: "NOT_REGISTERED", message: why, detail: null };
}

async function snapshot(): Promise<StateSnapshot> {
  const [device, config, connection, registration, identity, logs, book, run, receipts, ar] = await Promise.all([
    registeredDevice(),
    getConfig(),
    lget<ConnectionState>(KEYS.connection),
    lget<RegisterStatus>(KEYS.registration),
    lget<ReconciledIdentity>(KEYS.identity),
    lget<LogLine[]>(KEYS.logs),
    lget<unknown>(KEYS.schedule),
    lget<RunViewRecord>(KEYS.runView),
    lget<ReceiptStore>(KEYS.receipts),
    sget<ActiveRun>(KEYS.activeRun),
  ]);
  const settings = await getSettings();
  const cfg = device ? config : null;
  const cid = cfg?.heartbeat.login.state === "ready" ? cfg.heartbeat.account?.amazon_customer_id ?? cfg.heartbeat.login.customer_id : null;
  return {
    settings,
    device: deviceView(device),
    registration: registration ?? null,
    config: cfg,
    connection: connection ?? null,
    identity: identity ?? null,
    receipts: receiptViews(receipts),
    logs: Array.isArray(logs) ? logs : [],
    schedule: cid ? bookDay(normalizeBook(book), cid, serverNow(cfg)) : null,
    run: run ?? null,
    active_run: ar ? { run_id: ar.run_id, account_name: ar.account_name ?? null, started_at: ar.started_at, orphaned: ar.orphaned_at !== null } : null,
    ext_version: EXT_VERSION,
  };
}

// ── 自动登记 ─────────────────────────────────────────────────────────────

async function noteRegisterFailure(prev: RegisterStatus | undefined, e: ApiError, url: string): Promise<void> {
  const at = Date.now();
  const st = registerFailed(prev, e, at);
  await lset(KEYS.registration, st);
  await lset(KEYS.connection, { ok: false, at, kind: e.kind, code: e.code, message: e.message } satisfies ConnectionState);
  if (!prev || prev.ok || prev.code !== e.code) {
    const why = e.kind === "network" ? `连不上服务端 ${url}，插件会自动重试` : e.message || codeLabel(e.code);
    await log("err", `连接失败：${why}`);
  }
  try {
    if (st.next_at !== null) await chrome.alarms.create(ALARM_REGISTER, { when: st.next_at });
    else await chrome.alarms.clear(ALARM_REGISTER);
  } catch {
    // 闹钟接口不可用：每分钟的心跳闹钟、面板展开也会再试
  }
}

async function doRegister(reason: string, mode: RegisterMode): Promise<boolean> {
  const prev = await lget<RegisterStatus>(KEYS.registration);
  if (!registerAllowed(prev, Date.now(), mode)) return false;
  const sec = await ensureSecret();
  const settings = await getSettings();
  if (!sec) {
    // 令牌存不下来就不登记：否则每次唤醒都带着一个新令牌去登记，服务端会多出一串浏览器。
    await noteRegisterFailure(prev, localError("TOKEN_STORE"), settings.serverUrl);
    return false;
  }
  // 登记不带 Authorization：这个客户端不持有令牌，令牌只在请求体里。
  const r = await new PluginClient({ baseUrl: sec.url, extVersion: EXT_VERSION }).register(sec.token, settings.workspaceId);
  if (!r.ok) {
    await noteRegisterFailure(prev, r.error, sec.url);
    return false;
  }
  const at = Date.now();
  const old = await lget<DeviceRecord>(KEYS.device);
  const rec: DeviceRecord = {
    device_id: r.data.device_id,
    device_name: r.data.device_name,
    workspace_id: r.data.workspace_id,
    workspace_name: r.data.workspace_name,
    server_url: sec.url,
    registered_at: at,
  };
  await lset(KEYS.device, rec);
  await lset(KEYS.registration, registerSucceeded(at));
  await chrome.alarms.clear(ALARM_REGISTER).catch(() => undefined);
  const sameRow = !!old && old.device_id !== undefined && String(old.device_id) === String(rec.device_id) && normalizeServerUrl(String(old.server_url ?? "")) === sec.url;
  if (!sameRow) {
    const name = rec.device_name ?? `#${rec.device_id}`;
    await log("ok", `已在服务端登记：浏览器「${name}」${rec.workspace_name ? `（工作区 ${rec.workspace_name}）` : ""}（${reason}）`);
  }
  return true;
}

/** 输入：触发原因、谁在要求（core/register.RegisterMode）→ 输出：这个浏览器现在是不是登记上了。
 *  单飞：已经有一个登记请求在飞时，后到的等同一个结果。 */
function register(reason: string, mode: RegisterMode): Promise<boolean> {
  return regFlight.run(() => doRegister(reason, mode));
}

/** 带令牌的请求回了 401（服务端不认识这个浏览器：换过数据库，或这一行太久没联系被清理了）：
 *  带着**同一个**令牌重新登记。
 *  · 刚重新登记成功过还是 401：不再登记（登记记录留着），免得登记与 401 打转；
 *  · 重新登记没成功（服务端正在重启、新库里有多个工作区……）：本地的登记记录作废，之后按「还没登记上」走 ——
 *    受退避约束、该停下等人处理的就停下，不会每来一个 401 就再登记一次。 */
function reregisterAfter401(reason: string): Promise<boolean> {
  return reauthFlight.run(async () => {
    const last = await sget<number>(KEYS.reregisteredAt);
    const now = Date.now();
    if (typeof last === "number" && now >= last && now - last < REREGISTER_MIN_GAP_MS) return false;
    await log("warn", "服务端不认识这个浏览器（可能换过数据库，或太久没有联系被清理了）：用原来的令牌重新登记");
    const ok = await register(`${reason}，重新登记`, "auto");
    if (ok) {
      reauthEpoch += 1;
      await sset(KEYS.reregisteredAt, Date.now());
    } else {
      await lremove(KEYS.device);
    }
    return ok;
  });
}

/** 输入：一次带令牌的请求、触发原因 → 输出：它的结果。
 *  · 还没登记上：先登记（受退避约束）；登记不上就不发，按「没说上话」返回；
 *  · 回 401：重新登记成功后把原请求再发**一次**（401 说明服务端没有处理它，再发是安全的，提交登记也一样）。 */
async function authed<T>(fn: (c: PluginClient) => Promise<ApiResult<T>>, reason: string, mode: RegisterMode = "auto"): Promise<ApiResult<T>> {
  let dev = await registeredDevice();
  if (!dev) {
    if (await register(reason, mode)) dev = await registeredDevice();
    if (!dev) return { ok: false, error: await notRegisteredError() };
  }
  const epoch = reauthEpoch;
  let r = await fn(clientFor(dev));
  if (!r.ok && r.error.kind === "http" && r.error.status === 401) {
    // 这次请求发出之后别的一路已经重新登记过了：直接再发；否则自己去重新登记。
    const done = reauthEpoch !== epoch || (await reregisterAfter401(reason));
    const again = done ? await registeredDevice() : null;
    if (again) r = await fn(clientFor(again));
  }
  return r;
}

// ── 闹钟 ─────────────────────────────────────────────────────────────────

async function ensureAlarms(): Promise<void> {
  try {
    const hb = await chrome.alarms.get(ALARM_HEARTBEAT);
    if (!hb) await chrome.alarms.create(ALARM_HEARTBEAT, { periodInMinutes: 1, delayInMinutes: 0.1 });
    if (await sget<ActiveRun>(KEYS.activeRun)) await ensureRunBeatAlarm();
    if (Object.keys((await lget<ReceiptStore>(KEYS.receipts)) ?? {}).length) await ensureReceiptAlarm();
    await syncFastHeartbeat(await getConfig());
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

/** 快心跳只在「买家号还没有登记」时开，而且自这个状态出现起最多 10 分钟；其余时候关掉（60 秒一次）。 */
async function syncFastHeartbeat(cfg: ConfigCache | null): Promise<void> {
  const want = fastSyncWanted(cfg, Date.now());
  const ex = await chrome.alarms.get(ALARM_FAST_HEARTBEAT);
  if (want && !ex) {
    await chrome.alarms.create(ALARM_FAST_HEARTBEAT, { periodInMinutes: FAST_HEARTBEAT_MIN, delayInMinutes: FAST_HEARTBEAT_MIN });
  } else if (!want && ex) {
    await chrome.alarms.clear(ALARM_FAST_HEARTBEAT);
  }
}

/** 当前买家号对应的账号（只有登录状态是 ready 才有）与它的买家号 ID：定时跟着它走。 */
function scheduledAccount(cfg: ConfigCache | null): { acc: NonNullable<HeartbeatOut["account"]>; cid: string } | null {
  const hb = cfg?.heartbeat;
  if (!hb || hb.login.state !== "ready" || !hb.account) return null;
  const cid = hb.account.amazon_customer_id ?? hb.login.customer_id;
  return cid && CUSTOMER_ID_RE.test(cid) ? { acc: hb.account, cid } : null;
}

/** 按当前配置给每个执行时间设一个一次性闹钟（北京时间换算成本机时刻）。只在变化时重建。 */
async function planSchedule(): Promise<void> {
  const cfg = await getConfig();
  const cur = scheduledAccount(cfg);
  const wanted = new Map<string, number>();
  if (cur && cur.acc.purchase_mode === "scheduled") {
    for (const p of nextFirePlan(cur.acc.schedule_times, serverNow(cfg))) {
      wanted.set(ALARM_SCHED_PREFIX + p.slot, toLocalEpoch(p.at + SLOT_MARGIN_MS, cfg?.clock_offset_ms ?? 0));
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

/** 登录状态、账号变了就写一行日志（买家号 ID 在日志里用遮掩形式）。 */
async function logStatusChange(prev: HeartbeatOut | null, cur: HeartbeatOut): Promise<void> {
  const pl = prev?.login ?? null;
  const cl = cur.login;
  const acc = cur.account;
  if (!pl || pl.state !== cl.state || pl.customer_id !== cl.customer_id || (prev?.account?.id ?? null) !== (acc?.id ?? null)) {
    const label = loginLabel(cl.state, maskCustomerId(cl.customer_id), acc?.name ?? null, acc?.execution_paused === true);
    const level: LogLevel = cl.state === "ready" ? "ok" : cl.state === "unknown" ? "info" : "warn";
    const text =
      cl.state === "ready"
        ? `当前买家号 ${maskCustomerId(cl.customer_id)}：账号「${acc?.name ?? "—"}」${acc?.execution_paused ? "（已暂停）" : ""}`
        : cl.state === "unregistered"
          ? `买家号 ${maskCustomerId(cl.customer_id)} 还没有登记：在控制台「账号」页的待登记列表里点「登记」`
          : `登录状态：${label}`;
    await log(level, text);
    return;
  }
  const pa = prev?.account ?? null;
  if (pa && acc && pa.execution_paused !== acc.execution_paused) {
    await log(acc.execution_paused ? "warn" : "ok", acc.execution_paused ? `账号已暂停执行${acc.pause_reason ? `：${acc.pause_reason}` : ""}` : "账号已恢复执行");
  }
}

/** 输入：心跳或身份上报的返回（同一形状）、请求发出与收到的时刻、触发原因 → 输出：无。
 *  缓存配置、校正时钟、记连接成功、写状态变化的日志、重排定时闹钟、开关快心跳。 */
async function applyStatus(hb: HeartbeatOut, sentAt: number, now: number, reason: string): Promise<void> {
  await cfgGate.run(async () => {
    const prevCfg = await getConfig();
    const prevConn = await lget<ConnectionState>(KEYS.connection);
    const serverMs = parseIsoMs(hb.server_time);
    // 往返时间的一半近似为单程延迟。服务端没给时刻时沿用上一次的偏差。
    const offset = serverMs === null ? prevCfg?.clock_offset_ms ?? 0 : serverMs - Math.round((sentAt + now) / 2);
    const cfg: ConfigCache = { heartbeat: hb, received_at: now, clock_offset_ms: offset, unregistered_since: unregisteredSince(prevCfg, hb.login, now) };
    await lset(KEYS.config, cfg);
    await lset(KEYS.connection, { ok: true, at: now, kind: null, code: null, message: null } satisfies ConnectionState);
    if (!prevConn?.ok) await log("ok", `已连接服务端（${reason}）`);
    // 浏览器的名字只能在控制台改：心跳带回新名字时本地跟着改。
    const rec = await lget<DeviceRecord>(KEYS.device);
    if (rec && String(rec.device_id) === String(hb.device.id) && hb.device.name && rec.device_name !== hb.device.name) {
      await lset(KEYS.device, { ...rec, device_name: hb.device.name });
    }
    await logStatusChange(prevCfg?.heartbeat ?? null, hb);
    await planSchedule().catch(() => undefined);
    await syncFastHeartbeat(cfg).catch(() => undefined);
  });
}

/** 输入：触发原因、谁在要求（还没登记上时决定等不等退避）→ 输出：这次心跳是否成功。
 *  成功时缓存配置、校正时钟、重排定时闹钟、补交回执。 */
async function heartbeat(reason: string, mode: RegisterMode = "auto"): Promise<boolean> {
  const ok = await hbGate.run(async () => {
    await sset(KEYS.heartbeatTriedAt, Date.now());
    const ident = await lget<ReconciledIdentity>(KEYS.identity);
    // 只捎带新鲜的身份：几小时前读的快照不能每分钟被「重新上报」一次。
    const fresh = ident && isFresh(ident, Date.now()) ? toIdentity(ident) : null;
    const prevConn = await lget<ConnectionState>(KEYS.connection);
    const sentAt = Date.now();
    const r = await authed((c) => c.heartbeat(fresh), reason, mode);
    const now = Date.now();
    if (r.ok) {
      await applyStatus(r.data, sentAt, now, reason);
      return true;
    }
    if (r.error.code === "NOT_REGISTERED") return false; // 登记那一步已经记过连接失败
    // 401 之后重新登记没成功（登记记录已作废）：连接失败的原因是登记那一步记下的，不拿「401」盖掉它。
    if (r.error.kind === "http" && r.error.status === 401 && !(await lget<DeviceRecord>(KEYS.device))) return false;
    await lset(KEYS.connection, { ok: false, at: now, kind: r.error.kind, code: r.error.code, message: r.error.message } satisfies ConnectionState);
    if (prevConn?.ok !== false || prevConn.code !== r.error.code) await log("err", `心跳失败：${r.error.message}`);
    return false;
  });
  // 服务端连上了：顺带补交还没送到的回执（不等退避；不占心跳闸）。
  if (ok && Object.keys((await lget<ReceiptStore>(KEYS.receipts)) ?? {}).length) void drainReceipts({ ignoreBackoff: true });
  return ok;
}

async function heartbeatAndSchedule(reason: string, mode: RegisterMode = "auto"): Promise<void> {
  if (await heartbeat(reason, mode)) await checkSchedule(reason);
}

/** 买家号还没有登记（快同步期间）时的节流间隔：所有者往往刚在控制台点完「登记」就打开或刷新亚马逊页面，
 *  几秒前那次心跳的答复已经过时，不能让它挡住这一次同步。心跳闸会把并发的几次排成一队。 */
const UNREGISTERED_SYNC_MIN_AGE_MS = 1_000;

/** 输入：触发原因、间隔 → 输出：无。上一次心跳早于这个间隔就马上发一次（打开面板、回到页面、等待登记买家号）。 */
async function heartbeatIfStale(reason: string, minAgeMs: number): Promise<void> {
  const age = fastSyncWanted(await getConfig(), Date.now()) ? Math.min(minAgeMs, UNREGISTERED_SYNC_MIN_AGE_MS) : minAgeMs;
  const tried = await sget<number>(KEYS.heartbeatTriedAt);
  if (typeof tried === "number" && Date.now() - tried >= 0 && Date.now() - tried < age) return;
  await heartbeatAndSchedule(reason);
}

// ── 身份 ─────────────────────────────────────────────────────────────────

/** 多标签页对账的基准：本地有进行中的批次时 = 该批次账号的买家号 ID（开始批次时记下），否则没有。 */
async function baselineCustomerId(): Promise<string | null> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  return ar?.customer_id ?? null;
}

/** 输入：内容脚本送来的读数、发送的标签页、当前时刻 → 输出：规范化后的一份读数；形状不对为 null。 */
function toObservation(raw: IdentitySnapshot | null | undefined, tabId: number, now: number): IdentityObservation | null {
  if (!raw || typeof raw.signed_in !== "boolean") return null;
  const loaded = typeof raw.loaded_at === "number" && Number.isFinite(raw.loaded_at) ? raw.loaded_at : now;
  return {
    tab_id: tabId,
    signed_in: raw.signed_in,
    // 形状不对的买家号 ID 当作没读到（内容脚本读的时候已经校验过，这里再核一遍）。
    customer_id: raw.signed_in && typeof raw.customer_id === "string" && CUSTOMER_ID_RE.test(raw.customer_id) ? raw.customer_id : null,
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
  const snap = reconcileIdentity(store, await baselineCustomerId(), Date.now());
  if (!snap) return null;
  const prev = await lget<ReconciledIdentity>(KEYS.identity);
  await lset(KEYS.identity, snap);
  if (!sameIdentity(prev, snap) || !!prev?.conflict !== snap.conflict) {
    if (snap.conflict) {
      await log(
        "warn",
        `这个浏览器的亚马逊标签页显示的买家号不一致（其中一页：${snap.signed_in ? `买家号 ID ${maskCustomerId(snap.customer_id)}` : "未登录"}）：请刷新或关闭较早打开的亚马逊标签页`,
      );
    } else if (!snap.signed_in) {
      await log("warn", "这个浏览器未登录亚马逊");
    } else if (!snap.customer_id) {
      await log("info", "已登录亚马逊，这一页没有读到买家号 ID（打开订单页后自动读取）");
    } else {
      await log("info", `识别到亚马逊登录（买家号 ID ${maskCustomerId(snap.customer_id)}）`);
    }
  }
  return snap;
}

/** 输入：对账结论、是否强制 → 输出：这份结论算不算报到了服务端。单独上报身份（同一份 60 秒内不重复，强制时照发）。
 *  返回与心跳同形：直接拿它更新缓存，面板马上看到服务端按这份身份算出的登录状态与账号。 */
async function reportIdentity(snap: ReconciledIdentity, force: boolean): Promise<boolean> {
  // 一份几分钟前读的结论（这一轮哪个标签页都没读到）不当成「刚读到」再报：服务端看到的「最近上报」得是真的。
  // 有分歧的结论例外 —— 它只会让服务端拒绝，不会放行。
  if (!snap.conflict && !isFresh(snap, Date.now())) return false;
  const sig = JSON.stringify([snap.signed_in, snap.customer_id, snap.conflict]);
  const sent = await sget<{ sig: string; at: number }>(KEYS.identitySent);
  if (!force && sent && sent.sig === sig && Date.now() - sent.at < IDENTITY_DEDUPE_MS) return true;
  const sentAt = Date.now();
  const r = await authed((c) => c.identity(toIdentity(snap)), "身份上报");
  if (r.ok) {
    await sset(KEYS.identitySent, { sig, at: Date.now() });
    await applyStatus(r.data, sentAt, Date.now(), "身份上报");
    return true;
  }
  if (r.error.code !== "NOT_REGISTERED") await log("warn", `身份上报失败：${r.error.message}`);
  return false;
}

async function loginStateNow(): Promise<LoginStateName | null> {
  return (await getConfig())?.heartbeat.login.state ?? null;
}

/** 输入：内容脚本读到的身份、是否强制上报、发送的标签页 → 输出：服务端给的登录状态（拿不到为 null）与有没有分歧。 */
async function onIdentity(raw: IdentitySnapshot | null, force: boolean, tabId: number | undefined): Promise<{ login_state: LoginStateName | null; conflict: boolean }> {
  const snap = await identityGate.run(async () => {
    if (tabId !== undefined) {
      const obs = toObservation(raw, tabId, Date.now());
      if (obs) await editIdentityStore((s) => observeIdentity(s, obs));
    }
    return reconcileAndStore();
  });
  const reported = snap ? await reportIdentity(snap, force) : false;
  // 强制刷新而这一次没有报上去（这一页没读到身份、或结论已经过时）：拉一次心跳，拿到最新的状态与配置。
  if (force && !reported) await heartbeat("重新读取");
  return { login_state: await loginStateNow(), conflict: snap?.conflict ?? false };
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

/** 请每个亚马逊标签页重读身份（不在亚马逊页面上的、没答复的不算），对账、强制上报。
 *  own = 发起的那一页自己刚读到的身份（开始执行前）：先记下它，其余标签页再各自重读。
 *  返回对账结论（一份读数都没有为 null）与这一次有没有报上去。 */
async function readAllTabsAndReport(own?: { tabId: number; identity: IdentitySnapshot | null }): Promise<{ snap: ReconciledIdentity | null; reported: boolean }> {
  const tabs = (await amazonTabs()).filter((t) => !own || t.id !== own.tabId);
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
  if (own) readings.unshift({ tabId: own.tabId, identity: own.identity });
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

/** 开始执行前（规格 §3「开始执行」）：发起的那一页重读的身份交给后台，其余亚马逊标签页也重读、一起对账；
 *  有分歧 → 不开始（带 conflict 的结论照样报给服务端）；否则先把结论报给服务端（POST /identity），
 *  之后引擎才去申请批次。本地还有没结束的批次（含后台替它收尾、等回执送达的那段时间）时不发任何请求。 */
async function onPrepareStart(raw: IdentitySnapshot | null, tabId: number | undefined): Promise<PrepareReply> {
  const no = (code: string, why: string, extra: Partial<PrepareReply> = {}): PrepareReply => ({
    ok: false, conflict: false, customer_id: null, login_state: null, code, message: `开始执行失败：${why}`, ...extra,
  });
  const held = await settleLocalRun();
  if (held) return no("DEVICE_BUSY", "这个浏览器上一个批次还没有结束。");
  const { snap, reported } = await readAllTabsAndReport(tabId !== undefined ? { tabId, identity: raw } : undefined);
  const login_state = await loginStateNow();
  if (!snap) return no("NO_IDENTITY", "没有读到亚马逊登录状态，请刷新页面后再试");
  if (snap.conflict) {
    return no("IDENTITY_CONFLICT", "这个浏览器的亚马逊标签页显示的买家号不一致。请刷新或关闭较早打开的亚马逊标签页。", { conflict: true, login_state });
  }
  if (!snap.signed_in) return no("SIGNED_OUT", "浏览器没有登录亚马逊", { login_state });
  if (!snap.customer_id) return no("NO_CUSTOMER_ID", "没有读到买家号 ID，请打开订单页后再试", { login_state });
  if (!reported) {
    const conn = await lget<ConnectionState>(KEYS.connection);
    const why = conn && !conn.ok ? conn.message ?? codeLabel(conn.code) : "插件后台没有把登录状态报上去";
    return no(conn && !conn.ok && conn.code ? conn.code : "NOT_REGISTERED", `登录状态没有报到服务端（${why}）`, { customer_id: snap.customer_id, login_state });
  }
  return { ok: true, conflict: false, customer_id: snap.customer_id, login_state, code: null, message: null };
}

/** 执行中（提交登记之前、以及每隔几分钟）与面板的「重新读取」：每个亚马逊标签页重读身份、对账、强制上报
 *  （不发心跳，快）。服务端在领取、进度、提交登记时都要求登录状态 10 分钟内上报过。 */
async function onRefreshIdentity(): Promise<RefreshIdentityReply> {
  const { snap, reported } = await readAllTabsAndReport();
  return { reported, conflict: snap?.conflict ?? false, login_state: await loginStateNow() };
}

// ── 设置（特权：只接受扩展自己的页面）────────────────────────────────────

/** 换令牌之前的三道保护（原来挡在「解除配对」上的那三道）：正在执行、上一个批次还在收尾、有回执没送到
 *  → 不能换。换了令牌，旧服务端上那个浏览器名下的批次与回执就再也对不上了。返回不能换的原因；可以换为 null。 */
async function rotateBlockedReason(): Promise<string | null> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (ar && ar.orphaned_at === null) {
    const p = await probeEngine(ar);
    if (p !== "dead") return "正在执行，结束后再修改";
    await orphanRun(ar, "engine_gone", `执行页面上的引擎已经不在了：批次 #${ar.run_id} 中断`);
  }
  const pending = Object.keys(dropExpired(await lget<ReceiptStore>(KEYS.receipts), Date.now()).store).length;
  if (pending) return `还有 ${pending} 条结果回执没送到服务端，等服务端恢复、补交完成后再修改`;
  if (await sget<ActiveRun>(KEYS.activeRun)) return "上一个执行批次还在收尾，稍后再试";
  return null;
}

/** 换了令牌（新地址或新工作区）之后：旧服务端那一侧的本地状态全部作废。 */
async function clearServerState(): Promise<void> {
  await lremove([KEYS.device, KEYS.config, KEYS.connection, KEYS.registration, KEYS.schedule, KEYS.runView]);
  await sremove([KEYS.identitySent, KEYS.heartbeatTriedAt, KEYS.reregisteredAt]);
  for (const a of await chrome.alarms.getAll()) {
    if (a.name.startsWith(ALARM_SCHED_PREFIX) || [ALARM_RUN_BEAT, ALARM_FAST_HEARTBEAT, ALARM_REGISTER].includes(a.name)) {
      await chrome.alarms.clear(a.name);
    }
  }
}

async function onSaveSettings(patch: Partial<Settings>): Promise<{ ok: boolean; message: string }> {
  const cur = await getSettings();
  const next: Settings = { ...cur };
  if (patch.serverUrl !== undefined) {
    const url = normalizeServerUrl(String(patch.serverUrl));
    const v = validateServerUrl(url);
    if (!v.ok) return { ok: false, message: `保存失败：${v.reason}` };
    next.serverUrl = url;
  }
  if (patch.workspaceId !== undefined) {
    const w = normalizeWorkspaceId(patch.workspaceId);
    if (w === null && patch.workspaceId !== null && String(patch.workspaceId).trim() !== "") {
      return { ok: false, message: "保存失败：工作区 ID 应为正整数（控制台「设置」页可以查到）；不需要时留空" };
    }
    next.workspaceId = w;
  }
  if (patch.simulationOutcome !== undefined) next.simulationOutcome = normalizeOutcome(patch.simulationOutcome);

  const urlChanged = next.serverUrl !== cur.serverUrl;
  const wsChanged = next.workspaceId !== cur.workspaceId;
  const dev = await registeredDevice();
  // 已经登记在另一个工作区：服务端按令牌幂等，不会挪动已有的那一行，要换一个新令牌才能登记进新的工作区。
  const moveWs = wsChanged && next.workspaceId !== null && !!dev && typeof dev.rec.workspace_id === "number" && dev.rec.workspace_id !== next.workspaceId;

  if (urlChanged || moveWs) {
    const blocked = await rotateBlockedReason();
    if (blocked) return { ok: false, message: `保存失败：${blocked}` };
    // 先换令牌（连同它所属的地址，同一个事务），再存设置：旧令牌绝不发往新地址。
    if (!(await secretGate.run(() => writeSecret(generateDeviceToken(), next.serverUrl)))) {
      return { ok: false, message: `保存失败：${codeLabel("TOKEN_STORE")}` };
    }
    await lset(KEYS.settings, next);
    await clearServerState();
    await log("info", urlChanged ? `服务端地址改为 ${next.serverUrl}：换用新的令牌重新登记` : `工作区 ID 改为 ${next.workspaceId}：换用新的令牌重新登记`);
    if (await register("保存设置", "force")) {
      await heartbeatAndSchedule("保存设置");
      // 已经开着的亚马逊页面：马上把身份报上去，不等它们下一次重读。
      void readAllTabsAndReport().catch(() => undefined);
      return { ok: true, message: "已保存，并已重新登记" };
    }
    const conn = await lget<ConnectionState>(KEYS.connection);
    return { ok: true, message: `已保存。连接失败：${conn?.message ?? "未知原因"}` };
  }

  if (next.simulationOutcome !== cur.simulationOutcome) {
    const label = SIM_OUTCOMES.find((o) => o.value === next.simulationOutcome)?.label ?? next.simulationOutcome;
    await log("info", `模拟结局改为：${label}`);
  }
  await lset(KEYS.settings, next);
  // 填了（或改了）工作区 ID 而还没登记上：马上再登记一次（「系统里有多个工作区」停下的就是在等这个）。
  if (wsChanged && !dev && (await register("保存设置", "force"))) await heartbeatAndSchedule("保存设置");
  return { ok: true, message: "已保存" };
}

/** 设置页「重新登记」：带着同一个令牌立即再登记一次（幂等），然后同步心跳。 */
async function onReregister(): Promise<{ ok: boolean; message: string }> {
  if (!(await register("重新登记", "force"))) {
    const st = await lget<RegisterStatus>(KEYS.registration);
    return { ok: false, message: `重新登记失败：${st?.message ?? "未知原因"}` };
  }
  await heartbeatAndSchedule("重新登记");
  void readAllTabsAndReport().catch(() => undefined);
  return { ok: true, message: "已重新登记" };
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

/** 输入：无 → 输出：本地记着的批次里仍然占着这个浏览器的那一个；没有为 null。
 *  本地记录说在执行时不直接信：先问执行页面引擎还在不在，不在了就接管（没有待送的回执会立刻结束批次）；
 *  已经在接管的，再推一步。开始新批次（手动、定时）之前都先过这里：还占着就不发请求。 */
async function settleLocalRun(): Promise<ActiveRun | null> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (!ar) return null;
  if (ar.orphaned_at === null) {
    if ((await probeEngine(ar)) !== "dead") return ar;
    await orphanRun(ar, "engine_gone", `执行页面上的引擎已经不在了：批次 #${ar.run_id} 中断`);
  } else {
    await settleOrphan();
  }
  return (await sget<ActiveRun>(KEYS.activeRun)) ?? null;
}

/** 接管的批次：先补交它的回执（按退避）；都送到了就结束批次；还有没送到的就续租等着，
 *  租约预算用完、或接管满 CUSTODY_MAX_MS 还送不到（例如结果接口一直 5xx）则放手。 */
async function settleOrphan(): Promise<void> {
  const ar = await sget<ActiveRun>(KEYS.activeRun);
  if (!ar || ar.orphaned_at === null) return;
  await drainReceipts({ runId: ar.run_id });
  const store = await lget<ReceiptStore>(KEYS.receipts);
  const pending = pendingForRun(store, ar.run_id);
  const now = Date.now();
  const step = custodyStep(ar, now, pending);
  const minutes = leaseMinutes(ar.lease_ttl_s);
  if (step === "finish") {
    const r = await authed((c) => c.finishRun(ar.run_id, ar.orphan_reason ?? "engine_gone"), "结束批次");
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
  const r = await authed((c) => c.runHeartbeat(ar.run_id), "批次心跳");
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
  const r = await authed((c) => c.runHeartbeat(ar.run_id), "批次心跳");
  let stop: string | null = null;
  if (r.ok) {
    await touchRun(ar.tab_id, true);
    if (r.data.stop) stop = `服务端要求停止：${stopReasonLabel(r.data.stop_reason)}`;
  } else if (r.error.kind === "http" && !(r.error.status !== null && r.error.status >= 500)) {
    // 执行权已经没了（LEASE_LOST / 404；重新登记之后仍是 401）：本地也不再认这个批次。引擎下一次请求会自己停下。
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
    if (!(await registeredDevice())) return; // 还没连上服务端：回执留着，连上后的第一次心跳会来补交
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
      const r = await authed((c) => c.result(rc.attempt_id, rc.body), "补交回执");
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

/** 开始批次（规格 §3「开始执行」）。请求发出去之前本地先过三关，任何一关不过都**不发请求**：
 *    1. 本地还有没结束的批次（含后台替它收尾、等回执送达的那段时间）→ DEVICE_BUSY；
 *    2. 买家号 ID 的形状不对 → VALIDATION；
 *    3. 它不是后台刚对账出的那一个（有分歧、未登录、ID 不同）→ 不拿一个对不上的 ID 去领别的账号的任务。 */
async function onStartRun(a: unknown[], tabId: number | undefined): Promise<ApiResult<unknown>> {
  if (tabId === undefined) return { ok: false, error: localError("VALIDATION") };
  const trigger: Trigger = a[0] === "scheduled" ? "scheduled" : "manual";
  const customerId = typeof a[1] === "string" ? a[1].trim().toUpperCase() : "";
  if (!CUSTOMER_ID_RE.test(customerId)) return { ok: false, error: localError("NO_CUSTOMER_ID") };
  const sp = trigger === "scheduled" ? parseSlot(a[2]) : null;
  const slot = sp ? `${String(sp.h).padStart(2, "0")}:${String(sp.m).padStart(2, "0")}` : null;
  if (trigger === "scheduled" && !slot) return { ok: false, error: localError("VALIDATION", "定时执行需要带上时间点") };

  const held = await settleLocalRun();
  if (held) {
    return {
      ok: false,
      error: localError("DEVICE_BUSY", undefined, { run_id: held.run_id, account_name: held.account_name ?? null, since: new Date(held.started_at).toISOString(), lease_expires_at: null }),
    };
  }
  const ident = await lget<ReconciledIdentity>(KEYS.identity);
  if (!ident) return { ok: false, error: localError("NO_IDENTITY") };
  if (ident.conflict) return { ok: false, error: localError("IDENTITY_CONFLICT") };
  if (!ident.signed_in) return { ok: false, error: localError("SIGNED_OUT") };
  if (ident.customer_id !== customerId) {
    return { ok: false, error: localError("IDENTITY_MISMATCH", "页面上的买家号与插件最近上报的不一致，请刷新亚马逊页面后重试") };
  }

  const r = await authed((c) => c.startRun(trigger, customerId, slot), "开始批次");
  if (r.ok) {
    const now = Date.now();
    await sset(KEYS.activeRun, {
      run_id: r.data.run_id,
      tab_id: tabId,
      trigger,
      started_at: now,
      touched_at: now,
      lease_ttl_s: r.data.lease_ttl_s ?? null,
      last_beat_ok_at: now,
      orphaned_at: null,
      orphan_reason: null,
      customer_id: customerId,
      account_name: r.data.account?.name ?? null,
    } satisfies ActiveRun);
    await ensureRunBeatAlarm();
  }
  return r;
}

/** 输入：协议操作、参数、发起的标签页 → 输出：ApiResult（可序列化，原样回给引擎）。
 *  每一个请求都经 authed：还没登记上先登记；回 401 就重新登记后再发一次。 */
async function onApi(op: ApiOp, args: unknown[], tabId: number | undefined): Promise<ApiResult<unknown>> {
  const a = Array.isArray(args) ? args : [];
  const num = (i: number): number => Number(a[i]);
  let r: ApiResult<unknown>;
  switch (op) {
    case "startRun":
      return onStartRun(a, tabId);
    case "runHeartbeat":
      r = await authed((c) => c.runHeartbeat(num(0)), "批次心跳");
      await touchRun(tabId, r.ok);
      return r;
    case "next":
      r = await authed((c) => c.next(num(0)), "领取");
      break;
    case "progress":
      r = await authed((c) => c.progress(num(0), String(a[1] ?? ""), (a[2] as Record<string, unknown>) ?? {}), "进度");
      break;
    case "guard":
      r = await authed((c) => c.guard(num(0), a[1] as Parameters<PluginClient["guard"]>[1]), "护栏");
      break;
    case "submitting":
      r = await authed((c) => c.submitting(num(0)), "提交登记");
      break;
    case "verifying":
      r = await authed((c) => c.verifying(num(0), String(a[1] ?? "")), "等待验证");
      break;
    case "result": {
      // 先存回执再发：这一路断了（后台被回收、服务端重启），回执还在，后台补交。
      const attemptId = num(0);
      const body = a[1] as ResultIn;
      const ar = await sget<ActiveRun>(KEYS.activeRun);
      await saveReceipt(attemptId, ar && ar.tab_id === tabId ? ar.run_id : null, body);
      r = await authed((c) => c.result(attemptId, body), "上报结果");
      await settleReply(attemptId, r, false);
      break;
    }
    case "finishRun": {
      r = await authed((c) => c.finishRun(num(0), String(a[1] ?? "")), "结束批次");
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
  // 打开亚马逊页面 = 「环境上线」：同步一次（还没登记上就先登记；拿到刚在控制台登记的账号与改过的配置），
  // 顺带检查今天错过的定时。
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

/** 输入：触发原因 → 输出：无。检查当前买家号对应的账号今天有没有到点、还没处理的执行时间；
 *  有就请一个登着这个买家号的亚马逊标签页以 trigger = scheduled、带着时间点去申请批次。
 *
 *  「一个时间点一天只执行一次」的权威在服务端（按账号、北京日期、时间点去重）；本地的当天记录按买家号 ID
 *  分键，只用来少发请求。怎么记「处理过了」（规格 §3）：
 *    · 已开始、服务端说这个时间点今天执行过（SLOT_DONE）、账号已暂停 → 记；
 *    · 服务端说参数不对（422：账号改成手动了、时间点删了）→ 先刷新配置，这个时间点记为处理过；
 *    · 这个买家号正在别处执行（ACCOUNT_BUSY）、这个浏览器上一个批次还没结束（DEVICE_BUSY）、没说上话、
 *      登录状态对不上 → **不记**，下一分钟再问；
 *    · 没有登着这个买家号的亚马逊页面 → 只记一次「等待环境」，今天内打开后补执行（跨天不补）。 */
async function checkSchedule(reason: string): Promise<void> {
  await schedGate.run(async () => {
    if (!(await registeredDevice())) return;
    const cfg = await getConfig();
    const cur = scheduledAccount(cfg);
    if (!cur || cur.acc.purchase_mode !== "scheduled") return;
    const { acc, cid } = cur;
    const now = serverNow(cfg);
    const rawBook = await lget<unknown>(KEYS.schedule);
    const book = normalizeBook(rawBook);
    const st = bookDay(book, cid, now);
    const save = async (next: ScheduleDayState): Promise<void> => {
      const out = bookPut(book, cid, next, now);
      if (JSON.stringify(out) !== JSON.stringify(rawBook)) await lset(KEYS.schedule, out);
    };
    const due = dueSlots(acc.schedule_times, now, st).filter((s) => (slotEpoch(st.bj_date, s) ?? Infinity) + SLOT_MARGIN_MS <= now);
    const slot = slotToClaim(due);
    if (slot === null) return save(st);
    const label = `定时 ${due.join("、")}`;
    const done = async (level: LogLevel, text: string): Promise<void> => {
      await save(markSlots(st, due, now));
      await log(level, `${label}：${text}`);
    };
    /** 不算处理过，下一分钟再问；同一个时间点只写一次日志，不刷屏。 */
    const later = async (text: string): Promise<void> => {
      const fresh = due.filter((s) => !st.waiting_logged.includes(s));
      if (!fresh.length) return save(st);
      await save(markSlots(st, fresh, now, "waiting_logged"));
      await log("warn", `${label}：${text}`);
    };

    if (acc.execution_paused) return done("warn", "账号已暂停执行，今天这次不执行");
    // 本地记录说还有批次：先问执行页面引擎还在不在（不信一个可能已经过时的标记）；还占着就等它结束。
    if (await settleLocalRun()) return later("暂未开始（这个浏览器上一个批次还没有结束），稍后自动重试");

    let wrongBuyer = false;
    for (const t of await amazonTabs()) {
      let reply: ScheduledReply | null = null;
      try {
        const msg: TabMsg = { type: "pc.startScheduled", slot, slots: due, customer_id: cid };
        reply = (await withTimeout(chrome.tabs.sendMessage(t.id!, msg), SCHEDULE_REPLY_MS, "等待页面答复超时")) as ScheduledReply | null;
      } catch {
        reply = null; // 这个标签页没有内容脚本（插件装好之前就打开了）或答复超时
      }
      if (!reply || typeof reply.result !== "string") continue;
      const verdict = scheduleVerdict(reply);
      if (verdict === "next_tab") {
        wrongBuyer = true; // 这一页登的不是这个买家号，或这一页读不到买家号 ID：换下一个标签页
        continue;
      }
      if (verdict === "done") return done(reply.result === "started" ? "ok" : "info", reply.message);
      if (verdict === "refresh") {
        await done("warn", `${reply.message}（已刷新配置，今天这个时间点不再触发）`);
        await heartbeat("定时被拒后刷新配置");
        return;
      }
      return later(`暂未开始（${reply.message}），稍后自动重试`);
    }
    await later(
      wrongBuyer
        ? `已到点：打开的亚马逊页面登的不是这个买家号，或没有读到买家号 ID，等待环境（今天内用这个买家号打开订单页后自动补执行；触发：${reason}）`
        : `已到点：没有打开的亚马逊页面，等待环境（今天内打开亚马逊页面后自动补执行；触发：${reason}）`,
    );
  });
}

// ── 事件接线 ─────────────────────────────────────────────────────────────

/** 所有者点了面板上的链接：后台新开一个标签页。地址只有两个固定的去处，消息里不带地址。 */
async function openTab(url: string): Promise<{ ok: boolean }> {
  try {
    await chrome.tabs.create({ url });
    return { ok: true };
  } catch {
    return { ok: false };
  }
}

async function handle(msg: Msg, sender: chrome.runtime.MessageSender): Promise<unknown> {
  // 扩展自己的页面（设置页开在标签页里时也带着 sender.tab）不是亚马逊标签页：不当成内容脚本的标签页用
  // （租约、身份读数、批次都只认亚马逊页面上的内容脚本）。
  const fromOwnPage = typeof sender.url === "string" && sender.url.startsWith(EXT_ROOT);
  const tabId = fromOwnPage ? undefined : sender.tab?.id;
  // 特权消息（保存设置、重新登记、清日志）只接受扩展自己的页面：跑在亚马逊页面里的内容脚本不能改服务端地址。
  if (PRIVILEGED_TYPES.has(String(msg?.type)) && !isPrivilegedSender(sender, EXT_ROOT)) {
    return { ok: false, message: "操作失败：这个操作只能在插件的设置页里做" };
  }
  switch (msg?.type) {
    case "pc.getState":
      return snapshot();
    case "pc.heartbeatNow":
      await heartbeatAndSchedule("立即同步", "manual");
      return snapshot();
    case "pc.retry":
      await heartbeatAndSchedule("重试", "manual");
      return snapshot();
    case "pc.sync":
      await heartbeatIfStale(String(msg.reason ?? "同步"), Number.isFinite(msg.minAgeMs) ? Number(msg.minAgeMs) : SYNC_MIN_AGE_MS);
      return snapshot();
    case "pc.prepareStart":
      return onPrepareStart(msg.identity ?? null, tabId);
    case "pc.refreshIdentity":
      return onRefreshIdentity();
    case "pc.saveSettings":
      return onSaveSettings(msg.patch ?? {});
    case "pc.reregister":
      return onReregister();
    case "pc.clearLogs":
      await logGate.run(() => lset(KEYS.logs, []));
      return { ok: true, message: "已清空日志" };
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
    case "pc.openConsole":
      return openTab(consoleAccountsUrl((await getSettings()).serverUrl));
    case "pc.openOrders":
      return openTab(ORDER_HISTORY_URL);
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
  else if (a.name === ALARM_FAST_HEARTBEAT) void heartbeatAndSchedule("等待登记买家号");
  else if (a.name === ALARM_REGISTER) void heartbeatAndSchedule("重试连接");
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

chrome.runtime.onInstalled.addListener(() => {
  // 插件安装、更新、重新加载：storage.session（当前批次）已清空，旧页面里的引擎也已和后台断开，
  // 本地留下的「正在执行」不可能还成立。
  // 安装后**不**打开设置页：没有必填项（服务端地址有缺省值），装好就自动登记；
  // 经 AdsPower 分发时每个环境首次启动都弹一个设置页只会添乱。
  void markRunViewInactive(null);
  void ensureAlarms();
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
