/** 执行引擎：按契约 §5 一步一步跑一个执行批次（本阶段只做模拟）。
 *
 * 不碰任何 chrome API，也不碰页面：依赖全部从构造参数注入（RunApi、时钟、日志、定时器），
 * 所以能在 Node 里对着内存假服务端整条跑通（test/engine.test.mjs）。
 * 在浏览器里它跑在亚马逊标签页的内容脚本中，所有请求经由后台转发（content/remote-api.ts）。
 *
 * 流程（契约 §5.4 – §5.12）：
 *   开始批次（{customer_id: 当前页读到的买家号 ID, trigger, slot?, simulated: true}）
 *   → 循环 [批次心跳 → 领取 → 进度 → （护栏）→ 提交登记 → 结果] → 领不到（attempt: null）为止 → 结束批次。
 *   服务端按买家号 ID 找账号；没有设备与账号的绑定。
 *
 * 几条不能退让的规矩：
 *   · **提交登记绝不自动重试**，也只认 HTTP 200；登记之后的任何不顺利都不再「重来」，
 *     结果上报 unknown 或交给服务端清扫（已登记 → 结果待核实，账号暂停）。
 *     登记的响应丢了、浏览器把请求重发、服务端回「已登记」（409，attempt_status=submitting）：
 *     这就是「已登记、没点击」，上报 unknown 并结束本批次，绝不当成「被拒」。
 *   · **结果一定送到。** 结果上报没说上话或服务端 5xx：在租约预算之内按退避重发**同一份**结果，
 *     期间照常续租；后台在发出之前已经存了回执，引擎放弃之后它接着补交。
 *     结果没送到之前**绝不结束批次**：结束批次会把在途的已登记尝试判成「结果待核实」。
 *   · **其他没说上话就停。** 领取、护栏、提交登记的响应丢了，引擎不再继续、也不再续租，
 *     由服务端按契约清扫：未登记提交 → aborted、任务回到已分配；
 *     已登记提交 → unknown、账号暂停。这条路径叫「中断」（abandoned）。
 *   · **服务端是全局权威。** 409 ACCOUNT_BUSY 显示这个买家号正在哪个浏览器里执行、几点开始；
 *     409 DEVICE_BUSY 说明这个浏览器上一个批次还没有结束；
 *     LEASE_LOST 立即停止；批次心跳 stop=true 在当前这一笔结束后停止并结束批次。
 *     续租节奏按服务端给的租约有效期（lease_ttl_s）算：min(35 秒, 有效期 / 3)，每一步之前到点就补一次。
 *   · **身份要新鲜。** 服务端在开始批次、领取、进度与提交登记时都要求这个浏览器的登录状态在 identity_fresh_s
 *     （默认 600 秒）内上报过、而且登录的买家号就是批次账号登记的那个：引擎在批次进行中每 identityEveryMs
 *     （默认 3 分钟）请宿主重读并上报一次身份，并且在提交登记之前（登记前最后一次进度确认之前）无条件再报一次
 *     —— 读不到、报不上不影响流程，服务端是裁决者：登录状态过期或买家号变了时它让进度 continue=false
 *     （IDENTITY_STALE / IDENTITY_MISMATCH）。
 *   · **护栏事实带买家号 ID**（取事实那一刻页面上读到的；模拟执行时取当前页面的读数）：服务端的 buyer 项
 *     把「谁在下单」钉在下单那一页，读不到按数据不可靠拦截（一般的拦截：任务转异常，批次继续），对不上按
 *     IDENTITY_MISMATCH 拦截。后一种是中止类，而且**服务端自己把批次结束**（契约 §4 / §5）：尝试记为中止、任务回
 *     已分配（不进异常）、**不暂停账号**、批次的结束原因记 identity_mismatch。提交登记前的复核拦下时也一样
 *     （409 GUARD_BLOCKED，detail.block_code = IDENTITY_MISMATCH、detail.run_ended = true）。引擎这时
 *     **不上报这一笔的结果、不再领取、也不调用结束批次**（服务端都已经做完，再问只会得到 409），以
 *     「浏览器登录的买家号变了，这一笔在提交前中止」结束本批次；重新开始批次后会再领到这一笔。
 *     拦截的答复丢了、请求被重发时拿到的是 409 LEASE_LOST（detail.end_reason = identity_mismatch），同样处理。
 *   · **服务端要求中止时按原因码上报**（cancel_code，契约 §5 第 7 步）：只有 UPSTREAM_CANCELLED 报
 *     ABORTED_UPSTREAM_CANCELLED；ATTEMPT_FINISHED 不再上报；其余报 ABORTED_BY_SERVER（中止类，任务回到已分配）。
 *   · 本阶段尝试是「原子」的：模拟的一笔在毫秒级内完成，批次心跳的 stop 在两笔之间生效。
 *     真实驱动接入时，需要在每一步之间检查 stop（见 README「后续」）。
 *   · 所有等待都有界：每个请求有超时，结果重发有租约预算与总时长上限，单批次最多领取 maxAttempts 笔；
 *     领取时服务端说「稍后再领」（retry_after_s）最多等 6 轮、每轮最多 30 秒，等待期间照常续租。
 */

import type { ApiError, RunApi } from "./client.js";
import { parseBusyHolder, parseDeviceBusy, parseIdentityRejection } from "./client.js";
import { ACCOUNT_LEVEL_CODES, BUYER_CHANGED_TEXT, abortCodeFor, cancelLabel, codeLabel, stageLabel, stopReasonLabel } from "./codes.js";
import type { LogLevel } from "./log.js";
import { isLateReceipt, isRetryableResultError } from "./receipts.js";
import {
  buildCheckoutFacts,
  failureResult,
  simOrderNo,
  successResult,
  type SimOutcome,
} from "./simulate.js";
import { busyElsewhereText } from "./status.js";
import { bjHm } from "./time.js";
import type { Attempt, BusyHolder, ResultIn, Trigger } from "./types.js";

export type EnginePhase = "idle" | "starting" | "running" | "finishing" | "ended";

/** 批次怎么结束的。 */
export type RunEndStatus =
  /** 正常结束：领不到新尝试，或按要求停下，并已调用结束批次；或者服务端自己把批次结束了
   *  （护栏 buyer 项拦截，reason = buyer_changed），这时不再调用结束批次。 */
  | "finished"
  /** 409 ACCOUNT_BUSY（这个买家号正在别的浏览器里执行）或 DEVICE_BUSY（这个浏览器上一个批次还没结束）：本次没有开始。 */
  | "busy"
  /** 服务端拒绝开始（买家号没登记、登录状态对不上、账号暂停、这个时间点今天执行过……）或没说上话。 */
  | "rejected"
  /** 中途没说上话或执行权已失效：不再续租、不再调用，交给服务端清扫。 */
  | "abandoned";

export interface RunCounts {
  attempts: number;
  succeeded: number;
  failed: number;
  blocked: number;
  unknown: number;
  aborted: number;
}

export interface AttemptView {
  attempt_id: number;
  upstream_order_no: string | null;
  asin: string | null;
  quantity: number | null;
}

/** 给面板看的引擎状态。 */
export interface EngineView {
  phase: EnginePhase;
  trigger: Trigger | null;
  run_id: number | null;
  attempt: AttemptView | null;
  stage: string | null;
  stage_label: string | null;
  counts: RunCounts;
  started_at: number | null;
  message: string | null;
}

export interface RunSummary {
  status: RunEndStatus;
  /** 结束原因码（finish 的 reason，或开始失败的错误码）。 */
  reason: string;
  trigger: Trigger;
  run_id: number | null;
  counts: RunCounts;
  error: ApiError | null;
  busy: BusyHolder | null;
  /** 一句中文总结，面板与日志直接用。 */
  message: string;
}

/** 开始批次那一步的结论（给定时触发方判断「这一次算不算处理过」）。 */
export interface StartNotice {
  started: boolean;
  run_id: number | null;
  status: RunEndStatus | null;
  error: ApiError | null;
  message: string;
  /** 占着的是这个浏览器自己（DEVICE_BUSY：上一次的批次还没结束或租约还没到期）：过一会儿自然会好。 */
  busy_mine: boolean;
}

export interface Timers {
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

export interface EngineDeps {
  api: RunApi;
  outcome: SimOutcome;
  now?: () => number;
  log?: (level: LogLevel, text: string) => void;
  onView?: (v: EngineView) => void;
  timers?: Timers;
  /** 等待（结果重发的退避、领取的稍后再试）。默认真的 setTimeout。 */
  sleep?: (ms: number) => Promise<void>;
  /** 批次心跳间隔上限，默认 35 秒；实际取 min(它, 租约有效期 / 3)
   *  （契约租约 180 秒；后台标签页定时器最慢约 60 秒一次，所以每一步之前还会按时钟补发）。 */
  heartbeatEveryMs?: number;
  /** 单批次最多领取多少笔，默认 200。防止服务端异常时无限循环。 */
  maxAttempts?: number;
  /** 一笔结果最多重发多久（从第一次失败算），默认 10 分钟；租约预算先到就先停。 */
  reportRetryMaxMs?: number;
  /** 这个浏览器在服务端的 ID：ACCOUNT_BUSY 的持有者就是自己时按「本浏览器未结束」说明。 */
  ownDeviceId?: number | string | null;
  /** 重读本浏览器的亚马逊登录身份并上报服务端（`POST /plugin/v1/identity`）。提交登记之前必调，
   *  批次进行中每 identityEveryMs 补一次。没有它（或它失败）时引擎照常走，由服务端按身份新鲜度裁决。 */
  refreshIdentity?: () => Promise<unknown>;
  /** 取结算事实那一刻页面上读到的买家号 ID（模拟执行时就是当前页面的读数）；读不到返回 null。
   *  没有它时事实里的 customer_id 为 null，服务端护栏按数据不可靠拦截 —— 不拿开始批次时的那个 ID 顶替。 */
  readCustomerId?: () => string | null;
  /** 批次进行中多久补报一次身份，默认 3 分钟（服务端要求 10 分钟内上报过）。 */
  identityEveryMs?: number;
}

/** 契约 §5.4 的租约有效期默认值（服务端没给 lease_ttl_s 时用）。 */
export const DEFAULT_LEASE_TTL_S = 180;
/** 一笔结果最多重发多久（reportRetryMaxMs 的默认值）。后台接管批次之后续租补交的总时长也按它封顶（core/custody.ts）。 */
export const REPORT_RETRY_MAX_MS = 10 * 60_000;
/** 结果重发时给租约留的余量：离租约到期不到这么久就不再等下一次。 */
const LEASE_MARGIN_MS = 10_000;
/** 结果重发的退避节奏。 */
const REPORT_BACKOFF_MS = [2_000, 5_000, 10_000, 20_000, 30_000];
/** 领取时服务端说「稍后再领」（retry_after_s），最多等几轮、每轮最多等多久。 */
const CLAIM_RETRY_MAX = 6;
const CLAIM_RETRY_CAP_MS = 30_000;
const CLAIM_RETRY_MIN_MS = 1_000;
/** 批次进行中补报身份的默认间隔。 */
const IDENTITY_EVERY_MS = 3 * 60_000;

/** 输入：服务端的 retry_after_s → 输出：这一轮实际等多久（毫秒，夹在 1 – 30 秒之间）。 */
export function claimWaitMs(retryAfterS: number): number {
  const ms = Number.isFinite(retryAfterS) ? Math.ceil(retryAfterS * 1000) : CLAIM_RETRY_CAP_MS;
  return Math.min(CLAIM_RETRY_CAP_MS, Math.max(CLAIM_RETRY_MIN_MS, ms));
}

/** 批次结束原因（finish 的 detail）里没有错误详情时，给人看的中文。 */
const END_REASON_TEXT: Record<string, string> = {
  plugin_internal: "执行引擎内部错误",
  network: "无法连接服务端",
  lease_lost: "执行权已失效",
  result_not_delivered: "结果暂时送不到服务端",
  progress_not_delivered: "进度上报没有送到服务端",
  guard_not_delivered: "护栏请求没有送到服务端",
  submitting_unconfirmed: "提交登记没有得到确认",
};

/** 输入：租约有效期（秒）→ 输出：「约 N 分钟」里的 N（至少 1）。 */
export function leaseMinutes(ttlS: number | null | undefined): number {
  return Math.max(1, Math.round((ttlS ?? DEFAULT_LEASE_TTL_S) / 60));
}

/** 输入：心跳间隔上限、租约有效期（秒）→ 输出：实际的批次心跳间隔（毫秒）。 */
export function beatInterval(capMs: number, ttlS: number | null | undefined): number {
  const ttl = (ttlS ?? DEFAULT_LEASE_TTL_S) * 1000;
  return Math.max(1_000, Math.min(capMs, Math.floor(ttl / 3)));
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** 输入：409 的 detail → 输出：服务端说的尝试状态（attempt_status）；没有为 null。 */
function attemptStatusOf(e: ApiError): string | null {
  return isObj(e.detail) && typeof e.detail.attempt_status === "string" ? e.detail.attempt_status : null;
}

/** 护栏 buyer 项拦截时服务端结束批次所写的结束原因（服务端 execution.BUYER_BLOCK_END_REASON）。 */
const BUYER_END_REASON = "identity_mismatch";
/** 护栏 buyer 项的拦截码（服务端 execution.BUYER_BLOCK_CODE）。 */
const BUYER_BLOCK_CODE = "IDENTITY_MISMATCH";

/** 输入：某一步的失败 → 输出：是不是「服务端已经因为护栏 buyer 项拦截把这个批次结束了」
 *  （409 LEASE_LOST，detail.end_reason = identity_mismatch）。护栏或提交登记的第一次答复丢了、
 *  请求被重发时会拿到它：第一次已经把这一笔中止、把批次结束了。 */
function endedForBuyer(e: ApiError): boolean {
  return e.kind === "http" && e.code === "LEASE_LOST" && isObj(e.detail) && e.detail.end_reason === BUYER_END_REASON;
}

/** 输入：提交登记的失败 → 输出：是不是「提交前复核时护栏的 buyer 项拦下，服务端已经中止这一笔并结束了批次」
 *  （409 GUARD_BLOCKED，detail.block_code = IDENTITY_MISMATCH，且 detail.run_ended 为 true 或 attempt_status 为 aborted）。
 *  只回 {block_code, reasons}、什么都没改的服务端（旧版本）不算：那时照一般的复核拦截走。 */
function buyerBlockedAtSubmit(e: ApiError): boolean {
  if (e.kind !== "http" || e.code !== "GUARD_BLOCKED" || !isObj(e.detail)) return false;
  return e.detail.block_code === BUYER_BLOCK_CODE && (e.detail.run_ended === true || e.detail.attempt_status === "aborted");
}

const zeroCounts = (): RunCounts => ({ attempts: 0, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 0 });

const NO_TIMERS: Timers = {
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
};

/** 输入：IDENTITY_MISMATCH 的错误 → 输出：原因（不带「开始执行失败：」）。服务端的原话按情况已经写清楚了；
 *  没有原话时按 detail {login_state, stale} 说。 */
function identityRejectionText(err: ApiError): string {
  if (err.message && err.message !== codeLabel(err.code)) return err.message;
  const d = parseIdentityRejection(err.detail);
  if (d.login_state === "signed_out") return "浏览器没有登录亚马逊";
  if (d.login_state === "conflict") return "这个浏览器的亚马逊标签页显示的买家号不一致，请刷新或关闭较早打开的标签页";
  if (d.stale) return "插件太久没有上报登录状态，请打开或刷新亚马逊页面后重试";
  if (d.login_state === "no_id" || d.login_state === "unknown") return "插件还没有读到这个浏览器登录的买家号，请打开订单页后重试";
  return "页面上的买家号与插件最近上报的不一致，请刷新亚马逊页面后重试";
}

const deviceBusyText = (accountName: string | null, since: string | null): string => {
  const at = bjHm(since);
  const which = accountName || at ? `批次${accountName ? `属于账号「${accountName}」` : ""}${at ? `（${at} 开始）` : ""}，` : "";
  return `开始执行失败：这个浏览器上一个批次还没有结束。${which}页面刷新或异常退出留下的批次会在租约到期后自动释放`;
};

/** 输入：开始批次失败的错误、这个浏览器的 ID → 输出：给人看的一句话（规格 §5 状态文案表）、busy 详情、
 *  占着的是不是这个浏览器自己。 */
export function describeStartError(err: ApiError, ownDeviceId: number | string | null = null): { message: string; busy: BusyHolder | null; mine: boolean } {
  if (err.code === "DEVICE_BUSY") {
    const d = parseDeviceBusy(err.detail);
    return { busy: null, mine: true, message: deviceBusyText(d.account_name, d.since) };
  }
  if (err.code === "ACCOUNT_BUSY") {
    const busy = parseBusyHolder(err.detail);
    const mine = ownDeviceId !== null && busy.holder_device_id !== null && String(busy.holder_device_id) === String(ownDeviceId);
    // 持有者就是这个浏览器：正常情况下服务端先回 DEVICE_BUSY，这里只是并发时的另一种问法。
    if (mine) return { busy, mine, message: deviceBusyText(null, busy.since) };
    return { busy, mine, message: `开始执行失败：${busyElsewhereText(busy.holder_device_name, busy.since)}。` };
  }
  if (err.code === "IDENTITY_MISMATCH") {
    return { busy: null, mine: false, message: `开始执行失败：${identityRejectionText(err)}` };
  }
  if (err.code === "UNAUTHORIZED") {
    return { busy: null, mine: false, message: "开始执行失败：服务端不认识这个浏览器，插件正在重新登记，请稍后再点一次" };
  }
  if (GUIDED_START_CODES.has(err.code)) {
    // 这几种拒绝要人去做一件具体的事：用插件这边写好了处置的说明，服务端原话（如暂停原因）附在后面。
    const label = codeLabel(err.code);
    const extra = err.message && err.message !== label ? `（服务端：${err.message}）` : "";
    return { busy: null, mine: false, message: `开始执行失败：${label}${extra}` };
  }
  return { busy: null, mine: false, message: `开始执行失败：${err.message || codeLabel(err.code)}` };
}

/** 开始批次被拒时，插件给出处置说明的几个错误码（契约 §5.4 的前置条件）。 */
const GUIDED_START_CODES: ReadonlySet<string> = new Set(["ACCOUNT_NOT_REGISTERED", "ACCOUNT_PAUSED", "REAL_PURCHASE_DISABLED"]);

export class RunEngine {
  private readonly now: () => number;
  private readonly log: (level: LogLevel, text: string) => void;
  private readonly timers: Timers;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly beatCap: number;
  private readonly maxAttempts: number;
  private readonly reportRetryMaxMs: number;
  private readonly identityEvery: number;
  /** 上一次补报身份的时刻（开始批次之前宿主刚报过一次，所以从开始那一刻算）。 */
  private lastIdentityAt = 0;
  private beatEvery: number;
  private leaseTtlS: number = DEFAULT_LEASE_TTL_S;

  private view: EngineView = {
    phase: "idle",
    trigger: null,
    run_id: null,
    attempt: null,
    stage: null,
    stage_label: null,
    counts: zeroCounts(),
    started_at: null,
    message: null,
  };
  private stopReason: string | null = null;
  private stopNoFinish = false;
  /** 上一次尝试续租的时刻（不论成败）：到点补发看它。 */
  private lastBeatAt = 0;
  /** 上一次确认续租成功（或开始批次）的时刻：租约至少到这里 + 有效期。 */
  private lastBeatOkAt = 0;
  private beating: Promise<void> | null = null;
  private runId: number | null = null;

  constructor(private readonly deps: EngineDeps) {
    this.now = deps.now ?? (() => Date.now());
    this.log = deps.log ?? (() => undefined);
    this.timers = deps.timers ?? NO_TIMERS;
    this.sleep = deps.sleep ?? ((ms) => new Promise<void>((r) => setTimeout(r, ms)));
    this.beatCap = deps.heartbeatEveryMs ?? 35_000;
    this.beatEvery = beatInterval(this.beatCap, DEFAULT_LEASE_TTL_S);
    this.maxAttempts = deps.maxAttempts ?? 200;
    this.reportRetryMaxMs = deps.reportRetryMaxMs ?? REPORT_RETRY_MAX_MS;
    this.identityEvery = deps.identityEveryMs ?? IDENTITY_EVERY_MS;
  }

  /** 输入：无 → 输出：当前引擎状态的拷贝。 */
  state(): EngineView {
    return { ...this.view, counts: { ...this.view.counts }, attempt: this.view.attempt ? { ...this.view.attempt } : null };
  }

  /** 输入：停止原因 → 输出：无。在当前这一笔结束后停止并结束批次（用户点「停止」、本页租约丢失等）。 */
  requestStop(reason: string): void {
    if (this.stopReason === null) {
      this.stopReason = reason;
      this.log("warn", `收到停止请求（${reason}），当前这一笔结束后停止`);
    }
  }

  private emit(patch: Partial<EngineView>): void {
    this.view = { ...this.view, ...patch };
    this.deps.onView?.(this.state());
  }

  private summary(status: RunEndStatus, reason: string, trigger: Trigger, error: ApiError | null, busy: BusyHolder | null, message: string): RunSummary {
    return { status, reason, trigger, run_id: this.runId, counts: { ...this.view.counts }, error, busy, message };
  }

  /** 输入：触发方式、当前页面读到的买家号 ID、开始那一步的回调、定时触发的时间点（`HH:MM`，手动为 null）
   *  → 输出：批次总结。一个引擎实例只跑一个批次。 */
  async run(trigger: Trigger, customerId: string, onStarted?: (n: StartNotice) => void, slot: string | null = null): Promise<RunSummary> {
    if (this.view.phase !== "idle") {
      throw new Error("一个 RunEngine 只能跑一个批次");
    }
    this.emit({ phase: "starting", trigger, started_at: this.now(), message: "正在申请执行批次" });
    this.log("info", `申请执行批次（${trigger === "scheduled" ? `定时${slot ? ` ${slot}` : ""}` : "手动"}，模拟）`);

    const start = await this.deps.api.startRun(trigger, customerId, trigger === "scheduled" ? slot : null);
    if (!start.ok) {
      const { message, busy, mine } = describeStartError(start.error, this.deps.ownDeviceId ?? null);
      const status: RunEndStatus = start.error.code === "ACCOUNT_BUSY" || start.error.code === "DEVICE_BUSY" ? "busy" : "rejected";
      this.log(status === "busy" ? "warn" : "err", message);
      this.emit({ phase: "ended", message });
      onStarted?.({ started: false, run_id: null, status, error: start.error, message, busy_mine: mine });
      return this.summary(status, start.error.code, trigger, start.error, busy, message);
    }

    this.runId = start.data.run_id;
    this.leaseTtlS = start.data.lease_ttl_s ?? DEFAULT_LEASE_TTL_S;
    this.beatEvery = beatInterval(this.beatCap, this.leaseTtlS);
    this.lastBeatAt = this.now();
    this.lastBeatOkAt = this.lastBeatAt;
    this.lastIdentityAt = this.lastBeatAt;
    this.emit({ phase: "running", run_id: this.runId, message: `执行批次 #${this.runId} 已开始` });
    const accName = start.data.account?.name;
    this.log("ok", `执行批次 #${this.runId} 已开始（模拟${accName ? `，账号「${accName}」` : ""}）`);
    onStarted?.({ started: true, run_id: this.runId, status: null, error: null, message: `执行批次 #${this.runId} 已开始`, busy_mine: false });

    const handle = this.timers.setInterval(() => void this.beat(), this.beatEvery);
    let endReason = "completed";
    let abandoned = false;
    let abandonError: ApiError | null = null;
    let serverEnded = false;
    try {
      const out = await this.loop();
      endReason = out.reason;
      abandoned = out.abandon;
      abandonError = out.error;
      serverEnded = out.ended;
    } catch (e) {
      // 引擎自身的意外异常：当作没说上话处理（不再继续），交给服务端清扫。
      abandoned = true;
      endReason = "plugin_internal";
      console.error("采购助手执行引擎内部错误", e); // 原始的英文报错只进控制台，不进界面与日志
      this.log("err", "执行引擎内部错误，本批次中断");
    } finally {
      this.timers.clearInterval(handle);
    }

    const c = this.view.counts;
    const tally = `成功 ${c.succeeded}，失败 ${c.failed}，拦截 ${c.blocked}，待核实 ${c.unknown}，中止 ${c.aborted}`;

    if (serverEnded) {
      // 护栏 buyer 项拦截：服务端在拦截的同一个事务里已经把这一笔中止、把批次结束了。不调用结束批次（只会得到
      // 409 LEASE_LOST），也没有在途尝试留给清扫。放在「中断」之前判：并发的那一次批次心跳可能已经拿到 409、
      // 记了「执行权已失效」，但批次是服务端按规矩结束的，不是中断。
      const message = `执行批次 #${this.runId} 已结束：${BUYER_CHANGED_TEXT}（${tally}）`;
      this.log("warn", message);
      this.emit({ phase: "ended", attempt: null, stage: null, stage_label: null, message });
      return this.summary("finished", endReason, trigger, null, null, message);
    }

    if (abandoned || this.stopNoFinish) {
      const why = abandonError ? abandonError.message : this.stopReason ?? END_REASON_TEXT[endReason] ?? "执行中断";
      const message =
        endReason === "result_not_delivered"
          ? `执行中断：结果暂时送不到服务端（${why}）。回执已存在插件里，后台会继续续租并补交，送达后结束批次（${tally}）`
          : `执行中断：${why}。已停止续租，服务端会在租约到期（约 ${leaseMinutes(this.leaseTtlS)} 分钟）后处理在途尝试（${tally}）`;
      this.log("err", message);
      this.emit({ phase: "ended", attempt: null, stage: null, stage_label: null, message });
      return this.summary("abandoned", endReason, trigger, abandonError, null, message);
    }

    this.emit({ phase: "finishing", attempt: null, stage: null, stage_label: null, message: "正在结束执行批次" });
    const fin = await this.deps.api.finishRun(this.runId, endReason);
    if (!fin.ok) {
      // 结束批次失败不影响结论：服务端会按租约清扫。
      this.log("warn", `结束执行批次失败：${fin.error.message}（服务端会在租约到期后自动结束）`);
    }
    const message = `执行批次 #${this.runId} 已结束：${tally}`;
    this.log("ok", message);
    this.emit({ phase: "ended", message });
    return this.summary("finished", endReason, trigger, null, null, message);
  }

  /** 批次心跳。定时器与「到点补一次」共用；同一时刻只发一个（后到的等前一个结束）。 */
  private beat(): Promise<void> {
    if (this.runId === null || this.view.phase !== "running") return Promise.resolve();
    if (this.beating) return this.beating;
    this.lastBeatAt = this.now();
    this.beating = this.beatOnce(this.runId).finally(() => {
      this.beating = null;
    });
    return this.beating;
  }

  private async beatOnce(runId: number): Promise<void> {
    const r = await this.deps.api.runHeartbeat(runId);
    if (r.ok) {
      this.lastBeatOkAt = this.now();
      if (r.data.stop) this.requestStop(`服务端要求停止：${stopReasonLabel(r.data.stop_reason)}`);
      return;
    }
    if (r.error.kind !== "http" || (r.error.status !== null && r.error.status >= 500)) {
      // 一次续租没送到不致命（租约有余量），下一次再试；真断了网，下一步请求会让引擎停下。
      this.log("warn", `批次心跳失败：${r.error.message}`);
      return;
    }
    // 服务端明确拒绝（LEASE_LOST / 404 / 403）：执行权已经没了，不再结束批次。
    this.stopNoFinish = true;
    this.requestStop(`执行权已失效：${r.error.message}`);
  }

  /** 到点就补一次续租（每一步之前都调；后台标签页的定时器会被节流，不能只靠定时器）。 */
  private async beatIfDue(): Promise<void> {
    if (this.now() - this.lastBeatAt >= this.beatEvery) await this.beat();
  }

  /** 输入：无 → 输出：无。请宿主重读并上报身份；失败只记一行，不影响流程（服务端按新鲜度裁决）。 */
  private async refreshIdentity(): Promise<void> {
    const fn = this.deps.refreshIdentity;
    this.lastIdentityAt = this.now();
    if (!fn) return;
    try {
      await fn();
    } catch (e) {
      this.log("warn", `身份上报失败：${e instanceof Error && /[\u3400-\u9fff]/.test(e.message) ? e.message : "插件后台没有响应"}`);
    }
  }

  /** 批次进行中到点就补报一次身份（服务端要求身份 10 分钟内上报过）。 */
  private async identityIfDue(): Promise<void> {
    if (this.now() - this.lastIdentityAt >= this.identityEvery) await this.refreshIdentity();
  }

  /** 输入：要等多久 → 输出：无。分段等，每段之后到点就续租、补报身份；收到停止请求就不再等。 */
  private async waitKeepingLease(ms: number): Promise<void> {
    let left = ms;
    while (left > 0 && this.stopReason === null) {
      const slice = Math.min(left, this.beatEvery);
      await this.sleep(slice);
      left -= slice;
      await this.beatIfDue();
      await this.identityIfDue();
    }
  }

  /** 领取循环：一笔一笔跑到领不到、被要求停下、服务端自己结束了批次或中断为止。 */
  private async loop(): Promise<LoopEnd> {
    const done = (reason: string): LoopEnd => ({ reason, abandon: false, error: null, ended: false });
    const abandon = (reason: string, error: ApiError): LoopEnd => ({ reason, abandon: true, error, ended: false });
    let claimWaits = 0;
    for (;;) {
      await this.beatIfDue();
      await this.identityIfDue();
      if (this.stopReason !== null) return done("stopped");
      if (this.view.counts.attempts >= this.maxAttempts) {
        this.log("warn", `本批次已领取 ${this.maxAttempts} 笔，达到单批次上限，停止领取`);
        return done("attempt_cap");
      }

      const n = await this.deps.api.next(this.runId!);
      if (!n.ok) {
        const e = n.error;
        if (e.kind !== "http") {
          // 领取的响应丢了：服务端可能已经建了尝试。不再继续，交给清扫。
          return abandon("network", e);
        }
        if (e.code === "LEASE_LOST") return abandon("lease_lost", e);
        this.log("err", `领取任务失败：${e.message}`);
        return done(e.code.toLowerCase());
      }
      if (n.data.attempt === null) {
        if (n.data.retry_after_s !== null && claimWaits < CLAIM_RETRY_MAX) {
          // 候选任务只是暂时被别的操作（导入、同步、分配）锁着：等一会儿再领，不结束批次，等待期间照常续租。
          claimWaits += 1;
          const wait = claimWaitMs(n.data.retry_after_s);
          this.log("info", `${n.data.reason ?? "可执行的任务暂时被占用"}（${Math.ceil(wait / 1000)} 秒后第 ${claimWaits} 次再领，最多 ${CLAIM_RETRY_MAX} 次）`);
          await this.waitKeepingLease(wait);
          continue;
        }
        if (n.data.retry_after_s !== null) {
          this.log("warn", `可执行的任务一直被其他操作占用（已再领 ${CLAIM_RETRY_MAX} 次），本批次先结束，稍后再执行`);
          return done("completed");
        }
        this.log("info", `没有可执行的任务了${n.data.reason ? `：${n.data.reason}` : ""}`);
        return done("completed");
      }
      claimWaits = 0;

      const step = await this.runAttempt(n.data.attempt);
      this.emit({ attempt: null, stage: null, stage_label: null });
      if (step.kind === "abandon") return abandon(step.reason, step.error);
      if (step.kind === "ended") return { reason: step.reason, abandon: false, error: null, ended: true };
      if (step.kind === "stop") return done(step.reason);
    }
  }

  /** 输入：尝试、写进日志括号里的说明 → 输出：这一笔与本批次的结局。护栏的 buyer 项拦下了这一笔（结算页登录的买家号
   *  不是这个批次的买家号）：服务端在拦截的同一个事务里已经把它记为中止（任务回已分配，不进异常、不暂停账号）并把
   *  批次结束了。所以**不上报结果**（再报只会与服务端记下的结局对不上）、不再领取、也不调用结束批次。 */
  private buyerChanged(a: Attempt, note: string): StepResult {
    this.log("warn", `尝试 #${a.attempt_id}：${BUYER_CHANGED_TEXT}（${note}）`);
    this.emit({ message: `${BUYER_CHANGED_TEXT}。` });
    this.bump("aborted");
    return { kind: "ended", reason: "buyer_changed" };
  }

  private setStage(stage: string): void {
    this.emit({ stage, stage_label: stageLabel(stage) });
  }

  private bump(k: keyof RunCounts): void {
    this.emit({ counts: { ...this.view.counts, [k]: this.view.counts[k] + 1 } });
  }

  /** 上报一次结果。返回 "ok" = 送达；"rejected" = 服务端明确不收（409 等，可能已自行记过）；
   *  StepResult = 执行权已失效，或在租约预算内一直没送到 —— 这一笔与本批次中断，回执留给后台补交。
   *
   *  没说上话、服务端 5xx：按退避重发**同一份** body（服务端认得出重复上报），每次等待之后先续租。 */
  private async report(a: Attempt, body: ResultIn): Promise<"ok" | "rejected" | StepResult> {
    this.setStage("reporting");
    let firstFailAt: number | null = null;
    for (let n = 0; ; n++) {
      await this.beatIfDue();
      const r = await this.deps.api.result(a.attempt_id, body);
      if (r.ok) {
        if (n > 0) this.log("ok", `尝试 #${a.attempt_id} 的结果已补交送达`);
        return "ok";
      }
      const e = r.error;
      if (e.kind === "http" && e.code === "LEASE_LOST") return { kind: "abandon", reason: "lease_lost", error: e };
      if (!isRetryableResultError(e)) {
        if (isLateReceipt(e)) {
          this.log("warn", `尝试 #${a.attempt_id} 已被服务端判为「结果待核实」，这次的结果已作为迟到回执记下：${e.message}`);
        } else {
          this.log("warn", `尝试 #${a.attempt_id} 结果上报被服务端拒绝：${e.message}`);
        }
        return "rejected";
      }
      const now = this.now();
      firstFailAt ??= now;
      const leaseLeft = this.lastBeatOkAt + this.leaseTtlS * 1000 - LEASE_MARGIN_MS - now;
      const budgetLeft = firstFailAt + this.reportRetryMaxMs - now;
      if (leaseLeft <= 0 || budgetLeft <= 0) {
        this.log("err", `尝试 #${a.attempt_id} 的结果一直没有送达（${e.message}）；回执已存在插件里，服务端恢复后自动补交`);
        return { kind: "abandon", reason: "result_not_delivered", error: e };
      }
      const wait = Math.min(REPORT_BACKOFF_MS[Math.min(n, REPORT_BACKOFF_MS.length - 1)]!, leaseLeft, budgetLeft);
      if (n === 0) this.log("warn", `尝试 #${a.attempt_id} 结果上报没有送达（${e.message}），${Math.round(wait / 1000)} 秒后重发；回执已存在插件里`);
      await this.sleep(wait);
    }
  }

  /** 进度上报。返回 null = 继续；否则是这一笔的结局。 */
  private async progress(a: Attempt, stage: string, detail: Record<string, unknown>): Promise<StepResult | null> {
    await this.beatIfDue();
    await this.identityIfDue();
    this.setStage(stage);
    const p = await this.deps.api.progress(a.attempt_id, stage, { simulated: true, ...detail });
    if (!p.ok) {
      if (p.error.kind !== "http" || p.error.code === "LEASE_LOST") {
        return { kind: "abandon", reason: "progress_not_delivered", error: p.error };
      }
      this.log("err", `尝试 #${a.attempt_id} 进度上报被拒绝：${p.error.message}`);
      return { kind: "stop", reason: "progress_rejected" };
    }
    if (!p.data.continue) {
      const why = p.data.cancel_reason ?? cancelLabel(p.data.cancel_code);
      const code = abortCodeFor(p.data.cancel_code);
      if (p.data.cancel_code === "IDENTITY_MISMATCH") {
        // 执行中换了买家号：面板与日志上用同一句（规格 §5），服务端的原话附在后面。
        this.log("warn", `尝试 #${a.attempt_id}（${a.upstream_order_no ?? "—"}）：${BUYER_CHANGED_TEXT}${p.data.cancel_reason ? `（服务端：${p.data.cancel_reason}）` : ""}`);
        this.emit({ message: `${BUYER_CHANGED_TEXT}。` });
      } else {
        this.log("warn", `尝试 #${a.attempt_id}（${a.upstream_order_no ?? "—"}）：${why}${/中止$/.test(why) ? "" : "，未提交前中止"}`);
      }
      if (code === null) {
        // 服务端已经结束了这一笔（例如清除模拟结果、清扫）：不用再上报，继续下一笔。
        this.bump("aborted");
        return { kind: "continue" };
      }
      const rep = await this.report(a, failureResult("failed", code, why));
      if (typeof rep === "object") return rep;
      this.bump("aborted");
      return { kind: "continue" };
    }
    return null;
  }

  private async runAttempt(a: Attempt): Promise<StepResult> {
    this.bump("attempts");
    this.emit({
      attempt: { attempt_id: a.attempt_id, upstream_order_no: a.upstream_order_no, asin: a.asin, quantity: a.quantity },
      stage: "claimed",
      stage_label: stageLabel("claimed"),
    });
    this.log("info", `领取尝试 #${a.attempt_id}：${a.upstream_order_no ?? "—"} · ${a.asin ?? "—"} × ${a.quantity ?? "—"}`);

    if (!a.simulated) {
      // 服务端本阶段不会下发非模拟尝试；万一下发了，插件拒绝执行并结束批次。
      this.log("err", `尝试 #${a.attempt_id} 不是模拟尝试，插件拒绝执行`);
      const rep = await this.report(a, failureResult("failed", "PLUGIN_SIMULATION_ONLY", "插件当前只做模拟执行"));
      if (typeof rep === "object") return rep;
      this.bump("failed");
      return { kind: "stop", reason: "non_simulated_attempt" };
    }

    const outcome = this.deps.outcome;
    const started = await this.progress(a, "started", { outcome });
    if (started) return started;

    // 护栏之前就失败的几种模拟结局。
    const early: Partial<Record<SimOutcome, { code: string; detail: string; account: boolean }>> = {
      out_of_stock: { code: "OUT_OF_STOCK", detail: "模拟：商品缺货", account: false },
      page_load_failed: { code: "PAGE_LOAD_FAILED", detail: "模拟：页面加载失败", account: false },
      account_captcha: { code: "CAPTCHA_ENCOUNTERED", detail: "模拟：遇到验证码", account: true },
    };
    const e = early[outcome];
    if (e) {
      this.log("warn", `尝试 #${a.attempt_id}：${codeLabel(e.code)}（模拟）`);
      const rep = await this.report(a, failureResult("failed", e.code, e.detail));
      if (typeof rep === "object") return rep;
      this.bump("failed");
      if (e.account || ACCOUNT_LEVEL_CODES.has(e.code)) {
        this.log("warn", "账号级错误：服务端会暂停该账号执行，本批次结束");
        return { kind: "stop", reason: "account_level_error" };
      }
      return { kind: "continue" };
    }

    // 事实里的买家号 ID 取这一刻页面上的读数，不沿用开始批次时的那个：中途换了买家号，护栏要看得出来。
    let buyer: string | null = null;
    try {
      buyer = this.deps.readCustomerId?.() ?? null;
    } catch {
      buyer = null; // 读页面出错就是没读到，交给服务端按数据不可靠拦截
    }
    const facts = buildCheckoutFacts(a, outcome, this.now(), buyer);
    const checkout = await this.progress(a, "checkout", { total: facts.total, delivery_date: facts.delivery.date });
    if (checkout) return checkout;

    await this.beatIfDue();
    this.setStage("guard");
    const g = await this.deps.api.guard(a.attempt_id, facts);
    if (!g.ok) {
      if (endedForBuyer(g.error)) {
        // 护栏的响应丢了、请求被重发：服务端第一次已按 buyer 项拦下 —— 这一笔已中止（任务回已分配），批次也已结束。
        return this.buyerChanged(a, `第一次的响应没有收到，服务端：${g.error.message}`);
      }
      if (g.error.kind !== "http" || g.error.code === "LEASE_LOST") {
        return { kind: "abandon", reason: "guard_not_delivered", error: g.error };
      }
      if (g.error.status === 409 && attemptStatusOf(g.error) === "failed") {
        // 护栏的响应丢了、请求被重发：服务端第一次已按一般的拦截码结束了这一笔（尝试失败、任务转异常；buyer 项拦截
        // 不走这里，见上）。算拦截，继续下一笔。
        this.log("warn", `尝试 #${a.attempt_id} 护栏已拦截（第一次的响应没有收到，服务端：${g.error.message}），继续下一笔`);
        this.bump("blocked");
        return { kind: "continue" };
      }
      this.log("err", `尝试 #${a.attempt_id} 护栏请求被拒绝：${g.error.message}`);
      return { kind: "stop", reason: "guard_rejected" };
    }
    if (g.data.verdict !== "pass") {
      const code = g.data.code ?? "GUARD_BLOCKED";
      const why = g.data.reasons.length ? g.data.reasons.join("；") : codeLabel(code);
      if (code === BUYER_BLOCK_CODE) {
        // 护栏的 buyer 项：结算页登录的买家号不是这个批次的买家号。服务端按中止类落地（尝试中止、任务回已分配，
        // 不进异常、不暂停账号），并且已经把批次结束：不上报结果、不再领取、不调用结束批次。
        return this.buyerChanged(a, `${why}；服务端已结束本批次`);
      }
      // 一般的拦截码：服务端已把这一笔记为失败、任务转异常；照常上报同一份失败（服务端认出重复），继续下一笔。
      this.log("warn", `尝试 #${a.attempt_id} 护栏拦截：${codeLabel(code)}（${why}）`);
      const rep = await this.report(a, failureResult("failed", code, why));
      if (typeof rep === "object") return rep;
      this.bump("blocked");
      return { kind: "continue" };
    }
    this.log("ok", `尝试 #${a.attempt_id} 护栏放行：总额 $${facts.total ?? "—"}，送达 ${facts.delivery.date ?? "—"}`);

    // 提交登记之前先报一次新鲜的身份（服务端在登记前的确认与登记时都要求登录状态 10 分钟内上报过），
    // 再做登记前最后一次确认：上游在这之前取消、买家号变了或登录状态过期、来源有变化，这里拿到 continue=false。
    await this.refreshIdentity();
    const before = await this.progress(a, "before_submit", {});
    if (before) return before;

    await this.beatIfDue();
    this.setStage("submitting");
    const s = await this.deps.api.submitting(a.attempt_id);
    if (!s.ok) {
      if (s.error.kind !== "http") {
        // 登记的响应没拿到：服务端可能已经记了 submitted_at。绝不重试、不上报成功，交给清扫。
        this.log("err", `尝试 #${a.attempt_id} 提交登记没有得到确认（${s.error.message}），不再继续`);
        return { kind: "abandon", reason: "submitting_unconfirmed", error: s.error };
      }
      if (endedForBuyer(s.error)) {
        // 登记的响应丢了、浏览器把请求重发：服务端第一次复核时已按 buyer 项拦下 —— 没有登记提交，这一笔已中止，批次也已结束。
        return this.buyerChanged(a, `第一次的响应没有收到，服务端：${s.error.message}`);
      }
      if (s.error.code === "LEASE_LOST") return { kind: "abandon", reason: "lease_lost", error: s.error };
      const st = attemptStatusOf(s.error);
      if (s.error.status === 409 && (st === "submitting" || st === "verifying")) {
        // 第一次登记其实到了服务端、响应丢了，浏览器把请求重发，服务端回「已登记」。
        // 这就是「已登记、但插件没拿到 200、没有点击下单」：只能是结果待核实，绝不是「被拒」。
        this.log("err", `尝试 #${a.attempt_id} 提交登记的响应没有收到，服务端显示已登记（${s.error.message}）；没有点击下单，上报「结果不明」`);
        const rep = await this.report(a, failureResult("unknown", null, "提交登记的响应没有收到（服务端显示已登记），插件没有点击下单，请人工核实"));
        if (typeof rep === "object") return rep;
        this.bump("unknown");
        this.log("warn", "结果待核实：服务端会暂停该账号执行，本批次结束");
        return { kind: "stop", reason: "result_unknown" };
      }
      if (buyerBlockedAtSubmit(s.error)) {
        // 提交前复核时护栏的 buyer 项拦下了（存下的结算页买家号 ID 已不是批次账号当前登记的 ID）：没有登记提交，
        // 服务端已把这一笔中止（任务回已分配）并结束了批次。和护栏判定处一样：不上报结果、不调用结束批次。
        const d = isObj(s.error.detail) ? s.error.detail : {};
        const reasons = Array.isArray(d.reasons) ? d.reasons.filter((r): r is string => typeof r === "string" && r !== "") : [];
        return this.buyerChanged(a, `${reasons.length ? reasons.join("；") : s.error.message}；服务端已结束本批次`);
      }
      if (st !== null && ["succeeded", "failed", "unknown", "aborted"].includes(st)) {
        // 尝试已经被服务端结束了（清扫、清除模拟结果……）：再报只会 409。本批次不再继续。
        this.log("warn", `尝试 #${a.attempt_id} 提交登记被拒绝：${s.error.message}（服务端已结束这一笔）`);
        this.bump("aborted");
        return { kind: "stop", reason: "submit_rejected" };
      }
      if (s.error.code === "GUARD_BLOCKED") {
        // 按最新配置复核护栏不再放行（一般的拦截码；服务端什么都没改）：按拦截码上报失败，这一笔转异常，其他任务继续。
        // buyer 项的复核拦截由服务端自己落地并结束批次，在上面已经处理。
        const d = isObj(s.error.detail) ? s.error.detail : {};
        const block = typeof d.block_code === "string" && d.block_code ? d.block_code : "GUARD_BLOCKED";
        this.log("warn", `尝试 #${a.attempt_id} 提交前复核护栏未通过：${codeLabel(block)}（${s.error.message}）`);
        const rep = await this.report(a, failureResult("failed", block, s.error.message));
        if (typeof rep === "object") return rep;
        this.bump("blocked");
        return { kind: "continue" };
      }
      // 其他 409（任务已取消、账号暂停、买家号变了、来源有变化……）：先问一次服务端为什么。
      this.log("warn", `尝试 #${a.attempt_id} 提交登记被拒绝：${s.error.message}`);
      const again = await this.progress(a, "before_submit", { submit_rejected: s.error.code });
      if (again) return again;
      const rep = await this.report(a, failureResult("failed", "SUBMIT_REJECTED", s.error.message || codeLabel(s.error.code)));
      if (typeof rep === "object") return rep;
      this.bump("failed");
      return { kind: "stop", reason: "submit_rejected" };
    }

    // 已登记提交。从这里开始，任何不顺利都只能是「结果待核实」，不会再提交一次。
    if (outcome === "unknown_after_submit") {
      this.log("warn", `尝试 #${a.attempt_id}：模拟提交后无法确认结果，上报「结果不明」`);
      const rep = await this.report(a, failureResult("unknown", null, "模拟：提交后无法确认结果"));
      if (typeof rep === "object") return rep;
      this.bump("unknown");
      this.log("warn", "结果待核实：服务端会暂停该账号执行，本批次结束");
      return { kind: "stop", reason: "result_unknown" };
    }

    const orderNo = simOrderNo(a.attempt_id, this.now());
    const rep = await this.report(a, successResult(facts, orderNo));
    if (typeof rep === "object") return rep;
    if (rep === "rejected") {
      // 已登记提交、成功结果却被拒：这一笔交给服务端按「结果待核实」处理，本批次不再继续。
      this.log("err", `尝试 #${a.attempt_id} 的成功结果被服务端拒绝，本批次停止，请在控制台核实`);
      return { kind: "stop", reason: "success_rejected" };
    }
    this.bump("succeeded");
    this.setStage("done");
    this.log("ok", `尝试 #${a.attempt_id} 模拟成功：${orderNo}`);
    return { kind: "continue" };
  }
}

/** 领取循环怎么收场的。 */
interface LoopEnd {
  /** 结束原因（插件这一侧的细分原因：completed、stopped、buyer_changed……）。 */
  reason: string;
  /** 中断：不调用结束批次，交给服务端清扫。 */
  abandon: boolean;
  /** 中断时的那个错误。 */
  error: ApiError | null;
  /** 服务端已经自己把批次结束了（护栏 buyer 项拦截）：不调用结束批次，也不算中断。 */
  ended: boolean;
}

/** 一笔尝试跑完之后，批次接下来怎么走。 */
type StepResult =
  /** 这一笔结束了，领下一笔。 */
  | { kind: "continue" }
  /** 本批次到此为止：调用结束批次。 */
  | { kind: "stop"; reason: string }
  /** 服务端已经自己把批次结束了（护栏 buyer 项拦截）：不再领取，也不调用结束批次。 */
  | { kind: "ended"; reason: string }
  /** 中断：不再续租、不再调用，交给服务端清扫。 */
  | { kind: "abandon"; reason: string; error: ApiError };
