/** 错误码与中文说明（结构化错误码，不写自由文本）。
 *
 * 三组来源：
 *   · 服务端统一错误码（契约 §8 末尾），接口返回的 `code`；
 *   · 护栏拦截码（契约 §4），`/guard` 返回的 `code`，插件原样作为 `error_code` 上报；
 *   · 插件自己产生的码：一组描述「没说上话」（NETWORK / TIMEOUT / BAD_RESPONSE），
 *     一组是上报给服务端的尝试失败原因（契约 §5「结果处理」表里列的那些）。
 *
 * 文案规则（EasyQuick README）：简体中文，不用感叹号；失败提示「动作 + 失败：原因」，
 * 这里只给「原因」那半句，动作由调用处拼。
 */

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
};

export const GUARD_CODE_LABEL: Record<string, string> = {
  ITEM_MISMATCH: "商品或数量与任务不符",
  ZIP_MISMATCH: "收货邮编与任务不符",
  SUBTOTAL_OUT_OF_RANGE: "商品小计不在账号金额区间内",
  PRICE_OVER_CAP: "结算总额超过限价",
  DELIVERY_TOO_LONG: "配送时长超过上限",
  DATA_UNRELIABLE: "结算数据不完整或不可靠",
  CARD_MISMATCH: "支付卡尾号与账号设置不符",
};

/** 插件上报给服务端的尝试失败原因（`result.error_code`）。 */
export const ATTEMPT_CODE_LABEL: Record<string, string> = {
  OUT_OF_STOCK: "商品缺货",
  PAGE_LOAD_FAILED: "页面加载失败",
  CAPTCHA_ENCOUNTERED: "遇到验证码或风控，转人工处理",
  ABORTED_UPSTREAM_CANCELLED: "上游已取消，未提交前中止",
  GUARD_BLOCKED: "护栏拦截（服务端未给出拦截码）",
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

/** 输入：任意错误码 → 输出：中文说明；认不出就原样返回码本身（不吞掉）。 */
export function codeLabel(code: string | null | undefined): string {
  if (!code) return "未知原因";
  return (
    SERVER_CODE_LABEL[code] ??
    GUARD_CODE_LABEL[code] ??
    ATTEMPT_CODE_LABEL[code] ??
    LOCAL_CODE_LABEL[code] ??
    code
  );
}

/** 账号级错误：服务端收到后会暂停账号执行（契约 §5 结果处理表）。插件随后结束本批次。 */
export const ACCOUNT_LEVEL_CODES: ReadonlySet<string> = new Set([
  "LOGIN_REQUIRED",
  "ACCOUNT_RESTRICTED",
  "CARD_DECLINED",
  "CAPTCHA_ENCOUNTERED",
  "IDENTITY_MISMATCH",
]);

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

/** 批次心跳 `stop_reason` 的中文名。 */
export const STOP_REASON_LABEL: Record<string, string> = {
  account_paused: "账号已被暂停执行",
  lease_expired: "执行租约已过期",
  device_revoked: "设备授权已被撤销",
  swept: "批次已被服务端清扫",
};

/** 输入：停止原因码 → 输出：中文说明。 */
export function stopReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "服务端要求停止";
  return STOP_REASON_LABEL[reason] ?? reason;
}
