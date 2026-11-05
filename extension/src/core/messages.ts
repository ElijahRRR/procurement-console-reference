/** 内容脚本 / 设置页 / 弹出页 与 后台之间的消息（只有类型与常量，不碰 chrome API）。
 *
 * 所有服务端请求都经由后台：设备令牌只在后台使用，存在扩展自己源下的 IndexedDB 里
 * （background/secrets.ts）。内容脚本跑在亚马逊页面的源下，打不开那个库；chrome.storage.local
 * 内容脚本是读得到的，所以令牌**不**放在那里。
 * 引擎在内容脚本里跑，它的每一次协议调用都是一条 `pc.api` 消息。
 */

import type { ApiResult } from "./client.js";
import type { EngineView } from "./engine.js";
import type { IdentitySnapshot, ReconciledIdentity } from "./identity.js";
import type { LogLevel, LogLine } from "./log.js";
import type { ReceiptView } from "./receipts.js";
import type { ScheduleDayState } from "./schedule.js";
import type { Settings } from "./settings.js";
import type { Browser, HeartbeatOut, IdentityStatus, Trigger } from "./types.js";

/** 存储键（chrome.storage.local 与 chrome.storage.session）。 */
export const KEYS = {
  settings: "pc.settings",
  device: "pc.device",
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
} as const;

/** 本地保存的设备信息（chrome.storage.local）。**不含设备令牌**：令牌在 IndexedDB 里（background/secrets.ts）。
 *  早期版本把 `token` 也写在这里，后台第一次读到时搬走并删掉这个字段。 */
export interface DeviceRecord {
  token?: string;
  device_id: number | string;
  device_name: string;
  browser: Browser;
  workspace_name: string | null;
  server_url: string;
  paired_at: number;
}

/** 给界面看的设备信息（不含令牌）。 */
export interface DeviceView {
  paired: boolean;
  device_id: number | string | null;
  device_name: string | null;
  browser: Browser | null;
  workspace_name: string | null;
  server_url: string | null;
  paired_at: number | null;
}

/** 最近一次心跳的结论。 */
export interface ConnectionState {
  ok: boolean;
  at: number;
  code: string | null;
  message: string | null;
}

/** 缓存的心跳配置。 */
export interface ConfigCache {
  heartbeat: HeartbeatOut;
  received_at: number;
  /** 服务端时刻减本机时刻（毫秒）。 */
  clock_offset_ms: number;
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
  config: ConfigCache | null;
  connection: ConnectionState | null;
  /** 各标签页读数对账之后要上报的身份（见 core/identity.reconcileIdentity）。 */
  identity: ReconciledIdentity | null;
  identity_status: IdentityStatus;
  /** 还没送达服务端的结果回执。 */
  receipts: ReceiptView[];
  logs: LogLine[];
  schedule: ScheduleDayState | null;
  run: RunViewRecord | null;
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
  | { type: "pc.pair"; serverUrl: string; code: string; deviceName: string; browser: Browser }
  | { type: "pc.unpair" }
  | { type: "pc.saveSettings"; patch: Partial<Settings> }
  | { type: "pc.identity"; identity: IdentitySnapshot | null; force: boolean }
  | { type: "pc.heartbeatNow" }
  /** 界面打开或回到前台：上一次心跳早于 minAgeMs 就马上同步一次（拿到刚在控制台改的绑定、配置）。 */
  | { type: "pc.sync"; reason: string; minAgeMs?: number }
  /** 开始执行前：让每个亚马逊标签页重读身份、对账、上报，并同步一次心跳。 */
  | { type: "pc.prepareStart" }
  | { type: "pc.api"; op: ApiOp; args: unknown[] }
  | { type: "pc.lease"; busy: boolean }
  | { type: "pc.leaseRelease" }
  | { type: "pc.runView"; view: EngineView; active: boolean }
  | { type: "pc.log"; level: LogLevel; text: string }
  /** 亚马逊页面加载好了（内容脚本启动）。loaded_at = 这一页的加载时刻（performance.timeOrigin）。 */
  | { type: "pc.hello"; href: string; loaded_at?: number }
  /** 亚马逊页面要离开了（pagehide：刷新、跳转、关闭）。run_id = 这一页正在跑的批次（没有为 null）。 */
  | { type: "pc.bye"; loaded_at?: number; run_id: number | null }
  | { type: "pc.openOptions" };

/** 后台发给内容脚本的消息。 */
export type TabMsg =
  | { type: "pc.startScheduled"; slots: string[] }
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
  /** 身份对账有分歧（各标签页读到的登录账号不一致）：插件自己就不开始。 */
  conflict: boolean;
  identity_status: IdentityStatus | null;
  message: string | null;
}

export type ApiReply = ApiResult<unknown>;

export interface LeaseReply {
  granted: boolean;
  held_by: number | null;
  reason: string;
}

/** 内容脚本对定时触发的答复。 */
export interface ScheduledReply {
  /** started：已开始；busy：本浏览器已在执行（本页或另一页）或账号在别的设备上执行；
   *  rejected：服务端拒绝开始；network：没说上话；not_ready：未配对等；
   *  retry：暂时开始不了、过一会儿自然会好（本设备上一次的批次租约还没到期）。
   *  network / not_ready / retry 留到下次检查再试，其余算处理过。 */
  result: "started" | "busy" | "rejected" | "network" | "not_ready" | "retry";
  message: string;
}
