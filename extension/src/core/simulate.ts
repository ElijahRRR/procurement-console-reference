/** 模拟执行（第一个 PR 只做模拟）：根据尝试载荷推出一份「结算页事实」。
 *
 * **模拟驱动从不导航、点击或修改亚马逊页面。** 这里只有纯函数：输入是服务端下发的
 * 尝试载荷，输出是一份形状与真实结算页上报完全相同的事实，交给服务端护栏裁决。
 * 这样服务端的护栏、提交登记、结果处理、额度与状态机都走真实代码路径，
 * 只有「读页面」这一步是推出来的。
 *
 * 金额全部用整数「分」计算，不过浮点：`41.97 × 3` 在浮点里不等于 `125.91`。
 * 读不到的金额保持 null，交给服务端按 DATA_UNRELIABLE 拦截 —— **不当成 0**。
 */

import type { Attempt, CheckoutFacts, ResultIn } from "./types.js";
import { addDays, bjCompact, laDate } from "./time.js";

/** 模拟结局（设置页可选，默认 pass）。 */
export type SimOutcome =
  | "pass"
  | "price_over_cap"
  | "delivery_too_long"
  | "out_of_stock"
  | "page_load_failed"
  | "unknown_after_submit"
  | "account_captcha";

export const SIM_OUTCOMES: ReadonlyArray<{ value: SimOutcome; label: string; hint: string }> = [
  { value: "pass", label: "正常通过（默认）", hint: "护栏放行、提交登记、上报成功，单号形如 SIM-日期-尝试号" },
  { value: "price_over_cap", label: "结算总额超过限价", hint: "总额 = 限价 + 1 美元，服务端护栏应拦截 PRICE_OVER_CAP" },
  { value: "delivery_too_long", label: "配送时长超过上限", hint: "送达日期 = 洛杉矶今天 + 上限 + 1 天，应拦截 DELIVERY_TOO_LONG" },
  { value: "out_of_stock", label: "商品缺货", hint: "护栏之前上报失败 OUT_OF_STOCK，任务转异常" },
  { value: "page_load_failed", label: "页面加载失败", hint: "上报 PAGE_LOAD_FAILED：首次服务端自动重试一次，第二次转异常" },
  { value: "unknown_after_submit", label: "提交后结果不明", hint: "提交登记后上报 unknown：任务进结果待核实，账号暂停" },
  { value: "account_captcha", label: "遇到验证码", hint: "上报 CAPTCHA_ENCOUNTERED：账号暂停，任务保留" },
];

const OUTCOME_SET: ReadonlySet<string> = new Set(SIM_OUTCOMES.map((o) => o.value));

/** 输入：存下来的任意值 → 输出：合法的模拟结局；认不出按 pass。 */
export function normalizeOutcome(v: unknown): SimOutcome {
  return OUTCOME_SET.has(String(v)) ? (v as SimOutcome) : "pass";
}

/** 输入：金额（字符串 "41.97"、数字 41.97）→ 输出：整数分；读不出返回 null（不当成 0）。 */
export function toCents(v: unknown): number | null {
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return null;
    return Math.round(v * 100);
  }
  if (typeof v !== "string") return null;
  const s = v.trim().replace(/,/g, "").replace(/^\$/, "");
  const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(s);
  if (!m) return null;
  const cents = Number(m[2]) * 100 + Number((m[3] ?? "").padEnd(2, "0") || "0");
  return m[1] === "-" ? -cents : cents;
}

/** 输入：整数分 → 输出：两位小数的金额字符串；null 原样返回 null。 */
export function centsToStr(c: number | null): string | null {
  if (c === null || !Number.isFinite(c)) return null;
  const neg = c < 0;
  const abs = Math.abs(Math.round(c));
  const s = `${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
  return neg ? `-${s}` : s;
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** 输入：尝试载荷 → 输出：任务邮编；平铺（`postal_code`）与嵌套（`address` / `ship_to` /
 *  `recipient` / `shipping`）都认。读不到返回 null。 */
export function attemptPostalCode(a: Attempt): string | null {
  const raw = a.raw;
  const direct = raw.postal_code;
  if (typeof direct === "string" && direct.trim()) return direct.trim();
  for (const k of ["address", "ship_to", "recipient", "shipping"]) {
    const o = raw[k];
    if (isObj(o)) {
      const v = o.postal_code ?? o.zip ?? o.postcode;
      if (typeof v === "string" && v.trim()) return v.trim();
    }
  }
  return null;
}

/** 输入：尝试载荷、模拟结局、当前时刻 → 输出：模拟的结算页事实（契约 §4 的形状）。
 *
 *  正常（pass）：
 *    subtotal = unit_price × quantity；shipping = 0；tax = 0；total = subtotal；
 *    送达日期 = 洛杉矶今天 + min(3, 配送上限)；邮编取任务的；卡尾号取期望卡尾号或 null；
 *    币种 USD；配送方 amazon。
 *  price_over_cap：total = 限价 + 1（限价读不到则 total 为 null，服务端按数据不可靠拦截）。
 *  delivery_too_long：送达日期 = 洛杉矶今天 + 配送上限 + 1（上限读不到则日期为 null）。 */
export function buildCheckoutFacts(a: Attempt, outcome: SimOutcome, now: number): CheckoutFacts {
  const unit = toCents(a.unit_price);
  const qty = a.quantity !== null && Number.isInteger(a.quantity) && a.quantity > 0 ? a.quantity : null;
  const subtotal = unit !== null && qty !== null ? unit * qty : null;

  let total = subtotal;
  if (outcome === "price_over_cap") {
    const cap = toCents(a.price_cap);
    total = cap === null ? null : cap + 100;
  }

  const today = laDate(now);
  const maxDays = a.max_delivery_days;
  let deliveryDate: string | null;
  if (outcome === "delivery_too_long") {
    deliveryDate = maxDays === null ? null : addDays(today, maxDays + 1);
  } else {
    // 上限没下发时按 3 天模拟（上限缺失本身由服务端护栏判定，不在插件里补 0）。
    const days = maxDays === null ? 3 : Math.min(3, maxDays);
    deliveryDate = addDays(today, days);
  }

  return {
    items: [{ asin: a.asin, quantity: qty, unit_price: centsToStr(unit) }],
    subtotal: centsToStr(subtotal),
    shipping: "0.00",
    tax: "0.00",
    total_before_tax: centsToStr(subtotal),
    total: centsToStr(total),
    gift_card_amount: null,
    currency: "USD",
    delivery: {
      text: deliveryDate ? `模拟：预计 ${deliveryDate} 送达` : "模拟：没有送达日期",
      date: deliveryDate,
      days: null,
    },
    ship_to: { postal_code: attemptPostalCode(a) },
    card_last4: a.expected_card_last4 ?? null,
    fulfillment: "amazon",
  };
}

/** 输入：尝试 ID、当前时刻 → 输出：模拟单号 `SIM-<北京日期 YYYYMMDD>-<尝试 ID>`。 */
export function simOrderNo(attemptId: number, now: number): string {
  return `SIM-${bjCompact(now)}-${attemptId}`;
}

/** 输入：结算事实与模拟单号 → 输出：成功结果的上报体。 */
export function successResult(facts: CheckoutFacts, orderNo: string): ResultIn {
  return {
    outcome: "succeeded",
    amazon_order_no: orderNo,
    amounts: {
      subtotal: facts.subtotal,
      shipping: facts.shipping,
      tax: facts.tax,
      total_before_tax: facts.total_before_tax,
      total: facts.total,
    },
    card_last4: facts.card_last4,
    delivery: { ...facts.delivery },
    error_code: null,
    error_detail: null,
  };
}

/** 输入：结局（failed / unknown）、错误码、说明 → 输出：失败或不明结果的上报体。 */
export function failureResult(outcome: "failed" | "unknown", code: string | null, detail: string | null): ResultIn {
  return {
    outcome,
    amazon_order_no: null,
    amounts: null,
    card_last4: null,
    delivery: null,
    error_code: code,
    error_detail: detail,
  };
}
