/** 内存假服务端：按执行入口改造规格 v3（§2.2 – §2.5、§2.8）与开发契约 v2 §4（护栏）、§5（执行协议），
 *  对照 backend/services/devices.py、execution.py 实现插件接口，供引擎、客户端与后台测试用。
 *
 * 只实现插件这一侧会碰到的规则，但**边角路径照真实服务端的样子回**（这些地方模型一偏，引擎测试就会
 * 「过了，真跑不对」）：
 *   · 插件入口闸（gate：{code, message} → 403，在认证之前，例如非本机对端、网页来源）；
 *   · 设备登记 `POST /register {device_token, ext_version?, workspace_id?}`（不要令牌头）：令牌形状不对 → 422；
 *     同一个令牌再登记 → 返回那一行、created=false（幂等）；没有工作区 → 409 CONFLICT；多个工作区且没带
 *     存在的 workspace_id → 409 WORKSPACE_REQUIRED；工作区里还在用的浏览器到上限（maxDevices；移除了的不算）→
 *     429 RATE_LIMITED「登记的浏览器太多：请在控制台「账号与浏览器」里移除不用的浏览器，或调大 PC_MAX_DEVICES」
 *     （真实服务端 devices.MAX_DEVICES_MESSAGE；registerRateLimit 另可注入 detail.retry_after_s 与 Retry-After 头，
 *     测插件的退避）；名称「浏览器 <id>」；
 *   · 版本闸：登记、心跳、身份上报带来的 ext_version 低于 0.2.0 → 409 PLUGIN_OUTDATED（什么也不记）；
 *   · 其余接口带 Bearer 令牌，未知令牌 → 401 UNAUTHORIZED（forgetDevices() 模拟服务端换了库）；
 *   · 身份上报（心跳捎带或单独 `POST /identity`，返回同一形状，都清掉「已移除」）：清洗（customer_id 大写、
 *     形状不对当没读到，绝不报错）；设备登录状态 unknown / conflict / signed_out / no_id / signed_in；signed_in 时
 *     按买家号 ID 解析成 ready（未归档账号登记了它）/ ignored / unregistered；stale = 超过 identity_fresh_s
 *     （默认 600 秒），**从没上报过的不算过期**（真实服务端 devices.identity_stale）；
 *   · 心跳形状 {device, login, account, device_run, server_time, bj_date, simulation_only}；login.customer_id 只在
 *     登录状态是 signed_in 时有（真实服务端 login_customer_id：有分歧、没登录、没读到 ID 时为 null）；account 只在
 *     ready 时有，含 quota_today {limit, used, planned, available = max(0, limit − used − planned)}、executable_today、
 *     future_assigned、active_run；
 *   · 开始批次 `POST /runs {customer_id, trigger, slot?, simulated}`，依次（真实服务端 execution.start_run）：
 *     trigger 不是 manual / scheduled → 422（缺省 manual）；simulated 不是布尔 → 422「simulated 必须是 true 或 false」
 *     （在 DEVICE_BUSY 之前）；缺 customer_id → 422「插件版本过旧，请更新插件」；形状不对 → 422；设备已有未结束
 *     批次 → 409 DEVICE_BUSY（detail {run_id, account_name, since, lease_expires_at}）；登录状态不是 signed_in、
 *     ID 与请求不同、过期 → 403 IDENTITY_MISMATCH（detail {login_state, identity_at, stale}）；没有登记这个 ID
 *     的账号 → 403 ACCOUNT_NOT_REGISTERED；暂停 → 403 ACCOUNT_PAUSED（detail {pause_reason, paused_at}）；
 *     非模拟 → 403 REAL_PURCHASE_DISABLED；定时：没带 slot、slot 写法不对（两句不同的原话）、账号不是定时模式
 *     （detail.purchase_mode）、slot 不在执行时间里（原话带上执行时间，detail.schedule_times）、北京时间还没到点
 *     → 422（真实服务端 _norm_slot / _check_slot）；同一（账号、北京日期、slot）
 *     已有**以 finished 结束**的定时批次 → 409 SLOT_DONE；账号已有未结束批次（包括同一个时间点**还在执行**的
 *     定时批次）→ 409 ACCOUNT_BUSY（带持有者，插件下一分钟再问）；那个批次没跑完就结束（租约过期、buyer 项拦截、
 *     清除模拟结果）时时间点重新空出来，再问就能开始；
 *     返回 {run_id, lease_ttl_s, lease_expires_at, account: {id, name}}，租约按 lease_ttl_s 过期（到期那一刻起就算
 *     过期），只有批次心跳续租；
 *   · 执行中的身份检查（领取、进度、提交登记）：这台设备最新的登录状态是 signed_in、买家号 ID 等于批次账号
 *     **当前**登记的 ID、且不过期；批次心跳 stop_reason 取 account_paused / account_archived / identity_mismatch；
 *   · 领取不到时的原因照真实服务端 next_attempt 的次序：暂停、归档、买家号不符、身份过期、当天额度已用完
 *     （「账号 N 今天（日期）的额度已用完：已占用 U / 上限 L」；占用 = 当天这个账号没有失败或中止的尝试，
 *     account.used_today 可预置），账号在别的批次还有在途尝试（「账号还有一笔进行中的采购尝试（#N），等它结束后
 *     再领取」）；
 *   · 领取（同一批次已有在途尝试 → 409 ATTEMPT_IN_FLIGHT）。领取时复核来源指定：任务上来源指定的买家号已经不是
 *     这个账号（task.off_target，真实服务端 designation.off_target）→ 解除分配回待分配、跳过这一笔，领不到任务时
 *     原因里带「N 笔来源指定的买家号已不是本账号、已解除分配」。插件不看分配方式，只把原因写进日志；
 *   · 进度：{continue, cancel_reason, cancel_code}。尝试已结束 → ATTEMPT_FINISHED；未登记提交时依次看
 *     上游取消（UPSTREAM_CANCELLED）、账号归档（ACCOUNT_ARCHIVED）、暂停（ACCOUNT_PAUSED）、买家号不符
 *     （IDENTITY_MISMATCH）、登录状态过期（IDENTITY_STALE）、来源暂停（task.hold → SOURCE_HOLD）、领取后来源有变化
 *     （task.drift → SOURCE_CHANGED）。legacy: true 模拟旧服务端：不给 cancel_code、不认 ABORTED_BY_SERVER；
 *   · 护栏：裁决 {verdict, code, reasons, checks: {检查名: {ok, expected, actual}}, delivery_days, total_for_cap,
 *     fulfillment}（真实服务端 guard.evaluate 的形状与次序）。放行 → guard_passed；一般的拦截 → 尝试**结束为
 *     failed**、任务转异常，批次继续下一笔；对已结束的尝试再判 → 409 detail.attempt_status。buyer 项：事实里没有
 *     customer_id → DATA_UNRELIABLE（一般的拦截）；与批次账号登记的 ID 不同 → IDENTITY_MISMATCH（原因「结算页登录的
 *     买家号（…）不是这个批次的买家号（…）」）；账号没有登记 ID → IDENTITY_MISMATCH「这个批次的账号没有登记买家号 ID，
 *     不能放行」；
 *   · **护栏 buyer 项拦截（IDENTITY_MISMATCH）的落地是中止类，而且服务端自己把批次结束**（真实服务端
 *     execution._buyer_blocked，护栏判定与提交登记复核两处相同）：尝试 aborted（error_code = IDENTITY_MISMATCH、
 *     未登记提交）、任务回已分配（上游已取消 → cancelled；领取后的来源变化照样落到任务上）、不进异常、不暂停账号、
 *     批次统计记 aborted、批次 end_reason = identity_mismatch。`/guard` 处照旧回 200 的拦截裁决（形状不变）；
 *     提交登记复核处回 409 GUARD_BLOCKED，detail {block_code, reasons, attempt_status: aborted, order_status,
 *     run_ended: true, end_reason: identity_mismatch}。之后这个批次的领取、批次心跳、结束批次，以及这笔尝试的
 *     进度、护栏、提交登记、等待验证一律 409 LEASE_LOST「执行批次已结束（结算页登录的买家号不是这个批次的买家号），
 *     请重新开始批次」，detail {run_id, end_reason}；这笔尝试再报结果：报的正是 IDENTITY_MISMATCH 的失败 →
 *     200 duplicate，别的 → 409 CONFLICT；设备心跳的 device_run、account.active_run 为 null；重新开始批次马上可以，
 *     新批次会再领到这一笔。被它结束的定时批次不占时间点（只有 finished 才占）；
 *   · 提交登记：不在 guard_passed → 409（detail.attempt_status，已登记的是 submitting）、任务已取消 → 409、
 *     任务已不在执行这一笔（控制台改了它）→ 409「任务状态已变化，不能提交」（detail.order_status）、
 *     账号归档 → 409、暂停 → 409 ACCOUNT_PAUSED、买家号不符或过期 → 409 IDENTITY_MISMATCH（detail.login_state /
 *     stale）、来源暂停或有变化 → 409 SOURCE_CHANGED（detail.fields）、按最新配置对存下的结算事实复核护栏不过 →
 *     409 GUARD_BLOCKED（detail {block_code, reasons}，什么都没改；buyer 项除外，见上一条）；
 *   · 结果处理表（账号级错误暂停账号、PAGE_LOAD_FAILED 首次重试、登记后非成功 → unknown、
 *     中止类（含 ABORTED_BY_SERVER、IDENTITY_MISMATCH）回已分配、**不暂停账号**，再按领取后的来源变化重新定状态
 *     （task.drift.to），未知错误码按任务级失败）；已结束的尝试再报同一份 → 200 duplicate；
 *     已是 unknown 而这次不同、或已由控制台核实（resolve(attemptId, "ordered" | "not_ordered")）而这次不同 →
 *     409 detail.late_receipt_recorded（回执记下，不改结论）；其他不同 → 409；
 *     未登记提交、批次已失效 → LEASE_LOST；**结果上报、结束批次不做身份检查**；
 *   · 结束批次 {reason, detail?} → {run_id, ended_at, end_reason, stats}：end_reason 是封闭取值（finished /
 *     lease_expired / simulation_reset / identity_mismatch），插件结束的恒为 finished，插件给的短码记为
 *     plugin_reason；detail 去掉首尾空白截到 200 字（非字符串先转成 JSON 文本）记为 finish_detail；
 *     在途尝试按租约过期处理（已登记 → unknown + 暂停账号，否则 aborted）；
 *   · 批次失效的答复（409 LEASE_LOST）：已结束 →「执行批次已结束（<结束原因的中文>），请重新开始批次」，
 *     detail {run_id, end_reason}；租约过期 →「执行权已超时（租约过期），请重新开始批次」，
 *     detail {run_id, lease_expires_at}；
 *   · 批次统计 stats：claimed / succeeded / failed / blocked / unknown / aborted（真实服务端 _bump）。
 *
 * 测试用的几个直接操作：setLogin（这台设备刚上报了这一份身份）、forgetDevices（服务端换了库）、sweep（租约清扫）、
 * resolve（控制台核实一笔结果待核实的尝试）。
 *
 * 为了让引擎、客户端测试不用先走一遍登记：缺省预先登记一个浏览器（令牌 DEMO_TOKEN，登着账号的买家号、
 * 刚上报过身份）。后台测试传 preRegistered: false，让真正的后台自己生成令牌、自己登记。
 *
 * 故障注入（faults：{match: 路径正则, times}）：
 *   · 默认：请求没到服务端就断了（network）；
 *   · after: true：请求到了服务端、处理了，响应丢了（network）；
 *   · resend: true：请求到了、处理了、响应丢了，**浏览器把同一个 POST 原样重发**，返回第二次的答复；
 *   · status: 500：服务端回 5xx（事务回滚，什么都没改）。
 *
 * 所有数据都是虚构的演示数据（买家号 ID 用 A1DEMO…、A9OTHER… 这类明显的假值）。
 */

const json = (status, body, headers = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
const err = (status, code, message, detail = null, headers = {}) => json(status, { code, message, detail: detail ?? {} }, headers);

/** 预先登记的那个浏览器的令牌（形状合格的假值）。 */
export const DEMO_TOKEN = "demo-token-000000000000000000000000001";
/** 工作区里还在用的浏览器到上限时，新登记得到的 429 说明（真实服务端 devices.MAX_DEVICES_MESSAGE）。 */
export const MAX_DEVICES_MESSAGE = "登记的浏览器太多：请在控制台「账号与浏览器」里移除不用的浏览器，或调大 PC_MAX_DEVICES";
/** 演示账号登记的买家号 ID、另一个买家号 ID（都是假值）。 */
export const DEMO_CID = "A1DEMO00000";
export const OTHER_CID = "A9OTHER00000";

const TOKEN_RE = /^[A-Za-z0-9_-]{32,128}$/;
const CUSTOMER_ID_RE = /^A[0-9A-Z]{8,}$/;
const MIN_EXT_VERSION = [0, 2, 0];

/** 版本闸的 409（真实服务端 devices.check_ext_version）。 */
const outdatedErr = (v) =>
  err(409, "PLUGIN_OUTDATED", "插件版本过旧：请重新构建并加载新版插件（需要 0.2.0 及以上）", { ext_version: String(v).trim(), min_ext_version: "0.2.0" });

/** 输入：版本文本 → 输出：能解析且低于 0.2.0。缺失或解析不了不拦。 */
function outdated(v) {
  if (typeof v !== "string" || !/^\d+(\.\d+)*$/.test(v.trim())) return false;
  const p = v.trim().split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    const d = (p[i] ?? 0) - MIN_EXT_VERSION[i];
    if (d !== 0) return d < 0;
  }
  return false;
}

function bjDate(ms) {
  return new Date(ms + 8 * 3600_000).toISOString().slice(0, 10);
}
function bjHm(ms) {
  return new Date(ms + 8 * 3600_000).toISOString().slice(11, 16);
}

/** 输入：结束批次的 detail → 输出：真实服务端 execution._plugin_detail 记下的文本（去空白、截 200 字）。 */
function pluginDetail(v) {
  if (v === null || v === undefined) return null;
  const t = (typeof v === "string" ? v : JSON.stringify(v)).trim().slice(0, 200);
  return t || null;
}

function cents(v) {
  if (v === null || v === undefined) return null;
  const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(String(v).trim());
  if (!m) return null;
  const c = Number(m[2]) * 100 + Number((m[3] ?? "").padEnd(2, "0"));
  return m[1] === "-" ? -c : c;
}

function laDate(ms) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit" });
  const p = Object.fromEntries(f.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

/** 输入：整数分 → 输出：两位小数的金额文本；null 原样。 */
function money2(c) {
  if (c === null || c === undefined) return null;
  const abs = Math.abs(c);
  return `${c < 0 ? "-" : ""}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/** 输入：上报的买家号 ID → 输出：去空白、大写后形状合格的 ID；缺失、类型不对、形状不对 → null（真实服务端 guard._buyer_id）。 */
function buyerId(v) {
  if (typeof v !== "string") return null;
  const s = v.replace(/\s+/g, "").toUpperCase();
  return s.length <= 64 && CUSTOMER_ID_RE.test(s) ? s : null;
}

function daysBetween(a, b) {
  return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400000);
}

/** 服务端 services/models.ERROR_CODES 的类别（插件会碰到的那些）。 */
const CATEGORY = {
  OUT_OF_STOCK: "task", PAGE_LOAD_FAILED: "retry_once", LOGIN_REQUIRED: "account", ACCOUNT_RESTRICTED: "account",
  CARD_DECLINED: "account", CAPTCHA_ENCOUNTERED: "account", IDENTITY_MISMATCH: "abort",
  ABORTED_UPSTREAM_CANCELLED: "abort", ABORTED_BY_USER: "abort", ABORTED_BY_SERVER: "abort",
  ITEM_MISMATCH: "guard", ZIP_MISMATCH: "guard", SUBTOTAL_OUT_OF_RANGE: "guard", PRICE_OVER_CAP: "guard",
  DELIVERY_TOO_LONG: "guard", DATA_UNRELIABLE: "guard", CARD_MISMATCH: "guard", GUARD_BLOCKED: "guard",
  SUBMIT_REJECTED: "task", PLUGIN_SIMULATION_ONLY: "account",
};
/** 账号级错误码的中文（真实服务端暂停账号时用 ERROR_CODES 的 zh 作暂停原因）。 */
const ACCOUNT_ZH = {
  LOGIN_REQUIRED: "买家账号登录失效", ACCOUNT_RESTRICTED: "买家账号受限", CARD_DECLINED: "银行卡被拒付",
  CAPTCHA_ENCOUNTERED: "遇到验证码或风控验证，需要人工处理",
  PLUGIN_SIMULATION_ONLY: "插件只执行模拟任务，却领到了真实任务",
};
const IN_FLIGHT = new Set(["claimed", "checking", "guard_passed", "submitting", "verifying"]);
const PRE_SUBMIT = new Set(["claimed", "checking", "guard_passed"]);
const FINAL = new Set(["succeeded", "failed", "unknown", "aborted"]);
/** 批次结束原因的中文（真实服务端 execution.END_REASON_ZH）。 */
export const END_REASON_ZH = {
  finished: "插件结束了批次", lease_expired: "执行权超时（租约过期）",
  identity_mismatch: "结算页登录的买家号不是这个批次的买家号", device_revoked: "浏览器的授权已撤销（旧版）",
  simulation_reset: "已清除模拟结果",
};
/** 护栏 buyer 项的拦截码，以及它拦下时服务端结束批次所写的 end_reason。 */
const BUYER_BLOCK_CODE = "IDENTITY_MISMATCH";
const BUYER_BLOCK_END_REASON = "identity_mismatch";
/** 护栏检查的次序与各自的拦截码（真实服务端 guard.CHECK_ORDER）。 */
const CHECK_ORDER = [
  ["buyer", "IDENTITY_MISMATCH"], ["item", "ITEM_MISMATCH"], ["zip", "ZIP_MISMATCH"], ["subtotal", "SUBTOTAL_OUT_OF_RANGE"],
  ["price_cap", "PRICE_OVER_CAP"], ["delivery", "DELIVERY_TOO_LONG"], ["data", "DATA_UNRELIABLE"], ["card", "CARD_MISMATCH"],
];
/** 上报结局的中文（真实服务端 execution._OUTCOME_ZH）。 */
const OUTCOME_ZH = { succeeded: "成功", failed: "失败", unknown: "结果不明" };
const STATUS_ZH = { claimed: "已领取", checking: "结算中", guard_passed: "护栏已放行", submitting: "已登记提交", verifying: "等待验证", succeeded: "成功", failed: "失败", unknown: "结果待核实", aborted: "已中止" };

/** 输入：初始数据与选项 → 输出：{ fetch, state, calls, count(path 片段), now, setNow }。 */
export function createFakeServer(opts = {}) {
  let clock = opts.now ?? Date.parse("2026-11-05T06:00:00Z");
  const leaseTtlS = opts.leaseTtlS ?? 180;
  // 旧服务端（legacy）：进度不给 cancel_code，也不认 ABORTED_BY_SERVER（按未知码 → 任务级失败）。
  const legacy = opts.legacy === true;
  const cancelCodes = !legacy;
  const categories = { ...CATEGORY };
  if (legacy) delete categories.ABORTED_BY_SERVER;
  const identityFreshS = opts.identityFreshS ?? 600;
  const account0 = {
    id: 3,
    name: "环境-DEMO",
    amazon_customer_id: DEMO_CID,
    min_amount: "20.00",
    max_amount: "50.00",
    daily_limit: 20,
    max_delivery_days: 10,
    expected_card_last4: null,
    purchase_enabled: true,
    execution_paused: false,
    pause_reason: null,
    archived: false,
    purchase_mode: "scheduled",
    schedule_times: ["14:00"],
    ...(opts.account ?? {}),
  };
  const state = {
    workspaces: opts.workspaces ?? [{ id: 1, name: "工作区-DEMO" }],
    /** 令牌 → 设备。真实服务端只存哈希；这里直接拿令牌当键。 */
    tokens: new Map(),
    devices: [],
    nextDeviceId: 5,
    /** 第一个账号也叫 state.account（多数测试只有一个账号）。 */
    account: account0,
    accounts: [account0, ...(opts.accounts ?? [])],
    ignored: new Set(opts.ignored ?? []),
    /** 所有设备的身份上报记录（心跳捎带或单独上报）。 */
    identityReports: [],
    registrations: [],
    tasks: (opts.tasks ?? []).map((t, i) => ({
      id: i + 1,
      status: "assigned",
      cancelled: false,
      page_load_failures: 0,
      postal_code: "32960",
      quantity: 1,
      account_id: account0.id,
      ...t,
    })),
    runs: new Map(),
    attempts: new Map(),
    lateReceipts: [],
    nextRunId: 9,
    nextAttemptId: 7,
    /** 别的浏览器正占着这个账号时设置：{device_id, device_name, since}。 */
    foreignRun: opts.foreignRun ?? null,
    /** 最近一个登记或认证过的设备（多数测试只有一个浏览器）。 */
    device: null,
  };
  /** 输入：令牌、工作区 → 输出：新登记的设备行。 */
  function addDevice(token, ws) {
    const id = state.nextDeviceId++;
    const dev = { id, name: `浏览器 ${id}`, workspace_id: ws.id, workspace_name: ws.name, identity: null, identityAt: null, hidden: false, ext_version: null };
    state.tokens.set(token, dev);
    state.devices.push(dev);
    state.device = dev;
    return dev;
  }
  if (opts.preRegistered !== false) {
    const dev = addDevice(opts.token ?? DEMO_TOKEN, state.workspaces[0] ?? { id: 1, name: "工作区-DEMO" });
    // 缺省：登着演示账号的买家号、刚上报过身份（引擎测试不用先报一遍）。identity: null = 从没上报过。
    dev.identity = opts.identity === undefined ? { signed_in: true, customer_id: account0.amazon_customer_id, site: "US", page_url: null, conflict: false } : opts.identity;
    dev.identityAt = dev.identity === null ? null : opts.identityAt === undefined ? clock : opts.identityAt;
  }
  const calls = [];
  /** 故障注入：见文件头。 */
  const faults = [...(opts.faults ?? [])];
  /** 每次请求后推进时钟（毫秒），用来驱动引擎的「到点补心跳」与租约过期。 */
  const tickMs = opts.tickMs ?? 0;
  const hooks = opts.hooks ?? {};

  const runOf = (id) => state.runs.get(Number(id));
  const attemptOf = (id) => state.attempts.get(Number(id));
  const taskOf = (a) => state.tasks.find((t) => t.id === a.task_id);
  // 真实服务端 _require_active：lease_expires_at <= now 即过期（到期那一刻起不再有效）
  const runActive = (run) => run && !run.ended && clock < run.lease_until;
  const iso = (ms) => (ms === null || ms === undefined ? null : new Date(ms).toISOString());
  /** 批次失效的答复（真实服务端 _require_active）：已结束的带结束原因，租约过期的带过期时刻。 */
  const leaseLost = (run) =>
    run && run.ended
      ? err(409, "LEASE_LOST", `执行批次已结束（${END_REASON_ZH[run.end_reason ?? ""] ?? run.end_reason ?? "已结束"}），请重新开始批次`,
          { run_id: run.id, end_reason: run.end_reason ?? null })
      : err(409, "LEASE_LOST", "执行权已超时（租约过期），请重新开始批次", { run_id: run?.id ?? null, lease_expires_at: iso(run?.lease_until) });
  const accountOf = (run) => state.accounts.find((x) => x.id === run.account_id) ?? state.account;
  const deviceOf = (run) => state.devices.find((d) => d.id === run.device_id) ?? state.device;
  /** 身份清洗（真实服务端 devices.clean_identity）：坏 ID 当没读到，绝不报错；page_url 只留 origin + path。 */
  function cleanIdentity(v) {
    const o = v && typeof v === "object" ? v : {};
    const cid = typeof o.customer_id === "string" ? o.customer_id.replace(/\s+/g, "").toUpperCase() : "";
    let page = null;
    try { const u = new URL(String(o.page_url ?? "")); page = u.origin + u.pathname; } catch { page = null; }
    return { signed_in: o.signed_in === true, customer_id: CUSTOMER_ID_RE.test(cid) ? cid : null, site: "US", page_url: page, conflict: o.conflict === true };
  }
  /** 设备登录状态（不含解析结果）：unknown / conflict / signed_out / no_id / signed_in。 */
  function loginState(dev) {
    const i = dev.identity;
    if (!i) return "unknown";
    if (i.conflict) return "conflict";
    if (!i.signed_in) return "signed_out";
    return i.customer_id ? "signed_in" : "no_id";
  }
  const loginCustomerId = (dev) => (loginState(dev) === "signed_in" ? dev.identity.customer_id : null);
  const registered = (cid) => state.accounts.find((a) => !a.archived && a.amazon_customer_id === cid) ?? null;
  /** 解析：signed_in 时按买家号 ID 找账号 → ready / ignored / unregistered；其余状态原样。 */
  function resolveLogin(dev) {
    const st = loginState(dev);
    if (st !== "signed_in") return { state: st, account: null };
    const acc = registered(dev.identity.customer_id);
    if (acc) return { state: "ready", account: acc };
    return { state: state.ignored.has(dev.identity.customer_id) ? "ignored" : "unregistered", account: null };
  }
  /** 真实服务端 devices.identity_stale：从没上报过（identity_at 为空）不算过期，那是登录状态 unknown 的事。 */
  const identityStale = (dev) => dev.identityAt !== null && clock - dev.identityAt > identityFreshS * 1000;
  const STALE_TEXT = `插件超过 ${Math.max(1, Math.floor(identityFreshS / 60))} 分钟没有上报登录状态`;
  /** 登录状态不是 signed_in 时的说明（真实服务端 execution._LOGIN_PROBLEM_ZH，开始批次与执行中共用）。 */
  const LOGIN_PROBLEM = {
    unknown: "插件还没有上报这个浏览器的登录状态，请打开或刷新亚马逊页面后重试",
    conflict: "这个浏览器的亚马逊标签页显示的买家号不一致，请刷新或关闭较早打开的标签页",
    signed_out: "浏览器没有登录亚马逊",
    no_id: "插件没有读到浏览器登录的买家号 ID，请打开亚马逊订单页后重试",
  };
  /** 执行中的身份检查（真实服务端 _identity_problem）：这台设备登着的不是批次账号**当前**登记的买家号时的原话。 */
  function identityProblem(dev, acc) {
    const st = loginState(dev);
    if (st !== "signed_in") return LOGIN_PROBLEM[st];
    if (!acc.amazon_customer_id) return `账号 ${acc.name} 现在没有登记买家号 ID`;
    if (dev.identity.customer_id !== acc.amazon_customer_id) {
      return `浏览器登录的买家号变了（现在是 ${dev.identity.customer_id}，这个批次的账号 ${acc.name} 登记的是 ${acc.amazon_customer_id}）`;
    }
    return null;
  }
  /** 当天已占用的额度（真实服务端 quota.used：这个账号今天没有失败、中止的尝试；account.used_today 是预置的基数）。 */
  const usedToday = (acc) =>
    (acc.used_today ?? 0) + [...state.attempts.values()].filter((a) => a.account_id === acc.id && a.bj_date === bjDate(clock) && a.status !== "failed" && a.status !== "aborted").length;
  function storeIdentity(dev, raw, via) {
    dev.identity = cleanIdentity(raw);
    dev.identityAt = clock;
    state.identityReports.push({ via, at: clock, device_id: dev.id, ...dev.identity });
  }
  const runOfDevice = (dev) => [...state.runs.values()].find((r) => !r.ended && r.device_id === dev.id) ?? null;
  const runOfAccount = (acc) => [...state.runs.values()].find((r) => !r.ended && r.account_id === acc.id) ?? null;
  /** 心跳与身份上报的返回（同一形状，规格 §2.4）。 */
  function statusOut(dev) {
    const { state: st, account: acc } = resolveLogin(dev);
    const mine = runOfDevice(dev);
    const tasks = acc ? state.tasks.filter((t) => t.account_id === acc.id && t.status === "assigned" && !t.cancelled) : [];
    const holder = acc ? runOfAccount(acc) : null;
    const foreign = acc && acc.id === state.account.id ? state.foreignRun : null;
    return {
      device: { id: dev.id, name: dev.name },
      login: { state: st, customer_id: loginCustomerId(dev), at: iso(dev.identityAt), stale: identityStale(dev) },
      account: acc ? {
        id: acc.id, name: acc.name, amazon_customer_id: acc.amazon_customer_id, purchase_enabled: acc.purchase_enabled !== false,
        execution_paused: acc.execution_paused, pause_reason: acc.pause_reason, purchase_mode: acc.purchase_mode,
        schedule_times: acc.schedule_times, effective_max_delivery_days: acc.max_delivery_days,
        quota_today: { limit: acc.daily_limit, used: usedToday(acc), planned: tasks.length, available: Math.max(0, acc.daily_limit - usedToday(acc) - tasks.length) },
        executable_today: tasks.filter((t) => !t.future && !t.hold).length,
        future_assigned: tasks.filter((t) => t.future).length,
        active_run: holder
          ? { id: holder.id, device_id: holder.device_id, device_name: deviceOf(holder).name, started_at: iso(holder.started_at), is_mine: holder.device_id === dev.id }
          : foreign
            ? { id: 900, device_id: foreign.device_id, device_name: foreign.device_name, started_at: foreign.since, is_mine: false }
            : null,
      } : null,
      device_run: mine ? { id: mine.id, account_id: mine.account_id, account_name: accountOf(mine).name, started_at: iso(mine.started_at) } : null,
      server_time: new Date(clock).toISOString(),
      bj_date: bjDate(clock),
      simulation_only: true,
    };
  }
  /** 未登记提交就结束的尝试：任务回已分配（上游已取消 → cancelled），再按领取后的来源变化重新定状态
   *  （drift.to，例如待核实）；来源把任务暂停着（hold）→ conflict，不再领取。 */
  const release = (t) => {
    if (t.cancelled) { t.status = "cancelled"; return; }
    t.status = t.drift?.to ?? (t.hold ? "conflict" : "assigned");
    t.drift = null;
  };
  const stopReason = (run) => {
    const acc = accountOf(run);
    if (acc.archived) return "account_archived";
    if (acc.execution_paused) return "account_paused";
    if (identityProblem(deviceOf(run), acc)) return "identity_mismatch";
    return null;
  };

  /** 输入：尝试、结算页事实 → 输出：护栏裁决（真实服务端 guard.evaluate 的形状与次序），按任务与账号**当前**的值判。
   *  checks 里 ok 为 true（通过）/ false（不通过）/ null（缺数据无法判定，由 data 检查拦截）；code 是按 CHECK_ORDER
   *  第一个不通过的检查的拦截码。 */
  function guardEval(a, facts) {
    const t = taskOf(a);
    const acc = accountOf(runOf(a.run_id));
    const f = facts && typeof facts === "object" ? facts : {};
    const checks = {};
    const reasons = {};
    const problems = [];
    // buyer：把「谁在下单」钉在下单那一页。没读到 → 数据不可靠；不是批次账号登记的 ID（或账号没登记 ID）→ 拦截。
    const expBuyer = buyerId(acc.amazon_customer_id);
    const actBuyer = buyerId(f.customer_id);
    if (actBuyer === null) {
      checks.buyer = { ok: null, expected: expBuyer, actual: null };
      problems.push("结算页没有读到买家号 ID");
    } else if (expBuyer === null) {
      checks.buyer = { ok: false, expected: null, actual: actBuyer };
      reasons.buyer = "这个批次的账号没有登记买家号 ID，不能放行";
    } else {
      checks.buyer = { ok: actBuyer === expBuyer, expected: expBuyer, actual: actBuyer };
      if (actBuyer !== expBuyer) reasons.buyer = `结算页登录的买家号（${actBuyer}）不是这个批次的买家号（${expBuyer}）`;
    }
    const items = Array.isArray(f.items) ? f.items : [];
    const itemOk = items.length === 1 && items[0].asin === t.asin && items[0].quantity === t.quantity;
    checks.item = { ok: itemOk, expected: { asin: t.asin, quantity: t.quantity }, actual: items.length === 1 ? items[0] : items };
    if (!itemOk) reasons.item = "商品或数量不符";
    const zip = String(f.ship_to?.postal_code ?? "").slice(0, 5);
    const expZip = String(t.postal_code).slice(0, 5);
    checks.zip = { ok: zip === expZip, expected: expZip, actual: zip || null };
    if (zip !== expZip) reasons.zip = "邮编不符";
    const sub = cents(f.subtotal);
    const total = cents(f.total);
    if (sub === null) problems.push("商品小计读取不到或格式不对");
    if (total === null) problems.push("结算总额读取不到或格式不对");
    const range = { min: acc.min_amount, max: acc.max_amount };
    if (sub === null) {
      checks.subtotal = { ok: null, expected: range, actual: null };
    } else {
      const ok = sub >= cents(acc.min_amount) && (acc.max_amount === null || sub <= cents(acc.max_amount));
      checks.subtotal = { ok, expected: range, actual: money2(sub) };
      if (!ok) reasons.subtotal = "商品小计不在区间内";
    }
    const totalForCap = total === null ? null : total + Math.abs(cents(f.gift_card_amount) ?? 0);
    if (totalForCap === null) {
      checks.price_cap = { ok: null, expected: t.price_cap, actual: null };
    } else {
      const ok = totalForCap <= cents(t.price_cap);
      checks.price_cap = { ok, expected: t.price_cap, actual: money2(totalForCap) };
      if (!ok) reasons.price_cap = `总额 ${f.total} 超过限价 ${t.price_cap}`;
    }
    let days = null;
    if (f.delivery?.date) days = daysBetween(laDate(clock), f.delivery.date);
    else if (Number.isInteger(f.delivery?.days)) days = f.delivery.days;
    if (days !== null && days < 0) {
      days = null;
      problems.push("预计送达日期早于今天，读取不可靠");
    } else if (days === null) {
      problems.push("读取不到预计送达日期或天数");
    }
    if (days === null) {
      checks.delivery = { ok: null, expected: acc.max_delivery_days, actual: null };
    } else {
      const ok = days <= acc.max_delivery_days;
      checks.delivery = { ok, expected: acc.max_delivery_days, actual: days };
      if (!ok) reasons.delivery = `配送 ${days} 天超过上限 ${acc.max_delivery_days} 天`;
    }
    if (f.currency !== "USD") problems.push(`币种不是 USD（${typeof f.currency === "string" && f.currency ? f.currency : "读取不到"}）`);
    checks.data = { ok: problems.length === 0, expected: "USD；小计、总额可解析；配送天数可得；读到买家号 ID", actual: problems };
    if (problems.length) reasons.data = `结算数据不可靠：${problems.join("；")}`;
    const card = typeof f.card_last4 === "string" && f.card_last4 ? f.card_last4 : null;
    if (acc.expected_card_last4) {
      checks.card = { ok: card === acc.expected_card_last4, expected: acc.expected_card_last4, actual: card };
      if (card !== acc.expected_card_last4) reasons.card = "卡尾号不符";
    } else {
      checks.card = { ok: true, expected: null, actual: card };
    }
    const passed = CHECK_ORDER.every(([name]) => checks[name].ok === true);
    const code = passed ? null : (CHECK_ORDER.find(([name]) => checks[name].ok === false)?.[1] ?? "DATA_UNRELIABLE");
    return {
      verdict: passed ? "pass" : "block",
      code,
      reasons: CHECK_ORDER.filter(([name]) => name in reasons).map(([name]) => reasons[name]),
      checks,
      delivery_days: days,
      total_for_cap: money2(totalForCap),
      fulfillment: typeof f.fulfillment === "string" && f.fulfillment ? f.fulfillment : null,
    };
  }

  function finishAttempt(a, status, fields = {}) {
    Object.assign(a, fields);
    a.status = status;
    a.finished_at = clock;
  }

  /** 批次统计（真实服务端 _bump：execution_runs.stats[key] += 1）。 */
  function bump(run, key) {
    if (!run) return;
    run.stats = run.stats ?? {};
    run.stats[key] = (run.stats[key] ?? 0) + 1;
  }

  /** 暂停账号（真实服务端 accounts.pause）：更新原因，已暂停时保留最初的暂停时刻（恢复会清掉它；测试里直接把
   *  execution_paused 改回 false 的，按没暂停过算）。 */
  function pauseAccount(acc, reason) {
    acc.paused_at = acc.execution_paused && acc.paused_at != null ? acc.paused_at : clock;
    acc.execution_paused = true;
    acc.pause_reason = reason;
  }

  /** 尝试被迫结束（结束批次、清扫）：已登记 → unknown + 暂停；否则 aborted、任务回已分配。 */
  function settleInflight(a, cause) {
    const t = taskOf(a);
    if (a.submitted_at) {
      finishAttempt(a, "unknown", { error_detail: cause });
      t.status = "result_unknown";
      const acc = accountOf(runOf(a.run_id));
      pauseAccount(acc, "结果待核实");
      bump(runOf(a.run_id), "unknown");
    } else {
      finishAttempt(a, "aborted", { error_detail: cause });
      release(t);
      bump(runOf(a.run_id), "aborted");
    }
  }

  /** 结束批次（真实服务端 end_run）：它的在途尝试按租约过期同样处理；已经结束的批次不重复处理。 */
  function endRun(run, endReason, cause = END_REASON_ZH[endReason] ?? "执行批次已结束") {
    if (run.ended) return;
    for (const a of state.attempts.values()) {
      if (a.run_id === run.id && IN_FLIGHT.has(a.status)) settleInflight(a, cause);
    }
    run.ended = true;
    run.ended_at = clock;
    run.end_reason = endReason;
  }

  /** 护栏 buyer 项拦截的落地（真实服务端 execution._buyer_blocked；护栏判定与提交登记复核两处共用）。
   *  结算页上要下单的不是这个批次的买家号：不是任务的问题，也不是账号的问题，按**中止类**处理 ——
   *    · 尝试 → aborted（error_code = IDENTITY_MISMATCH，error_detail 为裁决的原因；未登记提交，不占额度）；
   *    · 任务回已分配、保留分配（上游已取消 → cancelled；领取后的来源变化照样落到任务上）；不进异常、不暂停账号；
   *    · 同一个事务里把批次结束（end_reason = identity_mismatch），批次统计记 aborted（不是 blocked）——
   *      否则只要浏览器上报的登录状态没变，同一批次会立刻再领到同一笔。
   *  at：'guard' = 护栏判定处，'submitting' = 提交登记前的复核处。 */
  function buyerBlocked(a, t, run, verdict, at) {
    const zh = END_REASON_ZH[BUYER_BLOCK_END_REASON];
    const why = verdict.reasons[0] ?? zh;
    finishAttempt(a, "aborted", { error_code: BUYER_BLOCK_CODE, error_detail: verdict.reasons.join("；") || zh, stage: at === "guard" ? "guard" : "submitting", blocked_at: at });
    release(t);
    if (t.status === "assigned") t.status_reason = `${why}，未提交前中止，任务保留待重新领取`;
    bump(run, "aborted");
    endRun(run, BUYER_BLOCK_END_REASON, zh);
  }

  /** 输入：尝试、上报体 → 输出：尝试应有的结局（真实服务端 _target_status）。 */
  function targetStatus(a, body) {
    if (body.outcome === "succeeded") {
      const ok = a.submitted_at && typeof body.amazon_order_no === "string" && body.amazon_order_no.startsWith("SIM-");
      return ok ? "succeeded" : "unknown";
    }
    if (body.outcome === "unknown" || a.submitted_at) return "unknown";
    return categories[body.error_code ?? ""] === "abort" ? "aborted" : "failed";
  }

  /** 设备登记（不要令牌头）。 */
  function register(body) {
    const rl = opts.registerRateLimit;
    if (rl) return err(429, "RATE_LIMITED", MAX_DEVICES_MESSAGE, rl.retry_after_s === undefined ? {} : { retry_after_s: rl.retry_after_s }, rl.header === undefined && rl.retry_after_s === undefined ? {} : { "retry-after": String(rl.header ?? rl.retry_after_s) });
    const token = body.device_token;
    if (typeof token !== "string" || !TOKEN_RE.test(token)) {
      return err(422, "VALIDATION", "浏览器令牌格式不对（应为 32–128 位字母、数字、下划线或连字符）", { field: "device_token" });
    }
    if (outdated(body.ext_version)) return outdatedErr(body.ext_version);
    state.registrations.push({ at: clock, token, workspace_id: body.workspace_id ?? null, ext_version: body.ext_version ?? null });
    let dev = state.tokens.get(token);
    let created = false;
    if (dev) {
      dev.hidden = false; // 幂等：返回那一行，不看 workspace_id
    } else {
      let ws = null;
      if (state.workspaces.length === 0) return err(409, "CONFLICT", "服务端还没有初始化：请先打开控制台创建用户");
      if (state.workspaces.length === 1) ws = state.workspaces[0];
      else ws = state.workspaces.find((w) => w.id === body.workspace_id) ?? null;
      if (!ws) return err(409, "WORKSPACE_REQUIRED", "系统里有多个工作区：请在插件设置的「工作区 ID」里填写（控制台「设置」页可以查到）");
      // 只数还在用的浏览器（真实服务端还排除过了保留期没联系的；这里没有时间维度，只排除移除了的）
      if (opts.maxDevices !== undefined && state.devices.filter((d) => d.workspace_id === ws.id && !d.hidden).length >= opts.maxDevices) {
        return err(429, "RATE_LIMITED", MAX_DEVICES_MESSAGE);
      }
      dev = addDevice(token, ws);
      created = true;
    }
    dev.ext_version = body.ext_version ?? dev.ext_version;
    return json(200, { device_id: dev.id, device_name: dev.name, workspace_id: dev.workspace_id, workspace_name: dev.workspace_name, created });
  }

  /** ACCOUNT_BUSY 的原话（真实服务端 execution._busy）。 */
  const busyText = (acc, deviceName, sinceMs) =>
    `账号 ${acc.name} 正由浏览器「${deviceName}」执行（北京时间 ${new Date(sinceMs + 8 * 3600_000).toISOString().slice(5, 16).replace("T", " ")} 起），同一账号同时只能有一个执行者`;

  /** 开始批次（真实服务端 execution.start_run 的顺序）。 */
  function startRun(dev, body) {
    const trigger = body.trigger === undefined ? "manual" : body.trigger;
    if (trigger !== "manual" && trigger !== "scheduled") return err(422, "VALIDATION", "触发方式只能是 manual 或 scheduled", { field: "trigger" });
    if (typeof body.simulated !== "boolean") return err(422, "VALIDATION", "simulated 必须是 true 或 false", { field: "simulated" });
    if (body.customer_id === undefined || body.customer_id === null || (typeof body.customer_id === "string" && !body.customer_id.trim())) {
      return err(422, "VALIDATION", "插件版本过旧，请更新插件", { field: "customer_id" });
    }
    const cid = typeof body.customer_id === "string" ? body.customer_id.replace(/\s+/g, "").toUpperCase() : "";
    if (cid.length > 64 || !CUSTOMER_ID_RE.test(cid)) return err(422, "VALIDATION", "买家号 ID 格式不对（应以 A 开头，至少 9 位字母或数字）", { field: "customer_id" });
    const mine = runOfDevice(dev);
    if (mine) {
      return err(409, "DEVICE_BUSY", "这个浏览器上一个批次还没有结束", {
        run_id: mine.id, account_name: accountOf(mine).name, since: iso(mine.started_at), lease_expires_at: iso(mine.lease_until),
      });
    }
    const st = loginState(dev);
    const stale = identityStale(dev); // 从没上报过的不算过期（detail.stale = false），由 login_state = unknown 说明
    const detail = { login_state: st, identity_at: iso(dev.identityAt), stale };
    if (st !== "signed_in") return err(403, "IDENTITY_MISMATCH", LOGIN_PROBLEM[st], detail);
    if (dev.identity.customer_id !== cid) return err(403, "IDENTITY_MISMATCH", "页面上的买家号与插件最近上报的不一致，请刷新亚马逊页面后重试", detail);
    if (stale) return err(403, "IDENTITY_MISMATCH", `${STALE_TEXT}，请打开或刷新亚马逊页面后重试`, detail);
    const acc = registered(cid);
    if (!acc) return err(403, "ACCOUNT_NOT_REGISTERED", "这个买家号还没有登记：请在控制台「账号」页登记后再开始", { customer_id: cid });
    if (acc.execution_paused) {
      // 真实服务端暂停时总会记下 paused_at（COALESCE(paused_at, now)）；测试直接改标志、没写时刻时按「刚刚暂停」
      return err(403, "ACCOUNT_PAUSED", `账号 ${acc.name} 已暂停执行：${acc.pause_reason ?? "人工暂停"}`, {
        pause_reason: acc.pause_reason ?? null, paused_at: iso(acc.paused_at ?? clock),
      });
    }
    if (body.simulated !== true) return err(403, "REAL_PURCHASE_DISABLED", "本阶段只允许模拟执行，服务端拒绝真实采购批次");
    let slot = null;
    if (trigger === "scheduled") {
      // 真实服务端 execution._norm_slot：没带（或空白）与写法不对是两句话
      if (body.slot === undefined || body.slot === null || (typeof body.slot === "string" && !body.slot.trim())) {
        return err(422, "VALIDATION", "定时执行需要带上时间点（slot，北京时间 HH:MM）", { field: "slot" });
      }
      const m = typeof body.slot === "string" ? /^\s*(\d{1,2}):(\d{2})\s*$/.exec(body.slot) : null;
      if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return err(422, "VALIDATION", "时间点格式不对（应为北京时间 HH:MM）", { field: "slot" });
      slot = `${m[1].padStart(2, "0")}:${m[2]}`;
      // 真实服务端 execution._check_slot：detail 带账号的执行方式 / 执行时间
      if (acc.purchase_mode !== "scheduled") {
        return err(422, "VALIDATION", `账号 ${acc.name} 不是定时执行模式，不能按时间点开始`, { field: "slot", purchase_mode: acc.purchase_mode });
      }
      const times = [...(acc.schedule_times ?? [])];
      if (!times.includes(slot)) {
        return err(422, "VALIDATION", `时间点 ${slot} 不在账号 ${acc.name} 的执行时间里（${times.join("、") || "未设置"}）`, { field: "slot", schedule_times: times });
      }
      if (bjHm(clock) < slot) return err(422, "VALIDATION", `北京时间还没有到 ${slot}，不能开始这个时间点的批次`, { field: "slot" });
      // 只有以 finished 结束的才算「已经执行过」（真实服务端 _slot_finished）。同一个时间点的批次还在执行时
      // 走下面的占用检查回 ACCOUNT_BUSY：它可能没跑完就结束，那时时间点重新空出来。
      const done = [...state.runs.values()].some((r) => r.account_id === acc.id && r.trigger === "scheduled" && r.bj_date === bjDate(clock) && r.scheduled_slot === slot && r.ended && r.end_reason === "finished");
      if (done) return err(409, "SLOT_DONE", "这个时间点今天已经执行过");
    }
    if (state.foreignRun && acc.id === state.account.id) {
      return err(409, "ACCOUNT_BUSY", busyText(acc, state.foreignRun.device_name, Date.parse(state.foreignRun.since)), {
        holder_device_id: state.foreignRun.device_id, holder_device_name: state.foreignRun.device_name, since: state.foreignRun.since,
      });
    }
    const holder = runOfAccount(acc);
    if (holder) {
      return err(409, "ACCOUNT_BUSY", busyText(acc, deviceOf(holder).name, holder.started_at), {
        holder_device_id: holder.device_id, holder_device_name: deviceOf(holder).name, since: iso(holder.started_at),
      });
    }
    const run = {
      id: state.nextRunId++, trigger, account_id: acc.id, device_id: dev.id, customer_id: cid, scheduled_slot: slot, bj_date: bjDate(clock),
      started_at: clock, lease_until: clock + leaseTtlS * 1000, ended: false, ended_at: null, stop: false, stop_reason: null, end_reason: null,
      plugin_reason: null, finish_detail: null, beats: 0, stats: {},
    };
    state.runs.set(run.id, run);
    return json(200, { run_id: run.id, lease_ttl_s: leaseTtlS, lease_expires_at: iso(run.lease_until), account: { id: acc.id, name: acc.name } });
  }

  function route(path, body, dev) {
    let m;
    if (path === "/plugin/v1/heartbeat") {
      if (outdated(body.ext_version)) return outdatedErr(body.ext_version);
      dev.hidden = false;
      if (body.identity) storeIdentity(dev, body.identity, "heartbeat");
      return json(200, statusOut(dev));
    }
    if (path === "/plugin/v1/identity") {
      // 真实服务端 report_identity = 带身份的心跳：同样先过版本闸（什么也不记）、清掉「已移除」
      if (outdated(body.ext_version)) return outdatedErr(body.ext_version);
      dev.hidden = false;
      storeIdentity(dev, body, "identity");
      return json(200, statusOut(dev));
    }

    if (path === "/plugin/v1/runs") return startRun(dev, body);
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/heartbeat$/.exec(path))) {
      const run = runOf(m[1]);
      if (!runActive(run)) return leaseLost(run);
      run.beats += 1;
      run.lease_until = clock + leaseTtlS * 1000;
      const reason = run.stop ? run.stop_reason : stopReason(run);
      return json(200, { lease_expires_at: new Date(run.lease_until).toISOString(), stop: reason !== null, stop_reason: reason });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/next$/.exec(path))) {
      const run = runOf(m[1]);
      if (!runActive(run)) return leaseLost(run);
      const acc = accountOf(run);
      const inflight = [...state.attempts.values()].find((a) => a.run_id === run.id && IN_FLIGHT.has(a.status));
      if (inflight) return err(409, "ATTEMPT_IN_FLIGHT", "本批次已有在途尝试", { attempt_id: inflight.id });
      // 真实服务端 next_attempt：账号暂停 / 归档、买家号不符、登录状态过期时不领取，返回原因
      const sr = stopReason(run);
      if (sr === "account_paused") return json(200, { attempt: null, reason: `账号已暂停执行：${acc.pause_reason ?? "人工暂停"}` });
      if (sr === "account_archived") return json(200, { attempt: null, reason: "账号已归档，停止领取" });
      if (sr !== null) return json(200, { attempt: null, reason: `${identityProblem(dev, acc)}，停止领取` });
      if (identityStale(dev)) return json(200, { attempt: null, reason: `${STALE_TEXT}，停止领取（请打开或刷新亚马逊页面后重新开始）` });
      const used = usedToday(acc);
      if (used >= acc.daily_limit) {
        return json(200, { attempt: null, reason: `账号 ${acc.name} 今天（${bjDate(clock)}）的额度已用完：已占用 ${used} / 上限 ${acc.daily_limit}` });
      }
      const elsewhere = [...state.attempts.values()].find((a) => a.account_id === acc.id && IN_FLIGHT.has(a.status));
      if (elsewhere) return json(200, { attempt: null, reason: `账号还有一笔进行中的采购尝试（#${elsewhere.id}），等它结束后再领取` });
      if (hooks.beforeClaim) { const r = hooks.beforeClaim(state); if (r) return json(200, r); }
      // 领取时复核来源指定（真实服务端 designation.off_target）：来源指定的买家号已经不是这个账号 → 解除分配回待分配、跳过
      let redesignated = 0;
      for (const x of state.tasks) {
        if (x.account_id === acc.id && x.status === "assigned" && !x.cancelled && !x.hold && !x.future && x.off_target) {
          x.status = "queued";
          x.account_id = null;
          x.status_reason = `来源指定的买家号已不是账号 ${acc.name}，领取时解除分配`;
          redesignated += 1;
        }
      }
      const t = state.tasks.find((x) => x.account_id === acc.id && x.status === "assigned" && !x.cancelled && !x.hold && !x.future);
      if (!t) {
        const extras = [];
        const future = state.tasks.filter((x) => x.account_id === acc.id && x.status === "assigned" && !x.cancelled && x.future).length;
        if (future) extras.push(`另有 ${future} 笔建议采购日期在未来`);
        if (redesignated) extras.push(`${redesignated} 笔来源指定的买家号已不是本账号、已解除分配`);
        return json(200, { attempt: null, reason: `今天没有可执行的任务${extras.length ? `（${extras.join("，")}）` : ""}` });
      }
      t.status = "executing";
      const a = { id: state.nextAttemptId++, task_id: t.id, run_id: run.id, account_id: acc.id, bj_date: bjDate(clock), attempt_no: [...state.attempts.values()].filter((x) => x.task_id === t.id).length + 1, status: "claimed", submitted_at: null, resolution: null, guard: null, guard_input: null, guards: 0, submits: 0, results: [] };
      t.current_attempt_id = a.id;
      state.attempts.set(a.id, a);
      bump(run, "claimed");
      if (hooks.afterClaim) hooks.afterClaim(t, a, state);
      return json(200, {
        // 载荷照真实服务端 execution._payload（插件用到的那些键）：里面没有分配方式，也没有来源指定的买家号
        attempt: {
          attempt_id: a.id, attempt_no: a.attempt_no, kind: "auto", order_id: t.id,
          upstream_order_no: t.upstream_order_no, asin: t.asin, quantity: t.quantity,
          unit_price: t.unit_price, price_cap: t.price_cap, max_delivery_days: acc.max_delivery_days,
          amount_range: { min: acc.min_amount, max: acc.max_amount },
          recipient_name: "Demo Buyer", address1: "1 Demo Way", address2: null, city: "Demo City", state: "FL",
          postal_code: t.postal_code, phone: "000-000-0000", country: "US",
          expected_card_last4: acc.expected_card_last4, simulated: t.simulated ?? true,
          account: { id: acc.id, name: acc.name },
        },
        reason: null,
      });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/finish$/.exec(path))) {
      const run = runOf(m[1]);
      if (!runActive(run)) return leaseLost(run);
      // end_reason 是封闭取值：插件结束的批次恒为 finished；插件给的短码另记（真实服务端记进 run.ended 事件的 plugin_reason）
      run.plugin_reason = typeof body.reason === "string" && body.reason.trim() ? body.reason.trim().slice(0, 64) : "finished";
      run.finish_detail = pluginDetail(body.detail);
      endRun(run, "finished", "插件结束了执行批次");
      return json(200, { run_id: run.id, ended_at: iso(run.ended_at), end_reason: run.end_reason, stats: run.stats ?? {} });
    }

    if ((m = /^\/plugin\/v1\/attempts\/(\d+)\/(progress|guard|submitting|verifying|result)$/.exec(path))) {
      const a = attemptOf(m[1]);
      if (!a) return err(404, "NOT_FOUND", "采购尝试不存在");
      const run = runOf(a.run_id);
      const t = taskOf(a);
      const acc = accountOf(run);
      const op = m[2];
      if (op === "progress") {
        if (!runActive(run)) return leaseLost(run);
        const stop = (code, reason) => json(200, { continue: false, cancel_reason: reason, ...(cancelCodes ? { cancel_code: code } : {}) });
        if (!IN_FLIGHT.has(a.status)) return stop("ATTEMPT_FINISHED", `本次采购尝试已结束（${STATUS_ZH[a.status]}）`);
        a.stage = body.stage;
        if (a.status === "claimed") a.status = "checking";
        if (!a.submitted_at) {
          if (t.cancelled) return stop("UPSTREAM_CANCELLED", "上游已取消");
          if (acc.archived) return stop("ACCOUNT_ARCHIVED", "账号已归档");
          if (acc.execution_paused) return stop("ACCOUNT_PAUSED", `账号已暂停执行：${acc.pause_reason ?? "人工暂停"}`);
          if (identityProblem(dev, acc)) return stop("IDENTITY_MISMATCH", identityProblem(dev, acc));
          if (identityStale(dev)) return stop("IDENTITY_STALE", STALE_TEXT); // 真实服务端 _progress_stop 的原话
          if (t.hold) return stop("SOURCE_HOLD", `${t.hold.text}，未提交前中止`);
          if (t.drift) return stop("SOURCE_CHANGED", `${t.drift.text}，未提交前中止`);
        }
        // afterProgress：这一次答复了「继续」之后才发生的变化（例如登记前确认之后、登记之前来源才变）
        if (hooks.afterProgress) hooks.afterProgress(t, a, state, body.stage);
        return json(200, { continue: true, cancel_reason: null, ...(cancelCodes ? { cancel_code: null } : {}) });
      }
      if (op === "guard") {
        if (!runActive(run)) return leaseLost(run);
        a.guards += 1;
        if (a.status === "submitting" || a.status === "verifying") return err(409, "CONFLICT", "已登记提交，不能再次做护栏判定", { attempt_status: a.status });
        if (!PRE_SUBMIT.has(a.status)) return err(409, "CONFLICT", `本次采购尝试已结束（${STATUS_ZH[a.status]}）`, { attempt_status: a.status });
        a.guard_input = body; // 存下的结算事实：提交登记时按最新配置再复核一次
        a.guard = guardEval(a, body);
        if (a.guard.verdict === "pass") {
          a.status = "guard_passed";
          if (hooks.afterGuard) hooks.afterGuard(t, a, state);
        } else if (a.guard.code === BUYER_BLOCK_CODE) {
          // buyer 项拦截：中止类，服务端同时把批次结束。答复照旧是 200 的拦截裁决，形状不变（没有 run_ended 之类的键）。
          buyerBlocked(a, t, run, a.guard, "guard");
        } else {
          // 一般的拦截：尝试结束为 failed（错误码 = 拦截码），任务转异常 —— 和真实服务端一样，不等插件上报；批次继续。
          finishAttempt(a, "failed", { error_code: a.guard.code });
          t.status = t.cancelled ? "cancelled" : "exception";
          t.status_reason = a.guard.code;
          bump(run, "blocked");
        }
        return json(200, a.guard);
      }
      if (op === "submitting") {
        a.submits += 1;
        if (!runActive(run)) return leaseLost(run);
        if (a.status !== "guard_passed" || !a.guard || a.guard.verdict !== "pass") {
          const msg = a.status === "submitting" || a.status === "verifying" ? "该尝试已登记提交，不能重复登记"
            : FINAL.has(a.status) ? `本次采购尝试已结束（${STATUS_ZH[a.status]}），不能提交` : "护栏还没有放行，不能提交";
          return err(409, "CONFLICT", msg, { attempt_status: a.status });
        }
        if (t.cancelled) return err(409, "CONFLICT", "上游已取消，不能提交", { order_status: t.status });
        // 真实服务端 _owns：任务还在执行**这一笔**（控制台改派、退回之类会让它不再是）
        if (t.status !== "executing" || t.current_attempt_id !== a.id) return err(409, "CONFLICT", "任务状态已变化，不能提交", { order_status: t.status });
        if (acc.archived) return err(409, "CONFLICT", "账号已归档，不能提交");
        if (acc.execution_paused) return err(409, "ACCOUNT_PAUSED", `账号已暂停执行（${acc.pause_reason ?? "人工暂停"}），不能提交`);
        if (identityProblem(dev, acc)) return err(409, "IDENTITY_MISMATCH", `${identityProblem(dev, acc)}，不能提交`, { login_state: loginState(dev), stale: false, identity_at: iso(dev.identityAt) });
        if (identityStale(dev)) return err(409, "IDENTITY_MISMATCH", `${STALE_TEXT}，不能提交`, { login_state: "signed_in", stale: true, identity_at: iso(dev.identityAt) });
        if (t.hold) return err(409, "SOURCE_CHANGED", `${t.hold.text}，不能提交`, { conflict_kind: t.hold.kind ?? null });
        if (t.drift) return err(409, "SOURCE_CHANGED", `${t.drift.text}，不能提交`, { fields: t.drift.fields ?? [], review: false, external: false });
        const refused = hooks.submitRefusal ? hooks.submitRefusal(a, t) : null;
        if (refused) return err(409, refused.code ?? "CONFLICT", refused.message, refused.detail ?? {});
        // 按最新配置（账号**当前**登记的买家号 ID、金额区间、配送上限、卡尾号、任务限价、站点今天）对存下的结算事实复核护栏。
        // recheck 钩子直接指定复核的结论：返回拦截码（字符串），或 {code: null} 模拟服务端没给 block_code。
        let recheck = guardEval(a, a.guard_input ?? {});
        const forced = hooks.recheck ? hooks.recheck(a, t) : null;
        if (forced) {
          const code = typeof forced === "string" ? forced : forced.code ?? null;
          recheck = { verdict: "block", code, reasons: code ? [code] : [] };
        }
        if (recheck.verdict !== "pass") {
          const message = `按最新配置复核护栏未通过：${recheck.reasons[0] ?? "护栏拦截"}，不能提交`;
          if (recheck.code === BUYER_BLOCK_CODE) {
            // 唯一会改状态的拒绝：存下的结算页买家号 ID 已不是批次账号当前登记的 ID。和护栏判定处同样落地
            // （尝试 aborted、任务回已分配、批次结束），照样回 409 —— 真实服务端这里不抛异常、事务照常提交。
            a.guard = recheck;
            buyerBlocked(a, t, run, recheck, "submitting");
            return err(409, "GUARD_BLOCKED", message, {
              block_code: recheck.code, reasons: recheck.reasons, attempt_status: a.status, order_status: t.status,
              run_ended: true, end_reason: BUYER_BLOCK_END_REASON,
            });
          }
          return err(409, "GUARD_BLOCKED", message, recheck.code ? { block_code: recheck.code, reasons: recheck.reasons } : { reasons: [] });
        }
        a.submitted_at = clock;
        a.status = "submitting";
        return json(200, { ok: true, submitted_at: new Date(clock).toISOString() });
      }
      if (op === "verifying") {
        if (!runActive(run)) return leaseLost(run);
        if (a.status !== "submitting" && a.status !== "verifying") return err(409, "CONFLICT", "只有已登记提交的尝试才能进入等待验证", { attempt_status: a.status });
        a.status = "verifying";
        return json(200, { ok: true, attempt_status: "verifying" });
      }
      // result
      a.results.push(body);
      const target = targetStatus(a, body);
      if (FINAL.has(a.status)) {
        const same = a.status === target && (target === "succeeded" ? a.amazon_order_no === body.amazon_order_no : (a.error_code ?? null) === (body.error_code ?? null));
        if (same) return json(200, { attempt_status: a.status, order_status: t.status, duplicate: true });
        const what = OUTCOME_ZH[body.outcome] ?? body.outcome;
        if (a.status === "unknown") {
          state.lateReceipts.push({ attempt_id: a.id, body });
          return err(409, "CONFLICT", `该采购尝试已登记为「结果待核实」，迟到的结果（${what}）已记录，请在控制台核实`,
            { attempt_status: "unknown", order_status: t.status, late_receipt_recorded: true });
        }
        if (a.resolution === "ordered" || a.resolution === "not_ordered") {
          // 已由控制台核实（真实服务端 _late_receipt_after_resolution）：只记下，不改尝试与核实结论
          state.lateReceipts.push({ attempt_id: a.id, body, resolution: a.resolution });
          const contradicts = body.outcome === "succeeded" && !!body.amazon_order_no
            && (a.resolution === "not_ordered" || body.amazon_order_no !== a.amazon_order_no);
          const resZh = a.resolution === "ordered" ? "已下单" : "未下单";
          return err(409, "CONFLICT",
            `该采购尝试已人工核实为「${resZh}」，迟到的结果（${what}）已记录${contradicts ? "，与核实结论矛盾，请在控制台核实是否重复下单" : "，不改变核实结论"}`,
            { attempt_status: a.status, order_status: t.status, late_receipt_recorded: true });
        }
        return err(409, "CONFLICT", `该采购尝试的结果已登记为「${STATUS_ZH[a.status]}」，与本次上报不一致`, { attempt_status: a.status, reported: body.outcome });
      }
      if (!a.submitted_at && !runActive(run)) return leaseLost(run);
      if (target === "succeeded") {
        finishAttempt(a, "succeeded", { amazon_order_no: body.amazon_order_no }); t.status = "purchased";
        bump(run, "succeeded");
        return json(200, { attempt_status: "succeeded", order_status: t.status });
      }
      if (target === "unknown") {
        finishAttempt(a, "unknown", { error_code: body.error_code ?? null }); t.status = "result_unknown";
        pauseAccount(acc, "结果待核实");
        bump(run, "unknown");
        return json(200, { attempt_status: "unknown", order_status: t.status });
      }
      if (target === "aborted") {
        // 中止类（ABORTED_*）：任务回到已分配、再按来源变化重新定状态；不暂停账号、不占额度
        finishAttempt(a, "aborted", { error_code: body.error_code }); release(t);
        bump(run, "aborted");
        return json(200, { attempt_status: "aborted", order_status: t.status });
      }
      const category = categories[body.error_code ?? ""] ?? "task";
      finishAttempt(a, "failed", { error_code: body.error_code });
      if (category === "account") {
        release(t); pauseAccount(acc, ACCOUNT_ZH[body.error_code] ?? "账号级错误");
      } else if (category === "retry_once") {
        t.page_load_failures += 1;
        if (t.page_load_failures >= 2) t.status = "exception"; else release(t);
      } else {
        t.status = "exception"; t.status_reason = body.error_code;
      }
      bump(run, "failed");
      return json(200, { attempt_status: "failed", order_status: t.status });
    }
    return err(404, "NOT_FOUND", "Not Found");
  }

  async function fakeFetch(url, init = {}) {
    const u = new URL(url);
    const path = u.pathname;
    let body = {};
    try { body = init.body ? JSON.parse(init.body) : {}; } catch { body = {}; }
    const headers = init.headers ?? {};
    const auth = headers["Authorization"] ?? headers["authorization"] ?? null;
    calls.push({ path, body, auth, at: clock, origin: u.origin, contentType: headers["Content-Type"] ?? headers["content-type"] ?? null });
    // 插件入口闸在认证之前（server/limits）：非本机对端、外来 Host、网页来源、不是 JSON
    if (opts.gate) return err(403, opts.gate.code ?? "FORBIDDEN", opts.gate.message ?? "插件接口只接受本机请求：跨机器部署需要接入密钥，当前版本不支持");
    const isRegister = path === "/plugin/v1/register";
    const token = typeof auth === "string" && auth.startsWith("Bearer ") ? auth.slice(7) : null;
    const dev = isRegister ? null : (token ? state.tokens.get(token) ?? null : null);
    const authed = isRegister || dev !== null;
    const handle = () => (isRegister ? register(body) : route(path, body, dev));
    const f = faults.find((x) => x.times > 0 && x.match.test(path));
    if (f) {
      f.times -= 1;
      if (f.status) return err(f.status, "INTERNAL", "服务器内部错误");
      if (f.resend) {
        if (authed) handle(); // 第一次：处理了，响应丢了
        calls.push({ path, body, auth, at: clock, resent: true });
        // 浏览器在复用的连接上把同一个 POST 原样重发：返回第二次的答复
      } else {
        if (f.after && authed) handle(); // 请求到了服务端，但响应丢了
        throw new TypeError("fetch failed");
      }
    }
    if (!authed) return err(401, "UNAUTHORIZED", "这个浏览器还没有登记，插件会自动重新登记");
    if (dev) state.device = dev;
    const res = handle();
    clock += tickMs;
    return res;
  }

  return {
    fetch: fakeFetch,
    state,
    calls,
    now: () => clock,
    setNow: (ms) => { clock = ms; },
    advance: (ms) => { clock += ms; },
    count: (re) => calls.filter((c) => re.test(c.path)).length,
    /** 输入：设备（缺省最近的那个）、身份 → 输出：无。直接改这台设备最新的身份（等于它刚上报了这一份）。 */
    setLogin: (identity, dev = state.device) => { dev.identity = identity === null ? null : cleanIdentity(identity); dev.identityAt = identity === null ? null : clock; },
    /** 服务端换了库（或这一行被清理了）：所有令牌都不认识了。之后带令牌的请求回 401，重新登记得到新的一行。 */
    forgetDevices: () => { state.tokens.clear(); state.devices.length = 0; state.device = null; },
    loginState: (dev = state.device) => resolveLogin(dev).state,
    /** 租约清扫（真实服务端 execution.sweep 的批次部分）：租约已过期、还没结束的批次按 lease_expired 结束，在途尝试
     *  照结束批次的规则落地（未登记提交的回已分配）。输出：结束了的批次 id。定时批次被这样结束后不占时间点。 */
    sweep: () => {
      const ended = [];
      for (const r of state.runs.values()) {
        if (!r.ended && clock >= r.lease_until) {
          endRun(r, "lease_expired");
          ended.push(r.id);
        }
      }
      return ended;
    },
    /** 控制台核实一笔结果待核实的尝试（真实服务端 orders.resolve_unknown）：ordered → 尝试成功（记单号）、任务已采购；
     *  not_ordered → 尝试失败、任务回已分配。之后插件再送来的不同回执按迟到回执记下（409 late_receipt_recorded）。 */
    resolve: (attemptId, resolution, amazonOrderNo = null) => {
      const a = attemptOf(attemptId);
      const t = taskOf(a);
      a.resolution = resolution;
      if (resolution === "ordered") {
        a.status = "succeeded";
        a.amazon_order_no = amazonOrderNo;
        t.status = "purchased";
      } else {
        a.status = "failed";
        t.status = "assigned";
      }
    },
  };
}
