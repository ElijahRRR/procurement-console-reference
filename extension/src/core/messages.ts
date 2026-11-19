/** 内容脚本 / 设置页 / 弹出页 与 后台之间的消息（只有类型与常量，不碰 chrome API）。
 *
 * 所有服务端请求都经由后台：设备令牌由后台生成、只在后台使用，存在扩展自己源下的 IndexedDB 里
 * （background/secrets.ts）。内容脚本跑在亚马逊页面的源下，打不开那个库；chrome.storage.local
 * 内容脚本是读得到的，所以令牌**不**放在那里。
 * 引擎在内容脚本里跑，它的每一次协议调用都是一条 `pc.api` 消息。
 */

import type { ApiResult } from "./client.js";
import type { EngineView } from "./engine.js";
import type { IdentitySnapshot, ReconciledIdentity } from "./identity.js";
import type { LogLevel, LogLine } from "./log.js";
import type { ReceiptView } from "./receipts.js";
import type { RegisterStatus } from "./register.js";
import type { ScheduleDayState } from "./schedule.js";
import type { Settings } from "./settings.js";
import type { HeartbeatOut, LoginStateName, Trigger } from "./types.js";

/** 存储键（chrome.storage.local 与 chrome.storage.session）。 */
export const KEYS = {
  settings: "pc.settings",
  device: "pc.device",
  /** local：最近一次登记的结论（core/register.RegisterStatus）。 */
  registration: "pc.registration",
  config: "pc.config",
  connection: "pc.connection",
  identity: "pc.identity",
  logs: "pc.logs",
  schedule: "pc.schedule",
  runView: "pc.runView",
  /** local：还没送达服务端的结果回执（core/receipts.ts）。 */
  receipts: "pc.receipts",
  /** session：标签页级执行租约。 */
  tabLease: "pc.tabLease",
  /** session：当前执行批次（后台补发批次心跳、接管断了引擎的批次用）。 */
  activeRun: "pc.activeRun",
  /** session：上一次单独上报身份的时刻与内容（去重用）。 */
  identitySent: "pc.identitySent",
  /** session：各标签页、各次页面加载读到的身份（core/identity.IdentityStore）。 */
  identityObs: "pc.identityObs",
  /** session：上一次发心跳的时刻（「打开面板就同步」的节流用）。 */
  heartbeatTriedAt: "pc.heartbeatTriedAt",
  /** session：上一次因为 401 重新登记成功的时刻（防止登记与 401 打转）。 */
  reregisteredAt: "pc.reregisteredAt",
} as const;

/** 这个浏览器在服务端的登记（chrome.storage.local）。**不含设备令牌**：令牌在 IndexedDB 里（background/secrets.ts）。
 *  有这条记录、而且 server_url 就是令牌所属的地址 = 登记成功过。
 *  旧版（配对得到的）记录也读得了：`paired_at` 当作 registered_at；早期把 `token` 也写在这里，
 *  后台第一次读到时搬走并删掉这个字段；`browser` 等旧字段忽略。 */
export interface DeviceRecord {
  /** 早期版本遗留，读到即搬走。 */
  token?: string;
  device_id: number | string;
  /** 服务端起的名字（「浏览器 <id>」），只能在控制台改；心跳带回新名字时更新。 */
  device_name: string | null;
  workspace_id?: number | null;
  workspace_name: string | null;
  server_url: string;
  registered_at?: number;
  /** 旧版字段。 */
  paired_at?: number;
}

/** 给界面看的登记信息（不含令牌）。 */
export interface DeviceView {
  /** 登记成功过（有登记记录，地址与当前服务端地址一致）。 */
  registered: boolean;
  device_id: number | string | null;
  device_name: string | null;
  workspace_id: number | null;
  workspace_name: string | null;
  registered_at: number | null;
}

/** 最近一次与服务端说话（登记或心跳）的结论。 */
export interface ConnectionState {
  ok: boolean;
  at: number;
  /** 失败的种类：network = 没说上话（断网、拒绝连接、超时）；http = 服务端明确拒绝；bad_response = 回了但不是约定的形状。 */
  kind: "network" | "http" | "bad_response" | null;
  code: string | null;
  message: string | null;
}

/** 缓存的心跳（或身份上报的返回，两者同形）。 */
export interface ConfigCache {
  heartbeat: HeartbeatOut;
  received_at: number;
  /** 服务端时刻减本机时刻（毫秒）。 */
  clock_offset_ms: number;
  /** 登录状态从什么时候起一直是 unregistered（同一个买家号 ID）；不是这个状态为 null。
   *  快心跳只在这个状态下开，而且自它出现起最多 10 分钟（core/status.fastSyncWanted）。 */
  unregistered_since: number | null;
}

/** 当前执行批次（后台在 storage.session 里记，跨后台重启保留）。 */
export interface ActiveRun {
  run_id: number;
  tab_id: number;
  trigger: Trigger;
  started_at: number;
  /** 引擎最近一次通过后台发协议请求的时刻：后台补发心跳前看它，引擎卡死就不替它续命。 */
  touched_at: number;
  /** 服务端给的租约有效期（秒）；不知道为 null（按 180 秒）。 */
  lease_ttl_s: number | null;
  /** 最近一次确认续租成功（或开始批次）的时刻：租约至少到 last_beat_ok_at + 有效期。 */
  last_beat_ok_at: number;
  /** 执行页面已经没了（刷新、离开、关闭）或引擎已中断、但批次还有回执没送到：后台接管的时刻。 */
  orphaned_at: number | null;
  /** 接管的原因（page_reloaded、tab_closed、engine_gone、engine_ended）。 */
  orphan_reason: string | null;
  /** 开始这个批次时用的买家号 ID（= 批次账号登记的 ID）：多标签页对账的基准（core/identity.reconcileIdentity）。 */
  customer_id: string | null;
  /** 批次的账号名（服务端在开始批次的答复里给的）；没给为 null。 */
  account_name: string | null;
}

/** 给界面看的「本浏览器还有一个没结束的批次」（含后台接管、等回执送达的那段时间）。 */
export interface ActiveRunView {
  run_id: number;
  account_name: string | null;
  started_at: number;
  /** 引擎已经不在了，后台在替它收尾。 */
  orphaned: boolean;
}

/** 给所有面板看的执行状态（storage.local，非权威）。 */
export interface RunViewRecord {
  active: boolean;
  tab_id: number | null;
  view: EngineView | null;
  updated_at: number;
}

/** 界面一次取全的状态。 */
export interface StateSnapshot {
  settings: Settings;
  device: DeviceView;
  /** 最近一次登记的结论；还没试过为 null。 */
  registration: RegisterStatus | null;
  config: ConfigCache | null;
  connection: ConnectionState | null;
  /** 各标签页读数对账之后要上报的身份（见 core/identity.reconcileIdentity）。 */
  identity: ReconciledIdentity | null;
  /** 还没送达服务端的结果回执。 */
  receipts: ReceiptView[];
  logs: LogLine[];
  /** 当前买家号（心跳里 account 的买家号 ID）今天的定时记录；没有账号为 null。 */
  schedule: ScheduleDayState | null;
  run: RunViewRecord | null;
  /** 后台记着的当前批次（权威在服务端；这里用来决定「开始执行」发不发请求）。 */
  active_run: ActiveRunView | null;
  ext_version: string;
}

/** 引擎可调用的协议操作。 */
export type ApiOp =
  | "startRun"
  | "runHeartbeat"
  | "next"
  | "progress"
  | "guard"
  | "submitting"
  | "verifying"
  | "result"
  | "finishRun";

export type Msg =
  | { type: "pc.getState" }
  /** 【特权】保存设置。改服务端地址、工作区 ID 会换一个新令牌重新登记（有进行中的批次或未送达的回执时拒绝）。 */
  | { type: "pc.saveSettings"; patch: Partial<Settings> }
  /** 【特权】设置页「重新登记」：带着同一个令牌立即再登记一次。 */
  | { type: "pc.reregister" }
  /** 【特权】清空日志。 */
  | { type: "pc.clearLogs" }
  /** 面板、弹出页的「重试」：还没连上服务端时马上再试一次登记（不等退避；429 的等待照旧），然后同步心跳。 */
  | { type: "pc.retry" }
  | { type: "pc.identity"; identity: IdentitySnapshot | null; force: boolean }
  | { type: "pc.heartbeatNow" }
  /** 界面打开或回到前台：上一次心跳早于 minAgeMs 就马上同步一次（拿到刚在控制台登记的账号、改过的配置）。 */
  | { type: "pc.sync"; reason: string; minAgeMs?: number }
  /** 开始执行前：identity = 发起的那一页刚读到的身份。后台记下它，让其余亚马逊标签页也重读、对账；
   *  有分歧就不开始，否则把结论报给服务端（POST /identity）。 */
  | { type: "pc.prepareStart"; identity: IdentitySnapshot | null }
  /** 执行中（提交登记之前、每隔几分钟）与面板的「重新读取」：让每个亚马逊标签页重读身份、对账、强制上报（不发心跳）。 */
  | { type: "pc.refreshIdentity" }
  | { type: "pc.api"; op: ApiOp; args: unknown[] }
  | { type: "pc.lease"; busy: boolean }
  | { type: "pc.leaseRelease" }
  | { type: "pc.runView"; view: EngineView; active: boolean }
  | { type: "pc.log"; level: LogLevel; text: string }
  /** 亚马逊页面加载好了（内容脚本启动）。loaded_at = 这一页的加载时刻（performance.timeOrigin）。 */
  | { type: "pc.hello"; href: string; loaded_at?: number }
  /** 亚马逊页面要离开了（pagehide：刷新、跳转、关闭）。run_id = 这一页正在跑的批次（没有为 null）。 */
  | { type: "pc.bye"; loaded_at?: number; run_id: number | null }
  | { type: "pc.openOptions" }
  /** 所有者点了「打开控制台」：后台新开一个标签页到 服务端地址 + /accounts（地址由后台定，消息里不带）。 */
  | { type: "pc.openConsole" }
  /** 所有者点了「打开订单页」：后台新开一个标签页到亚马逊订单历史页（固定地址）。 */
  | { type: "pc.openOrders" };

/** 特权消息：只接受扩展自己的页面（设置页、弹出页）发来的；内容脚本发来的一律拒绝。 */
export const PRIVILEGED_TYPES: ReadonlySet<string> = new Set(["pc.saveSettings", "pc.reregister", "pc.clearLogs"]);

/** 设置页的文件名（清单 options_ui.page）。 */
export const OPTIONS_PAGE = "options.html";

/** 消息发送方里用得到的那几项（chrome.runtime.MessageSender 的子集；这些都是浏览器填的，页面改不了）。 */
export interface SenderLike {
  /** 发送方所在的标签页。内容脚本一定有；扩展页面只有在标签页里打开时才有（弹出页、内嵌的设置页没有）。 */
  tab?: unknown;
  /** 发送方文档的地址：内容脚本是它所在网页的地址，扩展页面是 chrome-extension://<id>/…。 */
  url?: unknown;
  /** 发送方文档的源（Chrome 80+）。 */
  origin?: unknown;
}

/** 输入：消息的发送方、扩展自己的根地址（chrome.runtime.getURL("")）→ 输出：是不是扩展自己的页面。
 *
 *  规则（规格 §3）：`sender.tab` 为空，且 `sender.url` 以扩展的根地址开头。内容脚本的消息一定带着 sender.tab，
 *  地址也是它所在网页的，两条都过不了：跑在亚马逊页面渲染进程里的内容脚本不能改服务端地址、不能触发
 *  重新登记（任务与护栏裁决都来自那个地址）。
 *
 *  唯一的例外：**设置页是在标签页里打开的**（清单 options_ui.open_in_tab = true），它发来的消息也带着
 *  sender.tab。所以带着 tab 的发送方只在「文档就是扩展自己的设置页、而且浏览器报的源就是扩展的源」时放行；
 *  别的带 tab 的发送方（包括只有地址像、没有源的）一律拒绝。 */
export function isPrivilegedSender(sender: SenderLike | null | undefined, extensionRoot: string): boolean {
  if (!sender || extensionRoot === "" || typeof sender.url !== "string" || !sender.url.startsWith(extensionRoot)) return false;
  const origin = extensionRoot.replace(/\/+$/, "");
  if (sender.origin !== undefined && sender.origin !== null && sender.origin !== origin) return false;
  if (sender.tab === undefined || sender.tab === null) return true;
  const page = sender.url.slice(extensionRoot.length).split(/[?#]/)[0];
  return page === OPTIONS_PAGE && sender.origin === origin;
}

/** 后台发给内容脚本的消息。 */
export type TabMsg =
  /** 定时到点：请这一页以 customer_id 这个买家号开始定时批次。slot = 向服务端申请时带的时间点；
   *  slots = 今天到期、这一次一并处理的全部时间点（日志用）。这一页登的不是这个买家号时答 next_tab。 */
  | { type: "pc.startScheduled"; slot: string; slots: string[]; customer_id: string }
  /** 重读本页身份；答复 {ok, identity}（IdentitySnapshot，含 loaded_at）。 */
  | { type: "pc.readIdentity" }
  | { type: "pc.stopRun"; reason: string }
  /** 本页的引擎还活着吗；答复 PingReply。 */
  | { type: "pc.ping" };

/** 内容脚本对 pc.ping 的答复。run_id 为 null 而 running 为 true：正在申请批次。 */
export interface PingReply {
  running: boolean;
  run_id: number | null;
}

/** 后台对 pc.prepareStart 的答复。 */
export interface PrepareReply {
  /** 可以去申请批次了：没有分歧、对账结论已报给服务端、本地没有还没结束的批次。 */
  ok: boolean;
  /** 身份对账有分歧（各标签页读到的买家号不一致）：插件自己就不开始。 */
  conflict: boolean;
  /** 对账结论里的买家号 ID（已登录且读到了才有）。 */
  customer_id: string | null;
  /** 服务端按这份身份算出的登录状态（没报上去为 null）。 */
  login_state: LoginStateName | null;
  /** 不能开始时的原因码（本地码或服务端错误码）；可以开始为 null。 */
  code: string | null;
  /** 不能开始时给人看的一句话（「开始执行失败：……」）。 */
  message: string | null;
}

/** 后台对 pc.refreshIdentity 的答复。 */
export interface RefreshIdentityReply {
  /** 这一次把一份新鲜的身份报到了服务端。 */
  reported: boolean;
  conflict: boolean;
  login_state: LoginStateName | null;
}

export type ApiReply = ApiResult<unknown>;

export interface LeaseReply {
  granted: boolean;
  held_by: number | null;
  reason: string;
}

/** 「开始执行」那一步的结论：面板的手动开始、后台的定时触发都拿它。 */
export interface ScheduledReply {
  /** started：已开始。
   *  busy：这个买家号正在别的浏览器里执行（ACCOUNT_BUSY，包括同一个时间点的定时批次还没执行完），或本浏览器还有批次没结束
   *  （DEVICE_BUSY、另一个标签页在执行）。
   *  rejected：服务端明确拒绝（见 code）。
   *  network：没说上话。 not_ready：还没连上服务端、后台没有响应。
   *  identity：这个浏览器的登录状态对不上（没登录、标签页读数有分歧、读数还没报上去）。
   *  next_tab：这一页开始不了（没读到买家号 ID，或登的不是定时所属的买家号），定时触发时换下一个标签页。
   *
   *  定时触发怎么记「今天这个时间点处理过了」（规格 §3）：started 记；rejected 且 code 是 SLOT_DONE（今天已有正常结束的批次）、
   *  VALIDATION（422：先刷新配置）、ACCOUNT_PAUSED 记；其余（busy、network、not_ready、identity……）都不记，
   *  下一分钟再问 —— 去重的权威在服务端。 */
  result: "started" | "busy" | "rejected" | "network" | "not_ready" | "identity" | "next_tab";
  /** 服务端错误码或插件本地码；started 为 null。 */
  code: string | null;
  message: string;
}
