/** 结果回执（纯函数，不碰 chrome API）。
 *
 * 落地方案 v1 §4：「插件与服务端均保存必要的结果回执，服务恢复后补交」。插件这一侧：
 *   · 后台在把一笔结果发给服务端**之前**先存一份回执（chrome.storage.local，后台重启、浏览器重启都在）；
 *   · 送达（200，含服务端认出是重复上报的 duplicate）或服务端明确不收（4xx）之后删掉；
 *   · 没说上话（断网、超时、后台被回收）或服务端 5xx：留着，后台按退避节奏补交，最多保留 24 小时。
 * 补交发的是**同一份**请求体：单号、错误码都不变，服务端按「重复上报」认出来，不会登记两次。
 * 服务端已经把这一笔判成「结果待核实」后才送到的回执，服务端照样记下（409，detail.late_receipt_recorded），
 * 这也算送达：交给人工核实时参考。
 */

import type { ApiError, ApiResult } from "./client.js";
import type { ResultIn } from "./types.js";

export interface Receipt {
  attempt_id: number;
  /** 所属执行批次（后台据此判断「这个批次还有回执没送到，先别结束它」）；不知道为 null。 */
  run_id: number | null;
  body: ResultIn;
  created_at: number;
  /** 后台补交过几次（发起上报的那一次不算）。 */
  tries: number;
  /** 下一次补交不早于这个时刻。 */
  next_at: number;
  last_error: string | null;
}

/** 存储形状：attempt_id（字符串）→ 回执。 */
export type ReceiptStore = Record<string, Receipt>;

/** 回执最多保留多久：过了就不再补交（服务端早已按清扫处理，人工核实时以控制台为准）。 */
export const RECEIPT_MAX_AGE_MS = 24 * 3600_000;
/** 新存的回执多久之后才由后台补交：这段时间里发起上报的那一路（引擎）自己在送、自己在重试。 */
export const RECEIPT_GRACE_MS = 30_000;
/** 后台补交的退避节奏（第 n 次失败后等多久）。闹钟最细 30 秒一次，再细没有意义。 */
export const RECEIPT_BACKOFF_MS: readonly number[] = [30_000, 60_000, 120_000, 300_000];

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** 输入：结果上报的失败 → 输出：是不是「服务端已把这一笔判成结果待核实，迟到的回执已记下」。 */
export function isLateReceipt(e: ApiError): boolean {
  return e.kind === "http" && e.status === 409 && isObj(e.detail) && e.detail.late_receipt_recorded === true;
}

/** 输入：结果上报的失败 → 输出：值不值得原样再发一次（没说上话、服务端 5xx）。 */
export function isRetryableResultError(e: ApiError): boolean {
  if (e.kind !== "http") return true; // network / bad_response：不知道服务端收没收到，同一份再发一次无害
  return e.status !== null && e.status >= 500;
}

export type ReplyVerdict = "delivered" | "retry" | "drop";

/** 输入：一次结果上报的结论 → 输出：回执该删（delivered / drop）还是留着补交（retry）。 */
export function classifyResultReply(r: ApiResult<unknown>): ReplyVerdict {
  if (r.ok) return "delivered";
  if (isLateReceipt(r.error)) return "delivered";
  if (isRetryableResultError(r.error)) return "retry";
  return "drop"; // 服务端明确不收（尝试不存在、结果与已登记的不一致、令牌已撤销……），再发答案也一样
}

const sameBody = (a: ResultIn, b: ResultIn): boolean => JSON.stringify(a) === JSON.stringify(b);

/** 输入：回执仓库、尝试、批次、上报体、当前时刻 → 输出：新仓库。同一笔、同一份上报体已在仓库里时原样保留
 *  （不重置补交次数）；上报体不同时以新的为准。 */
export function putReceipt(store: ReceiptStore | null | undefined, attemptId: number, runId: number | null, body: ResultIn, now: number): ReceiptStore {
  const cur = { ...(store ?? {}) };
  const key = String(attemptId);
  const prev = cur[key];
  if (prev && sameBody(prev.body, body)) {
    if (prev.run_id === null && runId !== null) cur[key] = { ...prev, run_id: runId };
    return cur;
  }
  cur[key] = { attempt_id: attemptId, run_id: runId, body, created_at: now, tries: 0, next_at: now + RECEIPT_GRACE_MS, last_error: null };
  return cur;
}

/** 输入：回执仓库、尝试、这次的结论、失败说明、当前时刻、是不是后台补交 → 输出：新仓库。
 *  delivered / drop → 删掉；retry → 记下失败、排下一次补交。 */
export function settleReceipt(
  store: ReceiptStore | null | undefined,
  attemptId: number,
  verdict: ReplyVerdict,
  error: string | null,
  now: number,
  byDrain: boolean,
): ReceiptStore {
  const cur = { ...(store ?? {}) };
  const key = String(attemptId);
  const prev = cur[key];
  if (!prev) return cur;
  if (verdict !== "retry") {
    delete cur[key];
    return cur;
  }
  const tries = prev.tries + (byDrain ? 1 : 0);
  const wait = byDrain ? RECEIPT_BACKOFF_MS[Math.min(tries - 1, RECEIPT_BACKOFF_MS.length - 1)]! : RECEIPT_GRACE_MS;
  cur[key] = { ...prev, tries, next_at: now + wait, last_error: error };
  return cur;
}

/** 输入：回执仓库、当前时刻 → 输出：到了补交时间的回执（先存的在前）。过期的不在里面。 */
export function dueReceipts(store: ReceiptStore | null | undefined, now: number): Receipt[] {
  return Object.values(store ?? {})
    .filter((r) => r.next_at <= now && now - r.created_at <= RECEIPT_MAX_AGE_MS)
    .sort((a, b) => a.created_at - b.created_at);
}

/** 输入：回执仓库、当前时刻 → 输出：{留下的仓库, 过期丢弃的回执}。 */
export function dropExpired(store: ReceiptStore | null | undefined, now: number): { store: ReceiptStore; expired: Receipt[] } {
  const keep: ReceiptStore = {};
  const expired: Receipt[] = [];
  for (const [k, r] of Object.entries(store ?? {})) {
    if (now - r.created_at > RECEIPT_MAX_AGE_MS) expired.push(r);
    else keep[k] = r;
  }
  return { store: keep, expired };
}

/** 输入：回执仓库、批次 → 输出：这个批次还有几条回执没送到。 */
export function pendingForRun(store: ReceiptStore | null | undefined, runId: number): number {
  return Object.values(store ?? {}).filter((r) => r.run_id === runId).length;
}

/** 给界面看的回执（不含金额等细节）。 */
export interface ReceiptView {
  attempt_id: number;
  outcome: ResultIn["outcome"];
  amazon_order_no: string | null;
  error_code: string | null;
  created_at: number;
  tries: number;
  last_error: string | null;
}

/** 输入：回执仓库 → 输出：界面上的待补交列表（先存的在前）。 */
export function receiptViews(store: ReceiptStore | null | undefined): ReceiptView[] {
  return Object.values(store ?? {})
    .sort((a, b) => a.created_at - b.created_at)
    .map((r) => ({
      attempt_id: r.attempt_id,
      outcome: r.body.outcome,
      amazon_order_no: r.body.amazon_order_no ?? null,
      error_code: r.body.error_code ?? null,
      created_at: r.created_at,
      tries: r.tries,
      last_error: r.last_error,
    }));
}

const OUTCOME_TEXT: Record<ResultIn["outcome"], string> = { succeeded: "成功", failed: "失败", unknown: "结果不明" };

/** 输入：界面上的一条回执 → 输出：一行中文说明。 */
export function receiptText(r: ReceiptView): string {
  const what = r.outcome === "succeeded" && r.amazon_order_no ? `成功 ${r.amazon_order_no}` : OUTCOME_TEXT[r.outcome] + (r.error_code ? `（${r.error_code}）` : "");
  const tail = r.last_error ? `，最近一次：${r.last_error}` : "";
  return `尝试 #${r.attempt_id}：${what}${r.tries ? `，已补交 ${r.tries} 次` : ""}${tail}`;
}
