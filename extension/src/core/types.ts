/** 插件 ↔ 服务端的协议类型（开发契约 v2 §4、§5、§10；执行入口按规格 v3）。插件这一侧只有这一份。
 *
 * 约定（契约 §10）：金额是两位小数的字符串（`"52.00"`），可空；日期 `YYYY-MM-DD`；
 * 时刻 ISO 8601 带时区；字段缺失与 `null` 等价。**null 不是 0**。
 */

export type Trigger = "manual" | "scheduled";

/** 服务端解析出的登录状态全集（规格 §2.3）：
 *    unknown       从未上报；
 *    signed_out    未登录亚马逊；
 *    no_id         已登录，但没读到买家号 ID；
 *    conflict      各标签页的读数不一致；
 *    unregistered  读到了买家号 ID，控制台里还没有登记它；
 *    ignored       这个买家号 ID 在控制台被忽略了；
 *    ready         买家号 ID 对应一个已登记、未归档的账号。
 *  认不出的取值一律按 unknown（不往 ready 猜）。 */
export type LoginStateName = "unknown" | "signed_out" | "no_id" | "conflict" | "unregistered" | "ignored" | "ready";

/** 身份上报（规格 §2.3）：心跳捎带的 `identity`，或单独 `POST /plugin/v1/identity` 的请求体。 */
export interface Identity {
  signed_in: boolean;
  customer_id: string | null;
  site: "US";
  /** 只有 origin + pathname，不带查询串与片段。 */
  page_url: string | null;
  /** 这个浏览器的各个亚马逊标签页读数不一致：服务端据此把登录状态记为 conflict，不解析账号。 */
  conflict: boolean;
}

/** `POST /plugin/v1/register` 的返回（规格 §2.2）。设备令牌由插件生成，返回里没有令牌。 */
export interface RegisterOut {
  device_id: number | string;
  device_name: string | null;
  workspace_id: number | null;
  workspace_name: string | null;
  /** 这一次是不是新建了一行（false = 令牌已登记过，幂等返回）。 */
  created: boolean;
}

/** 心跳里的登录状态。 */
export interface LoginInfo {
  state: LoginStateName;
  customer_id: string | null;
  /** 服务端最近一次收到身份上报的时刻（ISO）；从未上报为 null。 */
  at: string | null;
  /** 距最近一次上报超过 identity_fresh_s（默认 600 秒）。 */
  stale: boolean;
}

export interface QuotaToday {
  limit: number | null;
  used: number | null;
  planned: number | null;
  available: number | null;
}

/** 账号的未结束批次（心跳里 `account.active_run`）。 */
export interface ActiveRunInfo {
  id: number;
  device_id: number | null;
  device_name: string | null;
  started_at: string | null;
  /** 是不是这个浏览器自己的批次。 */
  is_mine: boolean;
}

/** 心跳里当前买家号对应账号的有效配置（规格 §2.4）。只在 `login.state = ready` 时有。
 *  卡尾号与金额区间不在这里：只在领取后的尝试载荷里。 */
export interface AccountConfig {
  id: number;
  name: string;
  amazon_customer_id: string | null;
  purchase_enabled: boolean | null;
  execution_paused: boolean | null;
  pause_reason: string | null;
  purchase_mode: string | null;
  schedule_times: string[];
  effective_max_delivery_days: number | null;
  quota_today: QuotaToday | null;
  /** 今天可执行的笔数（与领取条件同口径，不与剩余额度取小）；没读到为 null，不是 0。 */
  executable_today: number | null;
  /** 已分配给这个账号、建议采购日期在今天之后的笔数；没读到为 null。 */
  future_assigned: number | null;
  active_run: ActiveRunInfo | null;
}

/** 这个浏览器自己的未结束批次（不论账号；心跳里的 `device_run`）。 */
export interface DeviceRunInfo {
  id: number;
  account_id: number | null;
  account_name: string | null;
  started_at: string | null;
}

/** `POST /plugin/v1/heartbeat` 与 `POST /plugin/v1/identity` 的返回（同一形状，规格 §2.4）。 */
export interface HeartbeatOut {
  device: { id: number | string; name: string | null };
  login: LoginInfo;
  account: AccountConfig | null;
  device_run: DeviceRunInfo | null;
  server_time: string | null;
  bj_date: string | null;
  simulation_only: boolean;
}

/** `POST /plugin/v1/runs` 的返回 `{run_id, lease_ttl_s, lease_expires_at, account: {id, name}}`。 */
export interface RunStartOut {
  run_id: number;
  lease_expires_at: string | null;
  /** 租约有效期（秒，服务端 registry 的 lease_ttl_s）；没给时为 null，插件按契约默认 180 秒。 */
  lease_ttl_s: number | null;
  /** 这个批次的账号（服务端按请求里的买家号 ID 找到的）；没给时为 null。 */
  account: { id: number; name: string | null } | null;
}

/** `POST /plugin/v1/runs/{id}/heartbeat` 的返回。 */
export interface RunHeartbeatOut {
  lease_expires_at: string | null;
  stop: boolean;
  stop_reason: string | null;
}

/** 领到的一笔采购尝试（契约 §5.6）。收件信息的形状契约只写了「收件信息」，
 *  插件平铺与嵌套都认（见 simulate.attemptPostalCode）。 */
export interface Attempt {
  attempt_id: number;
  upstream_order_no: string | null;
  asin: string | null;
  quantity: number | null;
  unit_price: string | null;
  price_cap: string | null;
  max_delivery_days: number | null;
  amount_range: { min: string | null; max: string | null } | null;
  expected_card_last4: string | null;
  simulated: boolean;
  /** 原样保留服务端下发的其余字段（收件信息等）。 */
  raw: Record<string, unknown>;
}

/** `POST /plugin/v1/runs/{id}/next` 的返回。 */
export interface NextOut {
  attempt: Attempt | null;
  reason: string | null;
  /** 候选任务只是暂时被别的操作锁着：这么多秒后再领，不要结束批次。没给时为 null。 */
  retry_after_s: number | null;
}

/** `POST /plugin/v1/attempts/{id}/progress` 的返回。 */
export interface ProgressOut {
  continue: boolean;
  cancel_reason: string | null;
  /** continue=false 的机读原因（UPSTREAM_CANCELLED、ACCOUNT_PAUSED、IDENTITY_MISMATCH……）。
   *  服务端没给时为 null —— 插件据此决定上报哪个中止码（见 codes.abortCodeFor）。 */
  cancel_code: string | null;
}

/** 结算页事实（契约 §4）。模拟执行时由 simulate.buildCheckoutFacts 推出来。 */
export interface CheckoutFacts {
  items: Array<{ asin: string | null; quantity: number | null; unit_price: string | null }>;
  subtotal: string | null;
  shipping: string | null;
  tax: string | null;
  total_before_tax: string | null;
  total: string | null;
  gift_card_amount: string | null;
  currency: string;
  delivery: { text: string | null; date: string | null; days: number | null };
  ship_to: { postal_code: string | null };
  card_last4: string | null;
  fulfillment: string | null;
  /** 取事实那一刻页面上读到的买家号 ID（模拟执行时取当前页面的读数）。读不到为 null：
   *  服务端护栏的 buyer 项按数据不可靠拦截，不当成「对得上」。 */
  customer_id: string | null;
}

/** `POST /plugin/v1/attempts/{id}/guard` 的返回。 */
export interface GuardOut {
  verdict: "pass" | "block";
  code: string | null;
  reasons: string[];
  checks: unknown;
}

/** `POST /plugin/v1/attempts/{id}/result` 的请求体（契约 §5.11）。 */
export interface ResultIn {
  outcome: "succeeded" | "failed" | "unknown";
  amazon_order_no: string | null;
  amounts: {
    subtotal: string | null;
    shipping: string | null;
    tax: string | null;
    total_before_tax: string | null;
    total: string | null;
  } | null;
  card_last4: string | null;
  delivery: { text: string | null; date: string | null; days: number | null } | null;
  error_code: string | null;
  error_detail: string | null;
}

/** `ACCOUNT_BUSY` 的 detail：这个买家号（账号）正在哪个浏览器里执行。 */
export interface BusyHolder {
  holder_device_id: number | null;
  holder_device_name: string | null;
  since: string | null;
}

/** `DEVICE_BUSY` 的 detail：这个浏览器自己还没结束的批次。 */
export interface DeviceBusy {
  run_id: number | null;
  account_name: string | null;
  since: string | null;
  lease_expires_at: string | null;
}

/** 开始批次被 `IDENTITY_MISMATCH` 拒绝时的 detail。login_state 是设备登录状态
 *  （unknown / conflict / signed_out / no_id / signed_in，不含解析结果）。 */
export interface IdentityRejection {
  login_state: string | null;
  identity_at: string | null;
  stale: boolean;
}
