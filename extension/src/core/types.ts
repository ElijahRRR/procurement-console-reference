/** 插件 ↔ 服务端的协议类型（开发契约 v2 §4、§5、§10）。插件这一侧只有这一份。
 *
 * 约定（契约 §10）：金额是两位小数的字符串（`"52.00"`），可空；日期 `YYYY-MM-DD`；
 * 时刻 ISO 8601 带时区；字段缺失与 `null` 等价。**null 不是 0**。
 */

export type Browser = "chrome" | "adspower" | "other";
export type Trigger = "manual" | "scheduled";
export type IdentityStatus = "match" | "mismatch" | "signed_out" | "unknown";

/** 身份上报（契约 §5.3）。 */
export interface Identity {
  signed_in: boolean;
  customer_id: string | null;
  site: "US";
  /** 只有 origin + pathname，不带查询串与片段。 */
  page_url: string | null;
}

/** `POST /plugin/v1/pair` 的返回。 */
export interface PairOut {
  device_id: number | string;
  device_token: string;
  workspace_name: string | null;
}

export interface QuotaToday {
  limit: number | null;
  used: number | null;
  planned: number | null;
  available: number | null;
}

/** 心跳里绑定账号的有效配置（契约 §10「插件心跳 / 配置」）。 */
export interface AccountConfig {
  id: number;
  name: string;
  amazon_customer_id: string | null;
  purchase_mode: string | null;
  schedule_times: string[];
  effective_max_delivery_days: number | null;
  purchase_enabled: boolean | null;
  execution_paused: boolean | null;
  pause_reason: string | null;
  min_amount: string | null;
  max_amount: string | null;
  daily_limit: number | null;
  expected_card_last4: string | null;
  quota_today: QuotaToday | null;
}

export interface ActiveRunInfo {
  id: number;
  device_id: number | null;
  device_name: string | null;
  is_mine: boolean;
}

/** `POST /plugin/v1/heartbeat` 的返回。 */
export interface HeartbeatOut {
  device: { id: number | string; name: string | null; bound_account_id: number | null };
  account: AccountConfig | null;
  identity_status: IdentityStatus;
  server_time: string | null;
  bj_date: string | null;
  simulation_only: boolean;
  active_run: ActiveRunInfo | null;
}

/** `POST /plugin/v1/runs` 的返回 `{run_id, lease_ttl_s, lease_expires_at}`（形状见 client.parseRunStart）。 */
export interface RunStartOut {
  run_id: number;
  lease_expires_at: string | null;
  /** 租约有效期（秒，服务端 registry 的 lease_ttl_s）；没给时为 null，插件按契约默认 180 秒。 */
  lease_ttl_s: number | null;
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

/** `ACCOUNT_BUSY` 的 detail（契约 §10 末尾）。 */
export interface BusyHolder {
  holder_device_id: number | null;
  holder_device_name: string | null;
  since: string | null;
}
