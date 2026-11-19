/** 状态 → 文案与下一步（纯函数；页面内面板、弹出页、设置页共用，Node 里可测）。
 *
 * 文案照规格 §5 的状态文案表，三端（插件、控制台、文档）用词一致：
 *   「买家号」= 一个亚马逊买家登录；「买家号 ID」= customerId；「账号」= 控制台里登记的买家号；
 *   「浏览器」= 一个装了插件的浏览器环境。界面上不出现「设备」「绑定」「配对」。
 * 判断逻辑放在 core（不是 ui/）：Node 测试只编译 core、dom、background，放进 ui/ 就测不到。
 */

import { codeLabel } from "./codes.js";
import type { ConfigCache, StateSnapshot } from "./messages.js";
import { bjHm } from "./time.js";
import type { AccountConfig, LoginStateName } from "./types.js";

export type Tone = "success" | "warning" | "danger" | "muted" | "brand";

/** 面板此刻处在哪一种状态：先看连不连得上服务端，再看登录状态。 */
export type StatusKind =
  /** 还没有登记或心跳的结果。 */
  | "connecting"
  /** 连不上服务端（断网、拒绝连接、超时）。 */
  | "offline"
  /** 服务端说插件版本过旧（PLUGIN_OUTDATED）。 */
  | "outdated"
  /** 服务端明确拒绝（要填工作区 ID、403、登记的浏览器太多……）或回的不是约定的形状。 */
  | "refused"
  | LoginStateName;

/** 状态带的按钮。「开始执行（模拟）」不在这里：它只在亚马逊页面的面板上有，能不能点看 startGate。 */
export type StatusAction = "retry" | "open_options" | "open_orders" | "reread" | "copy_id" | "open_console";

export const ACTION_LABEL: Record<StatusAction, string> = {
  retry: "重试",
  open_options: "打开设置",
  open_orders: "打开订单页",
  reread: "重新读取",
  copy_id: "复制买家号 ID",
  open_console: "打开控制台",
};

export interface PanelStatus {
  kind: StatusKind;
  tone: Tone;
  /** 短文案：收起的悬浮按钮、弹出页的标签用。 */
  short: string;
  /** 正文（规格 §5 表「插件面板」一栏）。 */
  text: string;
  /** 追加的说明（采购开关关闭、这个买家号正在别的浏览器里执行……）。 */
  extra: string[];
  actions: StatusAction[];
  /** 当前买家号 ID（完整显示、可复制）；没读到为 null。 */
  customer_id: string | null;
  /** 当前买家号对应的账号；只有 ready 才有。 */
  account: AccountConfig | null;
  /** 要所有者动手处理（悬浮按钮上带文字与状态点）。 */
  needs_owner: boolean;
}

/** unregistered 状态下快同步（10 秒一次）最多持续多久：所有者去控制台点「登记」的那段时间。 */
export const FAST_SYNC_MAX_MS = 10 * 60_000;

/** 输入：缓存的心跳、当前时刻 → 输出：要不要开快同步（10 秒一次的心跳闹钟、看得见的页面的 10 秒同步）。
 *  只在 login.state = unregistered 时开，而且自这个状态出现起最多 10 分钟；其余一律 60 秒。 */
export function fastSyncWanted(cfg: ConfigCache | null | undefined, now: number): boolean {
  if (!cfg || cfg.heartbeat.login.state !== "unregistered") return false;
  const since = cfg.unregistered_since;
  return typeof since === "number" && now >= since && now - since < FAST_SYNC_MAX_MS;
}

/** 输入：上一份缓存、新收到的登录状态、当前时刻 → 输出：新缓存的 unregistered_since。
 *  状态一直是 unregistered 且买家号 ID 没变 → 沿用原来的起点；刚变成这个状态（或换了买家号）→ 现在；否则 null。 */
export function unregisteredSince(prev: ConfigCache | null | undefined, login: { state: LoginStateName; customer_id: string | null }, now: number): number | null {
  if (login.state !== "unregistered") return null;
  const was = prev?.heartbeat.login;
  if (was && was.state === "unregistered" && was.customer_id === login.customer_id && typeof prev?.unregistered_since === "number") {
    return prev.unregistered_since;
  }
  return now;
}

const n = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : String(v));

/** 输入：账号配置 → 输出：ready 状态的那一行（规格 §5）。数字读不到写「—」，不写 0。 */
export function readyLine(a: AccountConfig): string {
  const q = a.quota_today;
  const quota = `额度 已用 ${n(q?.used)} / 上限 ${n(q?.limit)}`;
  // 没有任何已分配任务：今天可执行 0、未来也是 0（两个数都读到了才这么说）。
  if (a.executable_today === 0 && a.future_assigned === 0) {
    return `账号「${a.name}」· 还没有任务分给这个买家号，可在控制台「待采购」里查看原因 · ${quota}。`;
  }
  const future = a.future_assigned !== null && a.future_assigned > 0 ? `（另有 ${a.future_assigned} 笔建议采购日期在未来）` : "";
  return `账号「${a.name}」· 今天可执行 ${n(a.executable_today)} 笔${future || " "}· ${quota}。`;
}

/** 输入：账号的未结束批次（别的浏览器的）→ 输出：「这个买家号正在浏览器「…」里执行（HH:MM 开始）」；
 *  浏览器名、开始时间读不到时各自省掉。 */
export function busyElsewhereText(deviceName: string | null | undefined, since: string | number | null | undefined): string {
  const who = deviceName ? `浏览器「${deviceName}」` : "另一个浏览器";
  const at = bjHm(since);
  return `这个买家号正在${who}里执行${at ? `（${at} 开始）` : ""}`;
}

function connectionStatus(s: StateSnapshot): PanelStatus | null {
  const url = s.settings.serverUrl;
  const base = { extra: [] as string[], customer_id: null, account: null };
  const c = s.connection;
  if (c && !c.ok) {
    if (c.code === "PLUGIN_OUTDATED") {
      return { ...base, kind: "outdated", tone: "danger", short: "插件版本过旧", text: "连接失败：插件版本过旧，请重新构建并加载新版插件。", actions: [], needs_owner: true };
    }
    if (c.kind === "network") {
      return {
        ...base,
        kind: "offline",
        tone: "danger",
        short: "连接失败",
        text: `连接失败：连不上服务端 ${url}。请确认服务端已启动；地址不同时到设置里修改。插件会自动重试。`,
        actions: ["retry", "open_options"],
        needs_owner: true,
      };
    }
    if (c.kind === "bad_response") {
      // 回了，但不是这个版本的插件认识的形状：多半是服务端还没升级（旧版心跳没有 login）。
      return {
        ...base,
        kind: "refused",
        tone: "danger",
        short: "连接失败",
        text: "连接失败：服务端的答复不是这个版本的插件认识的格式。服务端可能还是旧版本，请先升级服务端（停服务、运行 db_init、再启动）。",
        actions: ["retry", "open_options"],
        needs_owner: true,
      };
    }
    const why = (c.message ?? codeLabel(c.code)).replace(/[。.]\s*$/, "");
    const halted = s.registration && !s.registration.ok && s.registration.halted;
    const wait = s.registration && !s.registration.ok && s.registration.next_at !== null && c.code === "RATE_LIMITED";
    return {
      ...base,
      kind: "refused",
      tone: "danger",
      short: "连接失败",
      text: `连接失败：${why}。${halted ? "处理后点「重试」。" : wait ? "插件稍后会自动重试。" : ""}`,
      actions: c.code === "WORKSPACE_REQUIRED" ? ["open_options", "retry"] : ["retry", "open_options"],
      needs_owner: true,
    };
  }
  if (!s.device.registered || !s.config) {
    return { ...base, kind: "connecting", tone: "muted", short: "正在连接", text: `正在连接服务端 ${url}。`, actions: [], needs_owner: false };
  }
  return null;
}

/** 输入：状态快照 → 输出：面板此刻的状态、文案与按钮。 */
export function panelStatus(s: StateSnapshot): PanelStatus {
  const conn = connectionStatus(s);
  if (conn) return conn;
  const hb = s.config!.heartbeat;
  // 本地刚对出分歧、还没等到服务端的答复时，也按分歧显示（服务端收到带 conflict 的上报后结论一样）。
  const state: LoginStateName = s.identity?.conflict ? "conflict" : hb.login.state;
  const id = hb.login.customer_id;
  const base = { extra: [] as string[], customer_id: null as string | null, account: null as AccountConfig | null };
  switch (state) {
    case "signed_out":
      return {
        ...base, kind: state, tone: "warning", short: "未登录亚马逊", needs_owner: true, actions: [],
        text: "未登录亚马逊。请在这个浏览器登录要用来采购的买家号，登录后这里自动更新。",
      };
    case "no_id":
      return {
        ...base, kind: state, tone: "warning", short: "没读到买家号 ID", needs_owner: true, actions: ["open_orders"],
        text: "这一页没有读到买家号 ID。打开订单页后插件会自动读取。",
      };
    case "conflict": {
      // 帮所有者找到是哪一页：其中一页显示的是谁（面板上买家号 ID 完整显示）。
      const one = s.identity?.conflict ? `其中一页显示：${s.identity.signed_in ? `买家号 ID ${s.identity.customer_id ?? "—"}` : "未登录"}。` : null;
      return {
        ...base, kind: state, tone: "danger", short: "买家号不一致", needs_owner: true, actions: ["reread"], extra: one ? [one] : [],
        text: "这个浏览器的亚马逊标签页显示的买家号不一致。请刷新或关闭较早打开的亚马逊标签页。",
      };
    }
    case "unregistered":
      return {
        ...base, kind: state, tone: "warning", short: "买家号还没有登记", needs_owner: true, customer_id: id,
        actions: id ? ["copy_id", "open_console"] : ["open_console"],
        text: `买家号 ${id ?? "—"} 还没有登记。在控制台「账号」页的待登记列表里点「登记」，之后这里自动更新。`,
      };
    case "ignored":
      return {
        ...base, kind: state, tone: "muted", short: "买家号已忽略", needs_owner: false, customer_id: id, actions: id ? ["copy_id"] : [],
        text: `买家号 ${id ?? "—"} 已在控制台忽略，不会分到任务。要使用它，在控制台「账号」页恢复后登记。`,
      };
    case "ready": {
      const a = hb.account;
      if (!a) break; // 服务端说 ready 却没给账号：不往 ready 猜
      if (a.execution_paused) {
        return {
          ...base, kind: state, tone: "warning", short: "已暂停执行", needs_owner: true, customer_id: id ?? a.amazon_customer_id, account: a, actions: [],
          text: `已暂停执行：${a.pause_reason ?? "没有填写原因"}。处理后在控制台「账号」页恢复执行。`,
        };
      }
      const extra: string[] = [];
      if (a.purchase_enabled === false) extra.push("控制台里这个账号的采购开关是关闭的，不会分到新任务；已分配的任务照常执行。");
      if (a.active_run && !a.active_run.is_mine) extra.push(`${busyElsewhereText(a.active_run.device_name, a.active_run.started_at)}。`);
      return { kind: state, tone: "success", short: `账号「${a.name}」`, needs_owner: false, customer_id: id ?? a.amazon_customer_id, account: a, actions: [], text: readyLine(a), extra };
    }
    default:
      break;
  }
  return {
    ...base, kind: "unknown", tone: "muted", short: "还没有读到登录状态", needs_owner: false, actions: [],
    text: "还没有读到登录状态。打开或刷新亚马逊页面后自动读取。",
  };
}

/** 控制台「浏览器」页签「当前登录」一栏的说法（规格 §5 表右栏）：设置页的状态区用同一套，三端一致。 */
export function loginLabel(state: LoginStateName, customerId: string | null, accountName: string | null, paused = false): string {
  switch (state) {
    case "signed_out":
      return "未登录亚马逊";
    case "no_id":
      return "已登录，没读到买家号 ID";
    case "conflict":
      return "登录状态不一致";
    case "unregistered":
      return `未登记的买家号 ${customerId ?? "—"}`;
    case "ignored":
      return `已忽略的买家号 ${customerId ?? "—"}`;
    case "ready":
      return `${accountName ?? "—"}${paused ? "（已暂停）" : ""}`;
    default:
      return "还没有上报";
  }
}
