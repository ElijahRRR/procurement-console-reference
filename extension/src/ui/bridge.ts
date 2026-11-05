/** 内容脚本、设置页、弹出页 → 后台的消息通道（有界等待）。 */

import { withTimeout } from "../core/gates.js";
import type { Msg, StateSnapshot } from "../core/messages.js";

/** 输入：消息、超时 → 输出：后台的答复；后台没有响应时抛错（调用方按「没说上话」处理）。 */
export async function toSw<T>(msg: Msg, timeoutMs = 30_000): Promise<T> {
  return withTimeout(chrome.runtime.sendMessage(msg) as Promise<T>, timeoutMs, "插件后台没有响应");
}

/** 输入：无 → 输出：后台的完整状态快照；拿不到返回 null。 */
export async function fetchState(): Promise<StateSnapshot | null> {
  try {
    const s = await toSw<StateSnapshot>({ type: "pc.getState" });
    return s && typeof s === "object" && "device" in s ? s : null;
  } catch {
    return null;
  }
}

/** 输入：日志级别与文本 → 输出：无。交给后台写入日志（失败不抛）。 */
export function swLog(level: "info" | "ok" | "warn" | "err", text: string): void {
  void toSw({ type: "pc.log", level, text }).catch(() => undefined);
}

/** 输入：触发原因、间隔 → 输出：无。请后台在上一次心跳早于这个间隔时马上同步一次（失败不抛）。
 *  结果经 chrome.storage 变化回到界面（onStateChange）。 */
export function syncNow(reason: string, minAgeMs = 5_000): void {
  void toSw({ type: "pc.sync", reason, minAgeMs }, 60_000).catch(() => undefined);
}

/** 已配对、还没绑定账号时，看得见的页面多久同步一次。 */
export const UNBOUND_SYNC_MS = 10_000;

/** 输入：取当前状态的函数 → 输出：无。界面打开、回到前台时同步一次；已配对还没绑定账号、页面又看得见时，
 *  每 10 秒同步一次（控制台里一绑定，这里马上显示账号、「开始执行」可以点）。 */
export function keepFresh(reason: string, getState: () => StateSnapshot | null): void {
  syncNow(reason);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") syncNow(`回到${reason.replace(/^打开/, "")}`);
  });
  setInterval(() => {
    const s = getState();
    if (document.visibilityState !== "visible" || !s?.device.paired || s.config?.heartbeat.account) return;
    syncNow("等待绑定账号", UNBOUND_SYNC_MS - 2_000);
  }, UNBOUND_SYNC_MS);
}

/** 输入：变化回调 → 输出：取消订阅的函数。chrome.storage.local 里任何插件键变化都会触发（合并 150 毫秒）。 */
export function onStateChange(fn: () => void): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const listener = (changes: Record<string, chrome.storage.StorageChange>, area: string): void => {
    if (area !== "local") return;
    if (!Object.keys(changes).some((k) => k.startsWith("pc."))) return;
    if (timer !== undefined) clearTimeout(timer);
    timer = setTimeout(fn, 150);
  };
  chrome.storage.onChanged.addListener(listener);
  return () => chrome.storage.onChanged.removeListener(listener);
}
