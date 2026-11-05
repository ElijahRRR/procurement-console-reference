/** 执行引擎：按契约 §5 一步一步跑一个执行批次（本阶段只做模拟）。
 *
 * 不碰任何 chrome API，也不碰页面：依赖全部从构造参数注入（RunApi、时钟、日志、定时器），
 * 所以能在 Node 里对着内存假服务端整条跑通（test/engine.test.mjs）。
 * 在浏览器里它跑在亚马逊标签页的内容脚本中，所有请求经由后台转发（content/remote-api.ts）。
 *
 * 流程（契约 §5.4 – §5.12）：
 *   开始批次（simulated: true）→ 循环 [批次心跳 → 领取 → 进度 → （护栏）→ 提交登记 → 结果]
 *   → 领不到（attempt: null）为止 → 结束批次。
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
 *   · **服务端是全局权威。** 409 ACCOUNT_BUSY 显示持有者设备与开始时间；
 *     LEASE_LOST 立即停止；批次心跳 stop=true 在当前这一笔结束后停止并结束批次。
 *     续租节奏按服务端给的租约有效期（lease_ttl_s）算：min(35 秒, 有效期 / 3)，每一步之前到点就补一次。
 *   · **身份要新鲜。** 服务端在开始批次、领取、进度与提交登记时都要求设备身份在 identity_fresh_s
 *     （默认 600 秒）内上报过：引擎在批次进行中每 identityEveryMs（默认 3 分钟）请宿主重读并上报一次身份，
 *     并且在提交登记之前（登记前最后一次进度确认之前）无条件再报一次 —— 读不到、报不上不影响流程，
 *     服务端是裁决者：身份过期或不符时它让进度 continue=false（IDENTITY_STALE / IDENTITY_MISMATCH）。
 *   · **服务端要求中止时按原因码上报**（cancel_code，契约 §5 第 7 步）：只有 UPSTREAM_CANCELLED 报
 *     ABORTED_UPSTREAM_CANCELLED；ATTEMPT_FINISHED 不再上报；其余报 ABORTED_BY_SERVER（中止类，任务回到已分配）。
 *   · 本阶段尝试是「原子」的：模拟的一笔在毫秒级内完成，批次心跳的 stop 在两笔之间生效。
 *     真实驱动接入时，需要在每一步之间检查 stop（见 README「后续」）。
 *   · 所有等待都有界：每个请求有超时，结果重发有租约预算与总时长上限，单批次最多领取 maxAttempts 笔；
 *     领取时服务端说「稍后再领」（retry_after_s）最多等 6 轮、每轮最多 30 秒，等待期间照常续租。
 */

import type { ApiError, RunApi } from "./client.js";
import { parseBusyHolder } from "./client.js";
import { ACCOUNT_LEVEL_CODES, abortCodeFor, cancelLabel, codeLabel, stageLabel, stopReasonLabel } from "./codes.js";
import type { LogLevel } from "./log.js";
import { isLateReceipt, isRetryableResultError } from "./receipts.js";
import {
  buildCheckoutFacts,
  failureResult,
  simOrderNo,
  successResult,
  type SimOutcome,
} from "./simulate.js";
import { bjShort } from "./time.js";
import type { Attempt, BusyHolder, ResultIn, Trigger } from "./types.js";

export type EnginePhase = "idle" | "starting" | "running" | "finishing" | "ended";

/** 批次怎么结束的。 */
export type RunEndStatus =
  /** 正常结束：领不到新尝试，或按要求停下，并已调用结束批次。 */
  | "finished"
  /** 409 ACCOUNT_BUSY：账号正在别处执行，本次没有开始。 */
  | "busy"
  /** 服务端拒绝开始（未绑定、身份不符、账号暂停、只允许模拟……）或没说上话。 */
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
  /** ACCOUNT_BUSY 的持有者就是本设备（上一次的批次租约还没到期）：过一会儿自然会好。 */
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
  /** 本设备 ID：ACCOUNT_BUSY 的持有者就是自己时换一句说明。 */
  ownDeviceId?: number | string | null;
  /** 重读本浏览器的亚马逊登录身份并上报服务端（`POST /plugin/v1/identity`）。提交登记之前必调，
   *  批次进行中每 identityEveryMs 补一次。没有它（或它失败）时引擎照常走，由服务端按身份新鲜度裁决。 */
  refreshIdentity?: () => Promise<unknown>;
  /** 批次进行中多久补报一次身份，默认 3 分钟（服务端要求 10 分钟内上报过）。 */
  identityEveryMs?: number;
}

/** 契约 §5.4 的租约有效期默认值（服务端没给 lease_ttl_s 时用）。 */
export const DEFAULT_LEASE_TTL_S = 180;
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

const zeroCounts = (): RunCounts => ({ attempts: 0, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 0 });

const NO_TIMERS: Timers = {
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
};

/** 输入：开始批次失败的错误、本设备 ID → 输出：给人看的一句话与 busy 详情。 */
export function describeStartError(err: ApiError, ownDeviceId: number | string | null = null): { message: string; busy: BusyHolder | null; mine: boolean } {
  if (err.code === "ACCOUNT_BUSY") {
    const busy = parseBusyHolder(err.detail);
    const since = bjShort(busy.since);
    const mine = ownDeviceId !== null && busy.holder_device_id !== null && String(busy.holder_device_id) === String(ownDeviceId);
    if (mine) {
      return {
        busy,
        mine,
        message:
          "开始执行失败：本设备已有一个未结束的执行批次" +
          (since ? `（北京时间 ${since} 开始）` : "") +
          "，可能是另一个标签页、刚刷新的页面或上次异常退出；租约到期后会自动释放",
      };
    }
    const who = busy.holder_device_name ? `「${busy.holder_device_name}」` : "另一台设备";
    return {
      busy,
      mine,
      message: `开始执行失败：账号正在${who}上执行` + (since ? `（北京时间 ${since} 开始）` : "") + "，本次没有开始",
    };
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
const GUIDED_START_CODES: ReadonlySet<string> = new Set([
  "UNAUTHORIZED",
  "NOT_BOUND",
  "IDENTITY_MISMATCH",
  "ACCOUNT_PAUSED",
  "REAL_PURCHASE_DISABLED",
]);

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
    this.reportRetryMaxMs = deps.reportRetryMaxMs ?? 10 * 60_000;
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

  /** 输入：触发方式、开始那一步的回调 → 输出：批次总结。一个引擎实例只跑一个批次。 */
  async run(trigger: Trigger, onStarted?: (n: StartNotice) => void): Promise<RunSummary> {
    if (this.view.phase !== "idle") {
      throw new Error("一个 RunEngine 只能跑一个批次");
    }
    this.emit({ phase: "starting", trigger, started_at: this.now(), message: "正在申请执行批次" });
    this.log("info", `申请执行批次（${trigger === "scheduled" ? "定时" : "手动"}，模拟）`);

    const start = await this.deps.api.startRun(trigger);
    if (!start.ok) {
      const { message, busy, mine } = describeStartError(start.error, this.deps.ownDeviceId ?? null);
      const status: RunEndStatus = start.error.code === "ACCOUNT_BUSY" ? "busy" : "rejected";
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
    this.log("ok", `执行批次 #${this.runId} 已开始（模拟）`);
    onStarted?.({ started: true, run_id: this.runId, status: null, error: null, message: `执行批次 #${this.runId} 已开始`, busy_mine: false });

    const handle = this.timers.setInterval(() => void this.beat(), this.beatEvery);
    let endReason = "completed";
    let abandoned = false;
    let abandonError: ApiError | null = null;
    try {
      const out = await this.loop();
      endReason = out.reason;
      abandoned = out.abandon;
      abandonError = out.error;
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

  private async loop(): Promise<{ reason: string; abandon: boolean; error: ApiError | null }> {
    let claimWaits = 0;
    for (;;) {
      await this.beatIfDue();
      await this.identityIfDue();
      if (this.stopReason !== null) return { reason: "stopped", abandon: false, error: null };
      if (this.view.counts.attempts >= this.maxAttempts) {
        this.log("warn", `本批次已领取 ${this.maxAttempts} 笔，达到单批次上限，停止领取`);
        return { reason: "attempt_cap", abandon: false, error: null };
      }

      const n = await this.deps.api.next(this.runId!);
      if (!n.ok) {
        const e = n.error;
        if (e.kind !== "http") {
          // 领取的响应丢了：服务端可能已经建了尝试。不再继续，交给清扫。
          return { reason: "network", abandon: true, error: e };
        }
        if (e.code === "LEASE_LOST") return { reason: "lease_lost", abandon: true, error: e };
        this.log("err", `领取任务失败：${e.message}`);
        return { reason: e.code.toLowerCase(), abandon: false, error: null };
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
          return { reason: "completed", abandon: false, error: null };
        }
        this.log("info", `没有可执行的任务了${n.data.reason ? `：${n.data.reason}` : ""}`);
        return { reason: "completed", abandon: false, error: null };
      }
      claimWaits = 0;

      const step = await this.runAttempt(n.data.attempt);
      this.emit({ attempt: null, stage: null, stage_label: null });
      if (step.kind === "abandon") return { reason: step.reason, abandon: true, error: step.error };
      if (step.kind === "stop") return { reason: step.reason, abandon: false, error: null };
    }
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
      this.log("warn", `尝试 #${a.attempt_id}（${a.upstream_order_no ?? "—"}）：${why}${/中止$/.test(why) ? "" : "，未提交前中止"}`);
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

    const facts = buildCheckoutFacts(a, outcome, this.now());
    const checkout = await this.progress(a, "checkout", { total: facts.total, delivery_date: facts.delivery.date });
    if (checkout) return checkout;

    await this.beatIfDue();
    this.setStage("guard");
    const g = await this.deps.api.guard(a.attempt_id, facts);
    if (!g.ok) {
      if (g.error.kind !== "http" || g.error.code === "LEASE_LOST") {
        return { kind: "abandon", reason: "guard_not_delivered", error: g.error };
      }
      if (g.error.status === 409 && attemptStatusOf(g.error) === "failed") {
        // 护栏的响应丢了、请求被重发：服务端第一次已按拦截结束了这一笔（任务转异常）。算拦截，继续下一笔。
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
      this.log("warn", `尝试 #${a.attempt_id} 护栏拦截：${codeLabel(code)}（${why}）`);
      const rep = await this.report(a, failureResult("failed", code, why));
      if (typeof rep === "object") return rep;
      this.bump("blocked");
      return { kind: "continue" };
    }
    this.log("ok", `尝试 #${a.attempt_id} 护栏放行：总额 $${facts.total ?? "—"}，送达 ${facts.delivery.date ?? "—"}`);

    // 提交登记之前先报一次新鲜的身份（服务端在登记前的确认与登记时都要求身份 10 分钟内上报过），
    // 再做登记前最后一次确认：上游在这之前取消、身份不符或过期、来源有变化，这里拿到 continue=false。
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
      if (st !== null && ["succeeded", "failed", "unknown", "aborted"].includes(st)) {
        // 尝试已经被服务端结束了（清扫、清除模拟结果……）：再报只会 409。本批次不再继续。
        this.log("warn", `尝试 #${a.attempt_id} 提交登记被拒绝：${s.error.message}（服务端已结束这一笔）`);
        this.bump("aborted");
        return { kind: "stop", reason: "submit_rejected" };
      }
      if (s.error.code === "GUARD_BLOCKED") {
        // 按最新配置复核护栏不再放行：和护栏拦截一样，这一笔转异常，其他任务继续。
        const d = isObj(s.error.detail) ? s.error.detail : {};
        const block = typeof d.block_code === "string" && d.block_code ? d.block_code : "GUARD_BLOCKED";
        this.log("warn", `尝试 #${a.attempt_id} 提交前复核护栏未通过：${codeLabel(block)}（${s.error.message}）`);
        const rep = await this.report(a, failureResult("failed", block, s.error.message));
        if (typeof rep === "object") return rep;
        this.bump("blocked");
        return { kind: "continue" };
      }
      // 其他 409（任务已取消、账号暂停、身份不符、来源有变化……）：先问一次服务端为什么。
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

type StepResult =
  | { kind: "continue" }
  | { kind: "stop"; reason: string }
  | { kind: "abandon"; reason: string; error: ApiError };
