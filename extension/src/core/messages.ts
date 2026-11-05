/** 内容脚本 / 设置页 / 弹出页 与 后台之间的消息（只有类型与常量，不碰 chrome API）。
 *
 * 所有服务端请求都经由后台：设备令牌只在后台使用，内容脚本永远拿不到它。
 * 引擎在内容脚本里跑，它的每一次协议调用都是一条 `pc.api` 消息。
 */

import type { ApiResult } from "./client.js";
import type { EngineView } from "./engine.js";
import type { IdentitySnapshot } from "./identity.js";
import type { LogLevel, LogLine } from "./log.js";
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
  /** session：标签页级执行租约。 */
  tabLease: "pc.tabLease",
  /** session：当前执行批次（后台补发批次心跳用）。 */
  activeRun: "pc.activeRun",
  /** session：上一次单独上报身份的时刻与内容（去重用）。 */
  identitySent: "pc.identitySent",
} as const;

/** 本地保存的设备信息。`token` 只在后台读取。 */
export interface DeviceRecord {
  token: string;
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
  identity: IdentitySnapshot | null;
  identity_status: IdentityStatus;
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
  | { type: "pc.api"; op: ApiOp; args: unknown[] }
  | { type: "pc.lease"; busy: boolean }
  | { type: "pc.leaseRelease" }
  | { type: "pc.runView"; view: EngineView; active: boolean }
  | { type: "pc.log"; level: LogLevel; text: string }
  | { type: "pc.hello"; href: string }
  | { type: "pc.openOptions" };

/** 后台发给内容脚本的消息。 */
export type TabMsg =
  | { type: "pc.startScheduled"; slots: string[] }
  | { type: "pc.readIdentity" }
  | { type: "pc.stopRun"; reason: string };

export type ApiReply = ApiResult<unknown>;

export interface LeaseReply {
  granted: boolean;
  held_by: number | null;
  reason: string;
}

/** 内容脚本对定时触发的答复。 */
export interface ScheduledReply {
  /** started：已开始；busy：本浏览器已在执行（本页或另一页）或账号在别处执行；
   *  rejected：服务端拒绝开始；network：没说上话（留到下次检查再试）；not_ready：未配对等。 */
  result: "started" | "busy" | "rejected" | "network" | "not_ready";
  message: string;
}
