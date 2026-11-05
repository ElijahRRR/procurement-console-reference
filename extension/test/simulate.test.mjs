/** 模拟结算事实：金额按整数分计算、读不到保持 null（不当成 0）、各模拟结局的取值。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { parseAttempt } from "../build/core/client.js";
import {
  attemptPostalCode,
  buildCheckoutFacts,
  centsToStr,
  normalizeOutcome,
  simOrderNo,
  toCents,
} from "../build/core/simulate.js";
import {
  DEFAULT_SERVER_URL,
  isValidPairingCode,
  normalizePairingCode,
  normalizeSettings,
  validateServerUrl,
} from "../build/core/settings.js";
import { appendLog } from "../build/core/log.js";
import { codeLabel } from "../build/core/codes.js";

const NOW = Date.parse("2026-11-05T06:00:00Z"); // 北京 11-05 14:00；洛杉矶 11-04 22:00
const attempt = (o = {}) =>
  parseAttempt({
    attempt_id: 7, upstream_order_no: "PO-DEMO-0001", asin: "B0DEMO0001", quantity: 3, unit_price: "41.97",
    price_cap: "130.00", max_delivery_days: 10, postal_code: "32960-1234", expected_card_last4: "0000", simulated: true, ...o,
  });

test("金额按整数分：41.97 × 3 = 125.91（浮点里不相等）", () => {
  const f = buildCheckoutFacts(attempt(), "pass", NOW);
  assert.equal(f.subtotal, "125.91");
  assert.equal(f.total, "125.91");
  assert.equal(f.total_before_tax, "125.91");
  assert.equal(f.shipping, "0.00");
  assert.equal(f.tax, "0.00");
  assert.equal(f.currency, "USD");
  assert.equal(f.fulfillment, "amazon");
  assert.equal(f.gift_card_amount, null);
  assert.deepEqual(f.items, [{ asin: "B0DEMO0001", quantity: 3, unit_price: "41.97" }]);
  assert.equal(f.ship_to.postal_code, "32960-1234");
  assert.equal(f.card_last4, "0000");
});

test("送达日期 = 洛杉矶今天 + min(3, 上限)", () => {
  assert.equal(buildCheckoutFacts(attempt(), "pass", NOW).delivery.date, "2026-11-07");
  assert.equal(buildCheckoutFacts(attempt({ max_delivery_days: 2 }), "pass", NOW).delivery.date, "2026-11-06");
  assert.equal(buildCheckoutFacts(attempt({ max_delivery_days: 10 }), "delivery_too_long", NOW).delivery.date, "2026-11-15");
});

test("超限价：total = 限价 + 1；限价读不到时 total 为 null（交给服务端按数据不可靠拦截）", () => {
  assert.equal(buildCheckoutFacts(attempt(), "price_over_cap", NOW).total, "131.00");
  assert.equal(buildCheckoutFacts(attempt({ price_cap: null }), "price_over_cap", NOW).total, null);
});

test("读不到的值保持 null，不当成 0", () => {
  const f = buildCheckoutFacts(attempt({ unit_price: null, quantity: null, expected_card_last4: null, postal_code: null }), "pass", NOW);
  assert.equal(f.subtotal, null);
  assert.equal(f.total, null);
  assert.equal(f.items[0].quantity, null);
  assert.equal(f.card_last4, null);
  assert.equal(f.ship_to.postal_code, null);
  assert.equal(buildCheckoutFacts(attempt({ max_delivery_days: null }), "delivery_too_long", NOW).delivery.date, null);
});

test("邮编：平铺与嵌套的收件信息都认", () => {
  assert.equal(attemptPostalCode(attempt({ postal_code: undefined, address: { postal_code: "10001" } })), "10001");
  assert.equal(attemptPostalCode(attempt({ postal_code: undefined, ship_to: { zip: "94105" } })), "94105");
  assert.equal(attemptPostalCode(attempt({ postal_code: "  " })), null);
});

test("模拟单号 SIM-<北京日期>-<尝试号>：北京已过零点时用新日期", () => {
  assert.equal(simOrderNo(7, NOW), "SIM-20261105-7");
  assert.equal(simOrderNo(8, Date.parse("2026-11-05T16:30:00Z")), "SIM-20261106-8");
});

test("toCents / centsToStr", () => {
  assert.equal(toCents("41.97"), 4197);
  assert.equal(toCents("41.9"), 4190);
  assert.equal(toCents("$1,234.5"), 123450);
  assert.equal(toCents(41.97), 4197);
  assert.equal(toCents("abc"), null);
  assert.equal(toCents(""), null);
  assert.equal(toCents(null), null);
  assert.equal(centsToStr(5), "0.05");
  assert.equal(centsToStr(-150), "-1.50");
  assert.equal(centsToStr(null), null);
  assert.equal(normalizeOutcome("price_over_cap"), "price_over_cap");
  assert.equal(normalizeOutcome("drop_table"), "pass");
});

test("设置：服务端地址只允许本机；配对码 8 位", () => {
  assert.deepEqual(validateServerUrl("http://127.0.0.1:8790"), { ok: true });
  assert.deepEqual(validateServerUrl("http://localhost:9000/"), { ok: true });
  assert.equal(validateServerUrl("http://192.168.1.20:8790").ok, false);
  assert.equal(validateServerUrl("http://127.0.0.1:8790/api").ok, false);
  assert.equal(validateServerUrl("ftp://127.0.0.1").ok, false);
  assert.equal(validateServerUrl("nonsense").ok, false);
  assert.equal(normalizeSettings({ serverUrl: "http://evil.example" }).serverUrl, DEFAULT_SERVER_URL);
  assert.equal(normalizeSettings(null).simulationOutcome, "pass");
  assert.equal(normalizePairingCode(" AB12-CD34 "), "AB12CD34");
  assert.equal(isValidPairingCode("AB12-CD34"), true);
  assert.equal(isValidPairingCode("AB12CD3"), false);
});

test("日志只保留最近 50 行；错误码都有中文说明", () => {
  let lines = [];
  for (let i = 0; i < 60; i++) lines = appendLog(lines, { at: i, level: "info", text: `第 ${i} 行` });
  assert.equal(lines.length, 50);
  assert.equal(lines[0].text, "第 10 行");
  assert.match(codeLabel("ACCOUNT_BUSY"), /其他设备/);
  assert.match(codeLabel("PRICE_OVER_CAP"), /限价/);
  assert.equal(codeLabel("SOMETHING_NEW"), "SOMETHING_NEW");
});
