/** 错误码与中文说明（结构化错误码，不写自由文本）。
 *
 * 几组来源：
 *   · 服务端统一错误码（契约 §8 末尾），接口返回的 `code`；
 *   · 护栏拦截码（契约 §4），`/guard` 返回的 `code`，插件原样作为 `error_code` 上报；
 *   · 尝试失败原因（契约 §5「结果处理」表，服务端 services/models.ERROR_CODES）；
 *   · 进度 continue=false 的机读原因 cancel_code（契约 §5 第 7 步）；
 *   · 插件本地的码：描述「没说上话」（NETWORK / TIMEOUT / BAD_RESPONSE）、没配对等。
 *
 * 原样的英文码（含认不出的）不进面板、弹出页与设置页：一律经 codeLabel / cancelLabel /
 * stopReasonLabel / stageLabel 换成中文。
 *
 * 文案规则（EasyQuick README）：简体中文，不用感叹号；失败提示「动作 + 失败：原因」，
 * 这里只给「原因」那半句，动作由调用处拼。
 */

/** 服务端统一错误码（契约 §8，顺序同契约与 web 的 API_ERROR_CODES）。 */
export const SERVER_CODE_LABEL: Record<string, string> = {
  UNAUTHORIZED: "设备令牌无效或已撤销，请重新配对",
  FORBIDDEN: "没有权限",
  NOT_FOUND: "服务端没有这个接口或资源",
  VALIDATION: "请求参数不正确",
  CONFLICT: "状态冲突，服务端拒绝了这次操作",
  ACCOUNT_BUSY: "该账号正在其他设备或页面上执行",
  ATTEMPT_IN_FLIGHT: "本批次还有一笔未结束的尝试",
  IDENTITY_MISMATCH: "身份未通过：当前浏览器登录的亚马逊账号与绑定账号不一致，或 10 分钟内没有上报身份",
  NOT_BOUND: "本设备还没有绑定买家账号，请在控制台「账号与设备」里绑定",
  ACCOUNT_PAUSED: "账号已暂停执行，请在控制台恢复后再试",
  REAL_PURCHASE_DISABLED: "服务端当前只允许模拟执行",
  LEASE_LOST: "执行权已失效（租约过期或批次已被结束）",
  FEISHU_NOT_CONFIGURED: "服务端还没有配置飞书应用",
  FEISHU_ERROR: "飞书接口出错",
  INVALID_TRANSITION: "任务状态不允许这次变更",
  SOURCE_CHANGED: "领取后来源有变化，提交前中止",
  // 既是服务端错误码（提交登记时按最新配置复核护栏不过），也是插件上报的兜底拦截码（护栏没给拦截码时）
  GUARD_BLOCKED: "护栏拦截：结算信息没有通过护栏校验",
  RATE_LIMITED: "尝试次数过多，请稍后再试",
  METHOD_NOT_ALLOWED: "服务端不支持这个请求方法",
  INTERNAL: "服务端内部错误",
};

/** 护栏拦截码（契约 §4，服务端 services/models.ERROR_CODES 的 guard 类）。 */
export const GUARD_CODE_LABEL: Record<string, string> = {
  ITEM_MISMATCH: "商品或数量与任务不符",
  ZIP_MISMATCH: "收货邮编与任务不符",
  SUBTOTAL_OUT_OF_RANGE: "商品小计不在账号金额区间内",
  PRICE_OVER_CAP: "结算总额超过限价",
  DELIVERY_TOO_LONG: "配送时长超过上限",
  DATA_UNRELIABLE: "结算数据不完整或不可靠",
  CARD_MISMATCH: "支付卡尾号与账号设置不符",
};

/** 尝试失败原因（`result.error_code`，服务端 services/models.ERROR_CODES 的其余各类）。
 *  插件本阶段上报的只是其中一部分；其余是真实驱动接入后会用到的，界面上（例如待补交的回执）也要有中文。 */
export const ATTEMPT_CODE_LABEL: Record<string, string> = {
  OUT_OF_STOCK: "商品缺货",
  QUANTITY_UNAVAILABLE: "无法选择所需数量",
  ADDRESS_FAILED: "收货地址填写失败",
  CART_MISMATCH: "购物车内容与任务不一致",
  UNEXPECTED_PAGE: "页面结构与预期不符",
  PAGE_LOAD_FAILED: "页面加载失败",
  LOGIN_REQUIRED: "买家账号登录失效",
  ACCOUNT_RESTRICTED: "买家账号受限",
  CARD_DECLINED: "银行卡被拒付",
  CAPTCHA_ENCOUNTERED: "遇到验证码或风控，转人工处理",
  ABORTED_UPSTREAM_CANCELLED: "上游已取消，未提交前中止",
  ABORTED_BY_USER: "插件端人工中止，未提交前中止",
  ABORTED_BY_SERVER: "服务端要求中止（账号暂停或归档、身份不符或过期、来源有变化），未提交前中止",
  SUBMIT_REJECTED: "服务端拒绝了提交登记",
  PLUGIN_SIMULATION_ONLY: "插件当前只做模拟执行，拒绝执行非模拟尝试",
};

/** 插件本地的错误（没说上话、没配对等），不上报。 */
export const LOCAL_CODE_LABEL: Record<string, string> = {
  NETWORK: "无法连接服务端",
  TIMEOUT: "请求超时",
  BAD_RESPONSE: "服务端响应格式不正确",
  NOT_PAIRED: "尚未配对，请先在设置页完成配对",
  SW_UNAVAILABLE: "插件后台没有响应",
  TAB_BUSY: "本浏览器的另一个亚马逊页面正在执行",
  ALREADY_RUNNING: "本页面已经在执行",
  NO_IDENTITY: "没有读到亚马逊登录状态",
};

/** 认不出的错误码的说明：界面上不出现原样的英文码。 */
export const UNKNOWN_CODE_LABEL = "其他原因（插件不认识这个错误码）";

/** 输入：任意错误码 → 输出：中文说明。`HTTP_<状态码>`（服务端没给错误体）说状态码；
 *  其他认不出的码给一句通用说明 —— 原样的英文码不进面板、弹出页与设置页。 */
export function codeLabel(code: string | null | undefined): string {
  if (!code) return "未知原因";
  const known = SERVER_CODE_LABEL[code] ?? GUARD_CODE_LABEL[code] ?? ATTEMPT_CODE_LABEL[code] ?? LOCAL_CODE_LABEL[code];
  if (known) return known;
  const http = /^HTTP_(\d{3})$/.exec(code);
  if (http) return Number(http[1]) >= 500 ? `服务端出错（状态码 ${http[1]}）` : `服务端拒绝了请求（状态码 ${http[1]}）`;
  return UNKNOWN_CODE_LABEL;
}

/** 进度 `continue=false` 的机读原因（服务端 execution.CANCEL_CODES，契约 §5 第 7 步）的中文说明。
 *  服务端同时给 cancel_reason（中文原话，更具体）；这里是它缺失时的兜底，也是日志里的分类。 */
export const CANCEL_CODE_LABEL: Record<string, string> = {
  UPSTREAM_CANCELLED: "上游已取消",
  ACCOUNT_ARCHIVED: "账号已归档",
  ACCOUNT_PAUSED: "账号已暂停执行",
  IDENTITY_MISMATCH: "浏览器登录的买家账号与绑定账号不一致，或没有登录",
  IDENTITY_STALE: "插件超过 10 分钟没有上报登录身份，身份信息已过期",
  SOURCE_HOLD: "来源把这笔任务暂停了（有冲突待处理）",
  SOURCE_CHANGED: "领取后来源有变化，提交前中止",
  ATTEMPT_FINISHED: "本次采购尝试已结束",
};

/** 输入：进度返回的 cancel_code → 输出：中文说明；没给或认不出时为通用说明。 */
export function cancelLabel(code: string | null | undefined): string {
  return (code ? CANCEL_CODE_LABEL[code] : undefined) ?? "服务端要求中止";
}

/** 账号级错误：服务端收到后会暂停账号执行（契约 §5 结果处理表）。插件随后结束本批次。 */
export const ACCOUNT_LEVEL_CODES: ReadonlySet<string> = new Set([
  "LOGIN_REQUIRED",
  "ACCOUNT_RESTRICTED",
  "CARD_DECLINED",
  "CAPTCHA_ENCOUNTERED",
  "IDENTITY_MISMATCH",
  "PLUGIN_SIMULATION_ONLY",
]);

/** 输入：进度返回的 cancel_code（服务端没给为 null）→ 输出：插件要上报的中止码；null = 不用上报
 *  （尝试已经结束了）。
 *
 *  · UPSTREAM_CANCELLED → ABORTED_UPSTREAM_CANCELLED（只有真的是上游取消才这么报）；
 *  · ATTEMPT_FINISHED → 不上报（服务端已经结束了这一笔，再报只会 409）；
 *  · 其他（账号暂停 / 归档、身份不符或过期、来源有变化）→ ABORTED_BY_SERVER（中止类，任务回到已分配）；
 *  · 服务端没给 cancel_code（旧服务端）→ ABORTED_UPSTREAM_CANCELLED：那时它认得的中止码只有这一个，
 *    报一个它不认识的码会被当成任务级失败、任务转异常。 */
export function abortCodeFor(cancelCode: string | null | undefined): string | null {
  if (cancelCode === null || cancelCode === undefined || cancelCode === "") return "ABORTED_UPSTREAM_CANCELLED";
  if (cancelCode === "UPSTREAM_CANCELLED") return "ABORTED_UPSTREAM_CANCELLED";
  if (cancelCode === "ATTEMPT_FINISHED") return null;
  return "ABORTED_BY_SERVER";
}

/** 执行阶段（`progress.stage`）的中文名。 */
export const STAGE_LABEL: Record<string, string> = {
  claimed: "已领取",
  started: "开始模拟",
  checkout: "模拟读取结算信息",
  guard: "护栏校验",
  before_submit: "准备提交登记",
  submitting: "提交登记",
  reporting: "上报结果",
  done: "本笔完成",
};

/** 批次心跳 `stop_reason` 的中文名（服务端 execution._stop_reason 的四个值，外加两个批次结束原因）。 */
export const STOP_REASON_LABEL: Record<string, string> = {
  account_paused: "账号已被暂停执行",
  account_archived: "账号已归档",
  device_unbound: "设备已解除绑定或改绑到别的账号",
  identity_mismatch: "浏览器登录的买家账号与绑定账号不一致，或身份已过期",
  lease_expired: "执行租约已过期",
  device_revoked: "设备授权已被撤销",
};

/** 输入：停止原因码 → 输出：中文说明。 */
export function stopReasonLabel(reason: string | null | undefined): string {
  return (reason ? STOP_REASON_LABEL[reason] : undefined) ?? "服务端要求停止";
}

/** 输入：执行阶段名 → 输出：中文名（认不出时为「执行中」，不显示原样的阶段名）。 */
export function stageLabel(stage: string | null | undefined): string {
  return (stage ? STAGE_LABEL[stage] : undefined) ?? "执行中";
}
