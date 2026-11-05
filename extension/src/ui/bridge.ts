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
