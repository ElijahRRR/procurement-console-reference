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
 *   · **没说上话就停。** 尝试进行到一半时网络断了，引擎不再继续、也不再续租，
 *     由服务端在租约到期后按契约清扫：未登记提交 → aborted、任务回到已分配；
 *     已登记提交 → unknown、账号暂停。这条路径叫「中断」（abandoned）。
 *   · **服务端是全局权威。** 409 ACCOUNT_BUSY 显示持有者设备与开始时间；
 *     LEASE_LOST 立即停止；批次心跳 stop=true 在当前这一笔结束后停止并结束批次。
 *   · 本阶段尝试是「原子」的：模拟的一笔在毫秒级内完成，批次心跳的 stop 在两笔之间生效。
 *     真实驱动接入时，需要在每一步之间检查 stop（见 README「后续」）。
 *   · 所有等待都有界：每个请求有超时，单批次最多领取 maxAttempts 笔。
 */

import type { ApiError, RunApi } from "./client.js";
import { parseBusyHolder } from "./client.js";
import { ACCOUNT_LEVEL_CODES, STAGE_LABEL, codeLabel, stopReasonLabel } from "./codes.js";
import type { LogLevel } from "./log.js";
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
  /** 批次心跳间隔，默认 35 秒（契约租约 180 秒；后台标签页定时器最慢约 60 秒一次，仍有余量）。 */
  heartbeatEveryMs?: number;
  /** 单批次最多领取多少笔，默认 200。防止服务端异常时无限循环。 */
  maxAttempts?: number;
  /** 本设备 ID：ACCOUNT_BUSY 的持有者就是自己时换一句说明。 */
  ownDeviceId?: number | string | null;
}

const zeroCounts = (): RunCounts => ({ attempts: 0, succeeded: 0, failed: 0, blocked: 0, unknown: 0, aborted: 0 });

const NO_TIMERS: Timers = {
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
};

/** 输入：开始批次失败的错误、本设备 ID → 输出：给人看的一句话与 busy 详情。 */
export function describeStartError(err: ApiError, ownDeviceId: number | string | null = null): { message: string; busy: BusyHolder | null } {
  if (err.code === "ACCOUNT_BUSY") {
    const busy = parseBusyHolder(err.detail);
    const since = bjShort(busy.since);
    const mine = ownDeviceId !== null && busy.holder_device_id !== null && String(busy.holder_device_id) === String(ownDeviceId);
    if (mine) {
      return {
        busy,
        message:
          "开始执行失败：本设备已有一个未结束的执行批次" +
          (since ? `（北京时间 ${since} 开始）` : "") +
          "，可能是另一个标签页或上次异常退出；租约到期（约 3 分钟）后会自动释放",
      };
    }
    const who = busy.holder_device_name ? `「${busy.holder_device_name}」` : "另一台设备";
    return {
      busy,
      message: `开始执行失败：账号正在${who}上执行` + (since ? `（北京时间 ${since} 开始）` : "") + "，本次没有开始",
    };
  }
  if (GUIDED_START_CODES.has(err.code)) {
    // 这几种拒绝要人去做一件具体的事：用插件这边写好了处置的说明，服务端原话（如暂停原因）附在后面。
    const label = codeLabel(err.code);
    const extra = err.message && err.message !== label ? `（服务端：${err.message}）` : "";
    return { busy: null, message: `开始执行失败：${label}${extra}` };
  }
  return { busy: null, message: `开始执行失败：${err.message || codeLabel(err.code)}` };
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
  private readonly beatEvery: number;
  private readonly maxAttempts: number;

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
  private lastBeatAt = 0;
  private beating = false;
  private runId: number | null = null;

  constructor(private readonly deps: EngineDeps) {
    this.now = deps.now ?? (() => Date.now());
    this.log = deps.log ?? (() => undefined);
    this.timers = deps.timers ?? NO_TIMERS;
    this.beatEvery = deps.heartbeatEveryMs ?? 35_000;
    this.maxAttempts = deps.maxAttempts ?? 200;
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
      const { message, busy } = describeStartError(start.error, this.deps.ownDeviceId ?? null);
      const status: RunEndStatus = start.error.code === "ACCOUNT_BUSY" ? "busy" : "rejected";
      this.log(status === "busy" ? "warn" : "err", message);
      this.emit({ phase: "ended", message });
      onStarted?.({ started: false, run_id: null, status, error: start.error, message });
      return this.summary(status, start.error.code, trigger, start.error, busy, message);
    }

    this.runId = start.data.run_id;
    this.lastBeatAt = this.now();
    this.emit({ phase: "running", run_id: this.runId, message: `执行批次 #${this.runId} 已开始` });
    this.log("ok", `执行批次 #${this.runId} 已开始（模拟）`);
    onStarted?.({ started: true, run_id: this.runId, status: null, error: null, message: `执行批次 #${this.runId} 已开始` });

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
      this.log("err", `执行引擎内部错误：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      this.timers.clearInterval(handle);
    }

    const c = this.view.counts;
    const tally = `成功 ${c.succeeded}，失败 ${c.failed}，拦截 ${c.blocked}，待核实 ${c.unknown}，中止 ${c.aborted}`;

    if (abandoned || this.stopNoFinish) {
      const why = abandonError ? abandonError.message : this.stopReason ?? endReason;
      const message =
        `执行中断：${why}。已停止续租，服务端会在租约到期（约 3 分钟）后处理在途尝试（${tally}）`;
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

  /** 批次心跳。定时器与循环里的「到点补一次」共用；同一时刻只发一个。 */
  private async beat(): Promise<void> {
    if (this.runId === null || this.beating || this.view.phase !== "running") return;
    this.beating = true;
    try {
      const r = await this.deps.api.runHeartbeat(this.runId);
      if (r.ok) {
        this.lastBeatAt = this.now();
        if (r.data.stop) this.requestStop(`服务端要求停止：${stopReasonLabel(r.data.stop_reason)}`);
        return;
      }
      if (r.error.kind === "network" || r.error.kind === "bad_response") {
        // 一次续租没送到不致命（租约 180 秒），下一次再试；真断了网，下一步请求会让引擎停下。
        this.log("warn", `批次心跳失败：${r.error.message}`);
        return;
      }
      // 服务端明确拒绝（LEASE_LOST / 404 / 403）：执行权已经没了，不再结束批次。
      this.stopNoFinish = true;
      this.requestStop(`执行权已失效：${r.error.message}`);
    } finally {
      this.beating = false;
    }
  }

  private async loop(): Promise<{ reason: string; abandon: boolean; error: ApiError | null }> {
    for (;;) {
      if (this.now() - this.lastBeatAt >= this.beatEvery) await this.beat();
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
        this.log("info", `没有可执行的任务了${n.data.reason ? `：${n.data.reason}` : ""}`);
        return { reason: "completed", abandon: false, error: null };
      }

      const step = await this.runAttempt(n.data.attempt);
      this.emit({ attempt: null, stage: null, stage_label: null });
      if (step.kind === "abandon") return { reason: step.reason, abandon: true, error: step.error };
      if (step.kind === "stop") return { reason: step.reason, abandon: false, error: null };
    }
  }

  private setStage(stage: string): void {
    this.emit({ stage, stage_label: STAGE_LABEL[stage] ?? stage });
  }

  private bump(k: keyof RunCounts): void {
    this.emit({ counts: { ...this.view.counts, [k]: this.view.counts[k] + 1 } });
  }

  /** 上报一次结果。返回 "ok" = 送达；"rejected" = 服务端明确拒绝（409 等，可能已自行记过）；
   *  StepResult = 没送到（已重试过）或执行权已失效，这一笔与本批次中断。 */
  private async report(a: Attempt, body: ResultIn): Promise<"ok" | "rejected" | StepResult> {
    this.setStage("reporting");
    const r = await this.deps.api.result(a.attempt_id, body);
    if (r.ok) return "ok";
    if (r.error.kind !== "http" || r.error.code === "LEASE_LOST") {
      return { kind: "abandon", reason: "result_not_delivered", error: r.error };
    }
    this.log("warn", `尝试 #${a.attempt_id} 结果上报被服务端拒绝：${r.error.message}`);
    return "rejected";
  }

  /** 进度上报。返回 null = 继续；否则是这一笔的结局。 */
  private async progress(a: Attempt, stage: string, detail: Record<string, unknown>): Promise<StepResult | null> {
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
      const why = p.data.cancel_reason ?? "上游已取消";
      this.log("warn", `尝试 #${a.attempt_id}（${a.upstream_order_no ?? "—"}）：${why}，未提交前中止`);
      const rep = await this.report(a, failureResult("failed", "ABORTED_UPSTREAM_CANCELLED", why));
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
      stage_label: STAGE_LABEL.claimed ?? "claimed",
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

    this.setStage("guard");
    const g = await this.deps.api.guard(a.attempt_id, facts);
    if (!g.ok) {
      if (g.error.kind !== "http" || g.error.code === "LEASE_LOST") {
        return { kind: "abandon", reason: "guard_not_delivered", error: g.error };
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

    // 登记前最后一次确认：上游在这之前取消的话，这里拿到 continue=false。
    const before = await this.progress(a, "before_submit", {});
    if (before) return before;

    this.setStage("submitting");
    const s = await this.deps.api.submitting(a.attempt_id);
    if (!s.ok) {
      if (s.error.kind !== "http") {
        // 登记的响应没拿到：服务端可能已经记了 submitted_at。绝不重试、不上报成功，交给清扫。
        this.log("err", `尝试 #${a.attempt_id} 提交登记没有得到确认（${s.error.message}），不再继续`);
        return { kind: "abandon", reason: "submitting_unconfirmed", error: s.error };
      }
      if (s.error.code === "LEASE_LOST") return { kind: "abandon", reason: "lease_lost", error: s.error };
      // 409：护栏未通过、批次失效或任务已取消。先问一次是不是上游取消。
      this.log("warn", `尝试 #${a.attempt_id} 提交登记被拒绝：${s.error.message}`);
      const again = await this.progress(a, "before_submit", { submit_rejected: s.error.code });
      if (again) return again;
      const rep = await this.report(a, failureResult("failed", "SUBMIT_REJECTED", `${s.error.code}：${s.error.message}`));
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
