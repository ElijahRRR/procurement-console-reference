/** 内容脚本、设置页、弹出页 → 后台的消息通道（有界等待）。 */

import { withTimeout } from "../core/gates.js";
import type { Msg, StateSnapshot } from "../core/messages.js";
import { fastSyncWanted } from "../core/status.js";

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

/** 买家号还没有登记时，看得见的页面多久同步一次。 */
export const FAST_SYNC_MS = 10_000;

/** 输入：触发原因、取当前状态的函数 → 输出：无。界面打开、回到前台时同步一次（还没连上服务端时，
 *  这一次同步也会让后台再试一次登记）。登录状态是「买家号还没有登记」、页面又看得见时每 10 秒同步一次，
 *  自这个状态出现起最多 10 分钟（core/status.fastSyncWanted）：所有者在控制台点了「登记」，
 *  这里几秒内显示账号、「开始执行」可以点。其余状态只靠后台 60 秒一次的心跳。 */
export function keepFresh(reason: string, getState: () => StateSnapshot | null): void {
  syncNow(reason);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") syncNow(`回到${reason.replace(/^打开/, "")}`);
  });
  setInterval(() => {
    const s = getState();
    if (document.visibilityState !== "visible" || !s || !fastSyncWanted(s.config, Date.now())) return;
    syncNow("等待登记买家号", FAST_SYNC_MS - 2_000);
  }, FAST_SYNC_MS);
}

/** 输入：要复制的文本、临时节点挂在哪（面板的 Shadow DOM 根或页面的 body）→ 输出：复制成没成。
 *  先用 navigator.clipboard（要页面有焦点、在点击里调用；不需要 clipboardWrite 权限）；
 *  不行再退回「选中临时文本框 + 复制命令」。临时节点只挂在我们自己的容器里，用完就摘掉。 */
export async function copyText(text: string, mount: ParentNode): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 落到下面的退路
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    mount.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
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
