/** 与服务端说话的**唯一**出口（契约 §5，前缀 `/plugin/v1`）。
 *
 * 照着早期实现与参考插件踩过的坑写：
 *   1. **每个请求都有超时**（AbortSignal.timeout）。服务端挂住不能让执行无限阻塞。
 *   2. **HTTP 200 不等于业务成功。** 每个端点都校验响应形状：护栏只有 `verdict === "pass"`
 *      才算放行，提交登记只认 HTTP 200；形状不对一律按 `bad_response` 失败处理。
 *   3. **没说上话 ≠ 服务端说不行。** `kind` 把三种失败分开：
 *        network       fetch 抛错（断网、拒绝连接、超时）—— 状态未知；
 *        http          服务端明确回了非 2xx（带 `{code, message, detail}`）—— 状态已定；
 *        bad_response  回了，但不是约定的形状 —— 当作没听懂，不当成功。
 *   4. **重试只针对「没说上话」，而且只对可以重复的请求。** 配对码一次性、开始批次会占执行权、
 *      领取会建尝试、**提交登记绝不自动重试**（登记即「可能已下单」）。
 *      服务端明确拒绝（4xx）就是拒绝，再问一遍答案一样，也不重试；
 *      5xx 表示服务端这次事务已回滚，只有跑单必需、可以重复的几个接口（RETRY_ON_5XX）再试。
 */

import type {
  Attempt,
  Browser,
  BusyHolder,
  CheckoutFacts,
  GuardOut,
  HeartbeatOut,
  Identity,
  IdentityStatus,
  NextOut,
  PairOut,
  ProgressOut,
  ResultIn,
  RunHeartbeatOut,
  RunStartOut,
  Trigger,
} from "./types.js";
import { codeLabel } from "./codes.js";

export type ApiErrorKind = "network" | "http" | "bad_response";

export interface ApiError {
  kind: ApiErrorKind;
  /** HTTP 状态码；没说上话时为 null。 */
  status: number | null;
  /** 结构化错误码：服务端的 `code`，或插件本地的 NETWORK / TIMEOUT / BAD_RESPONSE。 */
  code: string;
  /** 给人看的原因（中文）。 */
  message: string;
  detail: unknown;
  /** 响应头 Retry-After 的秒数（429 等）；没有或读不出为 null / 缺省。 */
  retry_after_s?: number | null;
}

export type ApiResult<T> = { ok: true; status: number; data: T } | { ok: false; error: ApiError };

export type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

export interface ClientOptions {
  baseUrl: string;
  token?: string | null;
  extVersion: string;
  fetch?: FetchLike;
  /** 单次请求超时，默认 15 秒。 */
  timeoutMs?: number;
  /** 可重试请求在 network 失败后的等待序列，默认 [1000, 3000]（即最多再试两次）。 */
  retryDelaysMs?: number[];
  sleep?: (ms: number) => Promise<void>;
}

/** 开始批次、领取、提交登记等「跑单那一组」接口。引擎只依赖这一组，
 *  内容脚本里的实现经由后台转发（content/remote-api.ts），测试里直接用 PluginClient。 */
export interface RunApi {
  startRun(trigger: Trigger): Promise<ApiResult<RunStartOut>>;
  runHeartbeat(runId: number): Promise<ApiResult<RunHeartbeatOut>>;
  next(runId: number): Promise<ApiResult<NextOut>>;
  progress(attemptId: number, stage: string, detail: Record<string, unknown>): Promise<ApiResult<ProgressOut>>;
  guard(attemptId: number, facts: CheckoutFacts): Promise<ApiResult<GuardOut>>;
  submitting(attemptId: number): Promise<ApiResult<unknown>>;
  verifying(attemptId: number, note: string): Promise<ApiResult<unknown>>;
  result(attemptId: number, body: ResultIn): Promise<ApiResult<unknown>>;
  /** reason：插件这一侧的细分原因（completed、stopped……）。上报时 `reason` 恒为契约缺省的 `finished`，
   *  细分原因放在 `detail` 里（服务端不认识的结束原因码不会出现在它的批次记录里）。 */
  finishRun(runId: number, reason: string): Promise<ApiResult<unknown>>;
}

/** 每个接口能不能在 network 失败后自动重试。**这张表就是重试策略的全部。** */
export const RETRY_POLICY = {
  pair: false, // 配对码一次性
  heartbeat: true,
  identity: true,
  startRun: false, // 会占执行权；丢了响应再发一次只会拿到 409 ACCOUNT_BUSY
  runHeartbeat: true,
  next: false, // 会建尝试；丢了响应再发一次只会拿到 409 ATTEMPT_IN_FLIGHT
  progress: true,
  guard: true, // 护栏是同一份事实的重新裁决
  submitting: false, // 绝不自动重试：登记即「可能已下单」
  verifying: true,
  result: true, // 结果送不到，尝试会一直在途；同一份结果重复送达，服务端回 200 duplicate
  finishRun: true,
} as const;

export type Endpoint = keyof typeof RETRY_POLICY;

/** 服务端回 5xx 时（这次事务已回滚）也再试的接口：跑单必需、重复无害的那几个。
 *  心跳每分钟都有、身份下一轮再报，不在这里；开始批次、领取、提交登记永远不在这里。 */
export const RETRY_ON_5XX: ReadonlySet<Endpoint> = new Set<Endpoint>(["runHeartbeat", "progress", "verifying", "result", "finishRun"]);

/** 结束批次时上报的 `reason`：契约 §5.12 的缺省值，服务端的批次结束原因表认得它。 */
export const FINISH_REASON = "finished";

const DEFAULT_TIMEOUT_MS = 15_000;
const DEFAULT_RETRY_DELAYS = [1000, 3000];

/** 响应体里没有错误码时按状态码推一个（与服务端 server/app 的框架层映射一致）。 */
const STATUS_CODE: Record<number, string> = {
  400: "VALIDATION",
  401: "UNAUTHORIZED",
  403: "FORBIDDEN",
  404: "NOT_FOUND",
  405: "METHOD_NOT_ALLOWED",
  409: "CONFLICT",
  413: "VALIDATION",
  422: "VALIDATION",
  429: "RATE_LIMITED",
  500: "INTERNAL",
};

type Parser<T> = (body: unknown, status: number) => T | null;

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const strOrNum = (v: unknown): string | null =>
  typeof v === "string" ? v : typeof v === "number" && Number.isFinite(v) ? String(v) : null;
const int = (v: unknown): number | null => {
  if (typeof v === "number" && Number.isInteger(v)) return v;
  if (typeof v === "string" && /^-?\d+$/.test(v.trim())) return Number(v.trim());
  return null;
};
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
/** 有限的数（数字或数字字符串，可带小数）；别的为 null。 */
const num = (v: unknown): number | null => {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && /^\s*\d+(\.\d+)?\s*$/.test(v)) return Number(v);
  return null;
};
/** 文本里有没有汉字：服务端给的原话是中文；框架层的英文短语（Not Found 等）不直接给人看。 */
const hasCjk = (s: string): boolean => /[\u3400-\u9fff]/.test(s);

/** 输入：任意值 → 输出：错误详情里的 `ACCOUNT_BUSY` 持有者；形状不对时各字段为 null。 */
export function parseBusyHolder(detail: unknown): BusyHolder {
  const d = isObj(detail) ? detail : {};
  return {
    holder_device_id: int(d.holder_device_id),
    holder_device_name: str(d.holder_device_name),
    since: str(d.since),
  };
}

const IDENTITY_STATUSES: ReadonlySet<string> = new Set(["match", "mismatch", "signed_out", "unknown"]);

/** 输入：任意值 → 输出：合法的身份状态（认不出按 unknown，不往好的方向猜）。 */
export function parseIdentityStatus(v: unknown): IdentityStatus {
  return IDENTITY_STATUSES.has(String(v)) ? (v as IdentityStatus) : "unknown";
}

function parsePair(body: unknown): PairOut | null {
  if (!isObj(body)) return null;
  const token = str(body.device_token);
  const id = int(body.device_id) ?? str(body.device_id);
  if (!token || id === null) return null;
  return { device_id: id, device_token: token, workspace_name: str(body.workspace_name) };
}

function parseAccount(v: unknown): HeartbeatOut["account"] {
  if (!isObj(v)) return null;
  const id = int(v.id);
  if (id === null) return null;
  const q = isObj(v.quota_today) ? v.quota_today : null;
  return {
    id,
    name: str(v.name) ?? `#${id}`,
    amazon_customer_id: str(v.amazon_customer_id),
    purchase_mode: str(v.purchase_mode),
    schedule_times: Array.isArray(v.schedule_times) ? v.schedule_times.filter((t): t is string => typeof t === "string") : [],
    effective_max_delivery_days: int(v.effective_max_delivery_days),
    purchase_enabled: bool(v.purchase_enabled),
    execution_paused: bool(v.execution_paused),
    pause_reason: str(v.pause_reason),
    min_amount: strOrNum(v.min_amount),
    max_amount: strOrNum(v.max_amount),
    daily_limit: int(v.daily_limit),
    expected_card_last4: str(v.expected_card_last4),
    quota_today: q
      ? { limit: int(q.limit), used: int(q.used), planned: int(q.planned), available: int(q.available) }
      : null,
  };
}

function parseHeartbeat(body: unknown): HeartbeatOut | null {
  if (!isObj(body) || !isObj(body.device)) return null;
  const dev = body.device;
  const devId = int(dev.id) ?? str(dev.id);
  if (devId === null) return null;
  const ar = isObj(body.active_run) ? body.active_run : null;
  const arId = ar ? int(ar.id) : null;
  return {
    device: { id: devId, name: str(dev.name), bound_account_id: int(dev.bound_account_id) },
    account: parseAccount(body.account),
    identity_status: parseIdentityStatus(body.identity_status),
    server_time: str(body.server_time),
    bj_date: str(body.bj_date),
    // 只认真正的 true/false；缺失按 true（本阶段服务端只允许模拟，往安全的一侧取）。
    simulation_only: bool(body.simulation_only) ?? true,
    active_run:
      ar && arId !== null
        ? { id: arId, device_id: int(ar.device_id), device_name: str(ar.device_name), is_mine: ar.is_mine === true }
        : null,
  };
}

/** `POST /runs` 的返回：服务端给 `{run_id, lease_ttl_s, lease_expires_at}`；早期的 `{id}`、`{run: {id}}` 也认。 */
function parseRunStart(body: unknown): RunStartOut | null {
  if (!isObj(body)) return null;
  const run = isObj(body.run) ? body.run : null;
  const id = int(body.run_id) ?? int(body.id) ?? (run ? int(run.id) : null);
  if (id === null) return null;
  const ttl = int(body.lease_ttl_s) ?? (run ? int(run.lease_ttl_s) : null);
  return {
    run_id: id,
    lease_expires_at: str(body.lease_expires_at) ?? (run ? str(run.lease_expires_at) : null),
    // 租约有效期只认正整数；读不出就是读不出（引擎按契约默认值算），不当成 0。
    lease_ttl_s: ttl !== null && ttl > 0 ? ttl : null,
  };
}

function parseRunHeartbeat(body: unknown): RunHeartbeatOut | null {
  if (!isObj(body)) return null;
  const stop = bool(body.stop);
  if (stop === null) return null;
  return { lease_expires_at: str(body.lease_expires_at), stop, stop_reason: str(body.stop_reason) };
}

/** 输入：服务端下发的尝试载荷 → 输出：规范化后的 Attempt；没有尝试 ID 返回 null。 */
export function parseAttempt(v: unknown): Attempt | null {
  if (!isObj(v)) return null;
  const id = int(v.attempt_id) ?? int(v.id);
  if (id === null) return null;
  const range = isObj(v.amount_range) ? v.amount_range : null;
  return {
    attempt_id: id,
    upstream_order_no: str(v.upstream_order_no),
    asin: str(v.asin),
    quantity: int(v.quantity),
    unit_price: strOrNum(v.unit_price),
    price_cap: strOrNum(v.price_cap),
    max_delivery_days: int(v.max_delivery_days),
    amount_range: range ? { min: strOrNum(range.min), max: strOrNum(range.max) } : null,
    expected_card_last4: str(v.expected_card_last4),
    // 只认真正的 true。缺失或别的值都当成「不是模拟」—— 引擎会拒绝执行。
    simulated: v.simulated === true,
    raw: v,
  };
}

function parseNext(body: unknown): NextOut | null {
  if (!isObj(body) || !("attempt" in body)) return null;
  // 只要是个不小于 0 的数（整数、小数、数字字符串）就算「稍后再领」；等多久由引擎夹在 1 – 30 秒之间。
  const retry = num(body.retry_after_s);
  const retry_after_s = retry !== null && retry >= 0 ? retry : null;
  if (body.attempt === null) return { attempt: null, reason: str(body.reason), retry_after_s };
  const a = parseAttempt(body.attempt);
  if (!a) return null;
  return { attempt: a, reason: str(body.reason), retry_after_s: null };
}

function parseProgress(body: unknown): ProgressOut | null {
  if (!isObj(body)) return null;
  const c = bool(body.continue);
  if (c === null) return null;
  return { continue: c, cancel_reason: str(body.cancel_reason), cancel_code: str(body.cancel_code) };
}

function parseGuard(body: unknown): GuardOut | null {
  if (!isObj(body)) return null;
  const v = body.verdict;
  if (v !== "pass" && v !== "block") return null;
  const reasons = Array.isArray(body.reasons) ? body.reasons.map((r) => String(r)) : [];
  return { verdict: v, code: str(body.code), reasons, checks: body.checks ?? null };
}

/** 提交登记：**只认 HTTP 200**（契约 §5.9「200 才允许点击下单」）。
 *  201、202、204 都不算 —— 这一格判错的方向是重复下单。 */
function parseSubmitting(body: unknown, status: number): unknown {
  if (status !== 200) return null;
  return body ?? {};
}

const parseAny: Parser<unknown> = (body) => body ?? {};

function describeFetchError(e: unknown): { code: string; message: string } {
  const name = (e as { name?: unknown })?.name;
  if (name === "TimeoutError" || name === "AbortError") return { code: "TIMEOUT", message: "请求超时" };
  // 浏览器给的原因是英文（Failed to fetch 等），不往界面上放。
  return { code: "NETWORK", message: "无法连接服务端（请确认服务端已启动、地址与端口正确）" };
}

/** 输入：Retry-After 响应头 → 输出：秒数（只认非负整数秒；HTTP 日期格式不认）；没有为 null。 */
export function parseRetryAfter(v: string | null | undefined): number | null {
  if (typeof v !== "string" || !/^\s*\d+\s*$/.test(v)) return null;
  return Number(v.trim());
}

/** 输入：非 2xx 的状态码、响应体、Retry-After 响应头 → 输出：结构化错误。
 *  契约的错误体是 `{code, message, detail}`；FastAPI 原生的 `{detail: {...}}` 与
 *  `{detail: "Not Found"}` 也认，码缺失时按状态码推一个。
 *  message 一律是中文：服务端的原话；没有原话（或只是框架层的英文短语）时用错误码的中文说明。 */
export function httpError(status: number, body: unknown, retryAfter: string | null = null): ApiError {
  let b: Record<string, unknown> = isObj(body) ? body : {};
  if (!str(b.code) && isObj(b.detail) && str(b.detail.code)) b = b.detail;
  const code = str(b.code) ?? STATUS_CODE[status] ?? `HTTP_${status}`;
  const said = str(b.message) ?? (typeof b.detail === "string" ? b.detail : null);
  const message = said && hasCjk(said) ? said : codeLabel(code);
  return { kind: "http", status, code, message, detail: "code" in b ? (b.detail ?? null) : null, retry_after_s: parseRetryAfter(retryAfter) };
}

/** 输入：失败 → 输出：服务端建议多少秒后再试（detail.retry_after_s 优先，其次 Retry-After 响应头）；没有为 null。 */
export function retryAfterSeconds(e: ApiError): number | null {
  const d = isObj(e.detail) ? num(e.detail.retry_after_s) : null;
  if (d !== null && d >= 0) return Math.ceil(d);
  return typeof e.retry_after_s === "number" && Number.isFinite(e.retry_after_s) && e.retry_after_s >= 0 ? e.retry_after_s : null;
}

/** 输入：端点、失败 → 输出：这一次失败值不值得原样再发一次。 */
function retryable(endpoint: Endpoint, e: ApiError): boolean {
  if (e.kind === "network") return true;
  return e.kind === "http" && e.status !== null && e.status >= 500 && RETRY_ON_5XX.has(endpoint);
}

/** 输入：配对失败、填的服务端地址 → 输出：给人看的一句「配对失败：……」。
 *  · 429 RATE_LIMITED（配对按 IP 限速）：说清楚多少秒后再试（detail.retry_after_s 或 Retry-After 响应头）；
 *  · 403 FORBIDDEN（服务端的 Host 允许名单：只接受本机地址与 PC_ALLOWED_HOSTS 里的名字）：
 *    提示改用 http://127.0.0.1:8790 或 localhost，或请管理员把这个主机名加进 PC_ALLOWED_HOSTS；
 *  · 其他：服务端的中文原话。 */
export function pairFailureText(e: ApiError, serverUrl: string): string {
  if (e.kind === "http" && (e.code === "RATE_LIMITED" || e.status === 429)) {
    const n = retryAfterSeconds(e);
    return n !== null && n > 0 ? `配对失败：尝试次数过多，请 ${n} 秒后再试` : "配对失败：尝试次数过多，请稍后再试";
  }
  if (e.kind === "http" && e.status === 403 && e.code === "FORBIDDEN") {
    let host = "";
    let port = "8790";
    try {
      const u = new URL(serverUrl);
      host = u.hostname;
      port = u.port || port;
    } catch {
      // 地址读不出来：提示里不带主机名
    }
    const who = host ? `主机名 ${host} ` : "这个主机名";
    return (
      `配对失败：服务端不接受${who}的请求（服务端只接受本机地址和 PC_ALLOWED_HOSTS 里登记的主机名）。` +
      `请把服务端地址改成 http://127.0.0.1:${port} 或 http://localhost:${port}，` +
      `或者请管理员把 ${host || "这个主机名"} 加进服务端的 PC_ALLOWED_HOSTS 后再试`
    );
  }
  return `配对失败：${e.message || codeLabel(e.code)}`;
}

/** 插件协议客户端。一个实例绑定一个服务端地址与设备令牌。 */
export class PluginClient implements RunApi {
  private readonly base: string;
  private readonly fetchFn: FetchLike;
  private readonly timeoutMs: number;
  private readonly retryDelays: number[];
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(private readonly opts: ClientOptions) {
    this.base = opts.baseUrl.trim().replace(/\/+$/, "");
    this.fetchFn = opts.fetch ?? ((url, init) => fetch(url, init));
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.retryDelays = opts.retryDelaysMs ?? DEFAULT_RETRY_DELAYS;
    this.sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  }

  /** 输入：端点名、路径、请求体、解析器 → 输出：ApiResult。按 RETRY_POLICY 决定是否在 network 失败后重试，
   *  按 RETRY_ON_5XX 决定是否在 5xx 后重试。重试发的是同一份请求体。 */
  async call<T>(endpoint: Endpoint, path: string, body: unknown, parse: Parser<T>): Promise<ApiResult<T>> {
    const retries = RETRY_POLICY[endpoint] ? this.retryDelays : [];
    let res = await this.once(path, body, parse);
    for (const delay of retries) {
      if (res.ok || !retryable(endpoint, res.error)) break;
      await this.sleep(delay);
      res = await this.once(path, body, parse);
    }
    return res;
  }

  private async once<T>(path: string, body: unknown, parse: Parser<T>): Promise<ApiResult<T>> {
    const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json" };
    if (this.opts.token) headers.Authorization = `Bearer ${this.opts.token}`;
    let res: Response;
    try {
      res = await this.fetchFn(this.base + path, {
        method: "POST",
        headers,
        body: JSON.stringify(body ?? {}),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (e) {
      const d = describeFetchError(e);
      return { ok: false, error: { kind: "network", status: null, code: d.code, message: d.message, detail: null } };
    }

    let text = "";
    try {
      text = await res.text();
    } catch (e) {
      const d = describeFetchError(e);
      return { ok: false, error: { kind: "network", status: res.status, code: d.code, message: d.message, detail: null } };
    }
    let json: unknown = undefined;
    let parsed = false;
    if (text.trim() !== "") {
      try {
        json = JSON.parse(text);
        parsed = true;
      } catch {
        parsed = false;
      }
    } else {
      json = null;
      parsed = true;
    }

    if (!res.ok) {
      return { ok: false, error: httpError(res.status, parsed ? json : null, res.headers?.get?.("retry-after") ?? null) };
    }
    if (!parsed) {
      return {
        ok: false,
        error: { kind: "bad_response", status: res.status, code: "BAD_RESPONSE", message: `HTTP ${res.status} 响应不是 JSON`, detail: null },
      };
    }
    const data = parse(json, res.status);
    if (data === null || data === undefined) {
      return {
        ok: false,
        error: {
          kind: "bad_response",
          status: res.status,
          code: "BAD_RESPONSE",
          message: `HTTP ${res.status} 响应不是约定的形状`,
          detail: json ?? null,
        },
      };
    }
    return { ok: true, status: res.status, data };
  }

  // ── 设备 ──────────────────────────────────────────────────────────────

  /** 输入：配对码、设备名、浏览器类型 → 输出：设备 ID 与设备令牌。配对码一次性，不重试。 */
  pair(code: string, deviceName: string, browser: Browser): Promise<ApiResult<PairOut>> {
    return this.call("pair", "/plugin/v1/pair", {
      code,
      device_name: deviceName,
      browser,
      ext_version: this.opts.extVersion,
    }, parsePair);
  }

  /** 输入：最新身份快照（过期或没有就不传）→ 输出：设备、绑定账号的有效配置与身份状态。 */
  heartbeat(identity: Identity | null): Promise<ApiResult<HeartbeatOut>> {
    const body: Record<string, unknown> = { ext_version: this.opts.extVersion };
    if (identity) body.identity = identity;
    return this.call("heartbeat", "/plugin/v1/heartbeat", body, parseHeartbeat);
  }

  /** 输入：身份快照 → 输出：服务端的回执（可能带 identity_status）。
   *  契约 §5 没有单列这个接口；服务端没有它时（404/405/422）调用方改用心跳捎带。 */
  identity(identity: Identity): Promise<ApiResult<{ identity_status: IdentityStatus | null }>> {
    return this.call("identity", "/plugin/v1/identity", { ...identity, ext_version: this.opts.extVersion }, (b) => {
      const o = isObj(b) ? b : {};
      return { identity_status: "identity_status" in o ? parseIdentityStatus(o.identity_status) : null };
    });
  }

  // ── 执行批次 ──────────────────────────────────────────────────────────

  startRun(trigger: Trigger): Promise<ApiResult<RunStartOut>> {
    // 本阶段只发模拟批次。这一位写死在这里，不从配置读 —— 没有任何开关能把它翻成 false。
    return this.call("startRun", "/plugin/v1/runs", { trigger, simulated: true }, parseRunStart);
  }

  runHeartbeat(runId: number): Promise<ApiResult<RunHeartbeatOut>> {
    return this.call("runHeartbeat", `/plugin/v1/runs/${runId}/heartbeat`, {}, parseRunHeartbeat);
  }

  next(runId: number): Promise<ApiResult<NextOut>> {
    return this.call("next", `/plugin/v1/runs/${runId}/next`, {}, parseNext);
  }

  progress(attemptId: number, stage: string, detail: Record<string, unknown>): Promise<ApiResult<ProgressOut>> {
    return this.call("progress", `/plugin/v1/attempts/${attemptId}/progress`, { stage, detail }, parseProgress);
  }

  guard(attemptId: number, facts: CheckoutFacts): Promise<ApiResult<GuardOut>> {
    return this.call("guard", `/plugin/v1/attempts/${attemptId}/guard`, facts, parseGuard);
  }

  submitting(attemptId: number): Promise<ApiResult<unknown>> {
    return this.call("submitting", `/plugin/v1/attempts/${attemptId}/submitting`, {}, parseSubmitting);
  }

  verifying(attemptId: number, note: string): Promise<ApiResult<unknown>> {
    return this.call("verifying", `/plugin/v1/attempts/${attemptId}/verifying`, { note }, parseAny);
  }

  result(attemptId: number, body: ResultIn): Promise<ApiResult<unknown>> {
    return this.call("result", `/plugin/v1/attempts/${attemptId}/result`, body, parseAny);
  }

  finishRun(runId: number, reason: string): Promise<ApiResult<unknown>> {
    return this.call("finishRun", `/plugin/v1/runs/${runId}/finish`, { reason: FINISH_REASON, detail: reason }, parseAny);
  }
}
