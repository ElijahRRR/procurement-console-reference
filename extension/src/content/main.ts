/** 内容脚本入口（https://www.amazon.com/*，document_idle，只在顶层页面）。
 *
 * 做三件事，都不导航、不点击、不改动亚马逊页面本身：
 *   1. 读身份：页面加载时、每 2 分钟、以及按需（「刷新身份」、开始执行前后台来问），
 *      从导航栏与内联脚本读登录状态与 customerId，连同**这一页的加载时刻**交给后台对账、上报
 *      （同一页重读一百遍也只是加载那一刻的样子：后台比先后只看加载时刻，见 core/identity.ts）；
 *   2. 承载执行引擎（本阶段只做模拟）：手动点「开始执行（模拟）」或后台在定时到点时调用；
 *      后台随时可以问一声「引擎还在吗」（pc.ping），页面离开时告诉后台（pc.bye）；
 *   3. 挂一个 Shadow DOM 面板（订单历史页默认展开，其余页面是收起的悬浮按钮）。
 * 唯一往页面里加的东西是面板的宿主节点；页面的样式进不来，我们的样式也出不去。
 */

import { identityFromPage, type IdentitySnapshot } from "../core/identity.js";
import type { Msg, PingReply, ScheduledReply, TabMsg } from "../core/messages.js";
import { readCustomerId, readLoginState } from "../dom/parse.js";
import { toSw } from "../ui/bridge.js";
import { mountPanel } from "./panel.js";
import { RunHost } from "./runner.js";

const IDENTITY_REFRESH_MS = 2 * 60_000;
/** 这一页的加载时刻（epoch 毫秒）。 */
const LOADED_AT = Math.round(performance.timeOrigin);

declare global {
  interface Window {
    __pcAssistantLoaded?: boolean;
  }
}

/** 输入：无 → 输出：这一页此刻读到的身份快照（登录状态读不出来时为 null）。不上报。 */
function readPage(): IdentitySnapshot | null {
  const href = location.href;
  const login = readLoginState(document, href);
  const cid = login === "ok" ? readCustomerId(document) ?? null : null;
  const snap = identityFromPage(login, cid, href, Date.now());
  return snap ? { ...snap, loaded_at: LOADED_AT } : null;
}

/** 输入：是否强制上报 → 输出：这一次读到的身份快照（登录状态读不出来时为 null）。 */
async function readIdentity(force: boolean): Promise<IdentitySnapshot | null> {
  const snap = readPage();
  if (!snap && !force) return null;
  try {
    await toSw({ type: "pc.identity", identity: snap, force }, 45_000);
  } catch {
    // 后台没有响应：下一次读取再报
  }
  return snap;
}

function boot(): void {
  if (window.top !== window) return; // 只在顶层页面
  if (window.__pcAssistantLoaded) return; // 防止重复注入
  window.__pcAssistantLoaded = true;

  const host = new RunHost();

  chrome.runtime.onMessage.addListener((msg: TabMsg, _sender, respond) => {
    if (msg?.type === "pc.startScheduled") {
      host.start("scheduled").then(respond, () =>
        respond({ result: "rejected", message: "开始执行失败：页面内部错误" } satisfies ScheduledReply),
      );
      return true;
    }
    if (msg?.type === "pc.readIdentity") {
      // 后台来问（开始执行前、刚配对完）：读一次本页，读数直接放在答复里，由后台对账。
      respond({ ok: true, identity: readPage() });
      return false;
    }
    if (msg?.type === "pc.ping") {
      respond({ running: host.running, run_id: host.view()?.run_id ?? null } satisfies PingReply);
      return false;
    }
    if (msg?.type === "pc.stopRun") {
      host.stop(String(msg.reason ?? "后台要求停止"));
      respond({ ok: true });
      return false;
    }
    return false;
  });

  void readIdentity(false);
  setInterval(() => void readIdentity(false), IDENTITY_REFRESH_MS);
  // 亚马逊页面打开了 = 环境上线：后台对一下「执行页面是不是刚被刷新」，同步一次心跳，检查今天错过的定时。
  void toSw({ type: "pc.hello", href: location.origin + location.pathname, loaded_at: LOADED_AT }).catch(() => undefined);
  // 页面要离开了（刷新、跳转、关闭）：告诉后台。正在跑的批次由后台接管，不用等它自己发现。
  window.addEventListener("pagehide", () => {
    const bye: Msg = { type: "pc.bye", loaded_at: LOADED_AT, run_id: host.view()?.run_id ?? null };
    try {
      void chrome.runtime.sendMessage(bye).catch(() => undefined);
    } catch {
      // 扩展已更新或已卸载：这一页和后台已经断开
    }
  });

  mountPanel(host, readIdentity);
}

boot();
