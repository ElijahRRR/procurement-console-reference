/** 自动登记（规格 §2.2、§3）：设备令牌的生成、登记失败后的退避、要不要再试（纯函数，不碰 chrome API）。
 *
 * 没有配对码，也没有人工步骤：
 *   · 设备令牌由插件自己生成（32 字节随机数的 base64url，43 个字符），存在扩展源下的 IndexedDB；
 *     它只是会话标识（把批次、尝试、回执归到同一个浏览器），不代表身份。旧版配对得到的令牌原样沿用。
 *   · 没有登记成功过、或任何接口回 401 → 带着**同一个**令牌调 `POST /plugin/v1/register`（服务端按令牌哈希幂等：
 *     响应丢了再发、服务端换了库之后再发，都不会多出一行）。
 *   · 登记失败按 5 / 15 / 60 秒退避；429 按服务端给的 retry_after_s（没有则 60 秒）。
 *     WORKSPACE_REQUIRED、PLUGIN_OUTDATED、403、密钥库写不进去这几种**不自动重试**：
 *     等所有者处理（填工作区 ID、更新插件、改地址）后点「重试」或「重新登记」。
 *   · 同一时刻只有一个登记请求在飞（core/gates.SharedFlight），后到的等同一个结果。
 */

import type { ApiError } from "./client.js";
import { retryAfterSeconds } from "./client.js";

/** 服务端接受的令牌形状（规格 §2.2）。 */
export const DEVICE_TOKEN_RE = /^[A-Za-z0-9_-]{32,128}$/;

/** 登记失败后的退避（秒）：第 1 / 2 / 3 次及以后。 */
export const REGISTER_BACKOFF_S: readonly number[] = [5, 15, 60];
/** 429 没给 retry_after_s 时等多久（秒）。 */
export const RATE_LIMIT_DEFAULT_S = 60;
/** 手动「重试」之间的最小间隔：面板上的按钮谁都点得到（内容脚本也发得出这条消息），不能连发成一片。 */
export const MANUAL_RETRY_MIN_GAP_MS = 2_000;
/** 401 触发的重新登记成功之后，这么久内再收到 401 不再重新登记（防止登记与 401 打转）。 */
export const REREGISTER_MIN_GAP_MS = 30_000;

type RandomFill = (buf: Uint8Array) => Uint8Array;

/** 输入：字节数组 → 输出：base64url 文本（不带 `=`）。 */
export function base64url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 输入：随机源（默认 crypto.getRandomValues）→ 输出：新的设备令牌（32 字节，base64url 43 个字符）。 */
export function generateDeviceToken(fill: RandomFill = (b) => crypto.getRandomValues(b)): string {
  return base64url(fill(new Uint8Array(32)));
}

/** 输入：任意值 → 输出：是不是服务端接受的令牌形状。 */
export function isValidDeviceToken(v: unknown): v is string {
  return typeof v === "string" && DEVICE_TOKEN_RE.test(v);
}

/** 最近一次登记的结论（存 chrome.storage.local，给界面与下一次唤醒的后台看）。 */
export interface RegisterStatus {
  ok: boolean;
  /** 最近一次尝试的时刻。 */
  at: number;
  /** 连续失败了几次（成功后归零）。 */
  fails: number;
  /** 下一次自动重试不早于这个时刻；null = 不会自动重试（已成功，或要人处理）。 */
  next_at: number | null;
  /** 不自动重试，等所有者处理。 */
  halted: boolean;
  code: string | null;
  message: string | null;
}

/** 这几种登记失败不自动重试（规格 §3）。 */
export const HALT_CODES: ReadonlySet<string> = new Set(["WORKSPACE_REQUIRED", "PLUGIN_OUTDATED", "TOKEN_STORE"]);

/** 输入：登记失败 → 输出：要不要停下等人处理。403（任何错误码）与上面几种都停；
 *  422（令牌形状不被接受）再发多少遍答案也一样，同样停。 */
export function registerHalts(e: ApiError): boolean {
  if (HALT_CODES.has(e.code)) return true;
  return e.kind === "http" && (e.status === 403 || e.status === 422);
}

/** 输入：登记失败、这是连续第几次失败（从 1 起）→ 输出：多少秒后自动重试；null = 不自动重试。 */
export function registerRetryDelayS(e: ApiError, fails: number): number | null {
  if (registerHalts(e)) return null;
  if (e.kind === "http" && (e.status === 429 || e.code === "RATE_LIMITED")) {
    const n = retryAfterSeconds(e);
    return n !== null && n > 0 ? n : RATE_LIMIT_DEFAULT_S;
  }
  return REGISTER_BACKOFF_S[Math.min(Math.max(fails, 1), REGISTER_BACKOFF_S.length) - 1]!;
}

/** 输入：上一次的结论、这次的失败、当前时刻 → 输出：新的结论。 */
export function registerFailed(prev: RegisterStatus | null | undefined, e: ApiError, now: number): RegisterStatus {
  const fails = (prev && !prev.ok ? prev.fails : 0) + 1;
  const delay = registerRetryDelayS(e, fails);
  return { ok: false, at: now, fails, next_at: delay === null ? null : now + delay * 1000, halted: delay === null, code: e.code, message: e.message };
}

/** 输入：当前时刻 → 输出：登记成功的结论。 */
export function registerSucceeded(now: number): RegisterStatus {
  return { ok: true, at: now, fails: 0, next_at: null, halted: false, code: null, message: null };
}

/** 谁在要求登记：
 *    auto    心跳闹钟、面板展开、页面回到前台、协议请求之前 —— 受退避约束，停下等人处理时不发；
 *    manual  所有者点「重试」 —— 不等 5 / 15 / 60 秒的退避，也重试停下的那几种；但 429 的等待照旧遵守，
 *            两次手动重试之间至少隔 MANUAL_RETRY_MIN_GAP_MS；
 *    force   设置页「重新登记」、保存了新的地址或工作区 ID、带令牌的请求回了 401 —— 立即发。 */
export type RegisterMode = "auto" | "manual" | "force";

/** 输入：上一次的结论、当前时刻、谁在要求 → 输出：现在发不发登记请求。 */
export function registerAllowed(prev: RegisterStatus | null | undefined, now: number, mode: RegisterMode): boolean {
  if (mode === "force") return true;
  if (!prev || prev.ok) return true;
  const rateLimited = prev.code === "RATE_LIMITED";
  if (mode === "manual") {
    if (rateLimited && prev.next_at !== null && now < prev.next_at) return false;
    return Math.abs(now - prev.at) >= MANUAL_RETRY_MIN_GAP_MS;
  }
  if (prev.halted || prev.next_at === null) return false;
  return now >= prev.next_at;
}
