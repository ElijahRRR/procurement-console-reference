/** 模拟结算事实：金额按整数分计算、读不到保持 null（不当成 0）、各模拟结局的取值。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { parseAttempt } from "../build/core/client.js";
import {
  attemptPostalCode,
  buildCheckoutFacts,
  centsToStr,
  normalizeOutcome,
  SIM_OUTCOMES,
  simOrderNo,
  toCents,
} from "../build/core/simulate.js";
import {
  DEFAULT_SERVER_URL,
  DEFAULT_SETTINGS,
  ORDER_HISTORY_URL,
  consoleAccountsUrl,
  normalizeSettings,
  normalizeWorkspaceId,
  validateServerUrl,
  validateWorkspaceIdText,
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

test("结算事实带买家号 ID：取事实那一刻页面上的读数；读不到保持 null（服务端按数据不可靠拦截），不拿别的值顶替", () => {
  assert.equal(buildCheckoutFacts(attempt(), "pass", NOW, "A1DEMO00000").customer_id, "A1DEMO00000");
  assert.equal(buildCheckoutFacts(attempt(), "pass", NOW, null).customer_id, null);
  assert.equal(buildCheckoutFacts(attempt(), "pass", NOW).customer_id, null, "没传就是没读到");
  assert.equal(buildCheckoutFacts(attempt(), "price_over_cap", NOW, "A2TEST00000").customer_id, "A2TEST00000");
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

test("模拟单号 SIM-<北京日期>-<尝试 ID>：北京已过零点时用新日期", () => {
  assert.equal(simOrderNo(7, NOW), "SIM-20261105-7");
  assert.equal(simOrderNo(8, Date.parse("2026-11-05T16:30:00Z")), "SIM-20261106-8");
});

test("模拟单号的说明写「尝试 ID」：单号末尾是尝试 ID（attempt_id），不是「第 N 次尝试」的序号", () => {
  assert.match(SIM_OUTCOMES.find((o) => o.value === "pass").hint, /单号形如 SIM-日期-尝试 ID/);
  for (const f of ["../src/core/simulate.ts", "../src/options/options.ts", "../README.md"]) {
    assert.doesNotMatch(readFileSync(new URL(f, import.meta.url), "utf8"), /尝试号/, f);
  }
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

test("设置：零设置可用（服务端地址缺省 http://127.0.0.1:8790）；地址只允许本机；工作区 ID 是可选的正整数；没有配对码、设备名称、浏览器类型", () => {
  assert.deepEqual(DEFAULT_SETTINGS, { serverUrl: "http://127.0.0.1:8790", simulationOutcome: "pass", workspaceId: null });
  assert.deepEqual(normalizeSettings(undefined), DEFAULT_SETTINGS);
  assert.deepEqual(Object.keys(normalizeSettings({ serverUrl: "http://localhost:9000/", deviceName: "x", browser: "chrome", code: "AB12CD34" })).sort(), ["serverUrl", "simulationOutcome", "workspaceId"]);
  assert.deepEqual(validateServerUrl("http://127.0.0.1:8790"), { ok: true });
  assert.deepEqual(validateServerUrl("http://localhost:9000/"), { ok: true });
  assert.equal(validateServerUrl("http://192.168.1.20:8790").ok, false);
  assert.equal(validateServerUrl("http://127.0.0.1:8790/api").ok, false);
  assert.equal(validateServerUrl("ftp://127.0.0.1").ok, false);
  assert.equal(validateServerUrl("nonsense").ok, false);
  // 永远连不上的地址一律不收（保存它会白白换掉令牌）：清单只有 http 的主机权限；fetch 拒绝带用户名密码的地址；
  // 查询串、片段会让请求拼成「/?x=1/plugin/v1/…」。只写一个 ? 或 # 时 URL 解析出的 search、hash 是空的，所以看文本。
  for (const [u, reason] of [
    ["http://127.0.0.1:8790?x=1", "只填协议、主机和端口，不要带路径"],
    ["http://127.0.0.1:8790#frag", "只填协议、主机和端口，不要带路径"],
    ["http://127.0.0.1:8790?", "只填协议、主机和端口，不要带路径"],
    ["http://127.0.0.1:8790#", "只填协议、主机和端口，不要带路径"],
    ["http://127.0.0.1:8790?@attacker.example/", "只填协议、主机和端口，不要带路径"],
    // 反斜杠：URL 解析当成「/」（路径看着是「/」），请求却拼成「//plugin/v1/…」，服务端答「接口不存在」
    ["http://127.0.0.1:8790\\", "只填协议、主机和端口，不要带路径"],
    ["http://localhost:8790\\/", "只填协议、主机和端口，不要带路径"],
    ["http://user:pw@127.0.0.1:8790", "地址里不要带用户名或密码"],
    ["http://user@localhost:8790", "地址里不要带用户名或密码"],
    ["https://localhost:8790", "只支持 http 地址，例如 http://127.0.0.1:8790"],
    ["https://127.0.0.1", "只支持 http 地址，例如 http://127.0.0.1:8790"],
  ]) {
    assert.deepEqual(validateServerUrl(u), { ok: false, reason }, u);
  }
  assert.deepEqual(validateServerUrl(" http://localhost:8790/ "), { ok: true }, "首尾空白与末尾斜杠照旧去掉");
  assert.equal(normalizeSettings({ serverUrl: "http://127.0.0.1:8790?x=1" }).serverUrl, DEFAULT_SERVER_URL, "存下的坏地址读出来是缺省地址");
  assert.equal(normalizeSettings({ serverUrl: "http://evil.example" }).serverUrl, DEFAULT_SERVER_URL);
  assert.equal(normalizeSettings(null).simulationOutcome, "pass");
  for (const [v, want] of [[2, 2], ["7", 7], [" 12 ", 12], ["", null], [null, null], [0, null], [-3, null], [1.5, null], ["abc", null], ["1e3", null]]) {
    assert.equal(normalizeWorkspaceId(v), want, JSON.stringify(v));
  }
  assert.equal(normalizeSettings({ workspaceId: "5" }).workspaceId, 5);
  assert.deepEqual(validateWorkspaceIdText("  "), { ok: true, value: null }, "留空可以");
  assert.deepEqual(validateWorkspaceIdText("3"), { ok: true, value: 3 });
  assert.equal(validateWorkspaceIdText("x3").ok, false);
  assert.equal(consoleAccountsUrl("http://127.0.0.1:8790/"), "http://127.0.0.1:8790/accounts", "「打开控制台」= 服务端地址 + /accounts");
  assert.equal(ORDER_HISTORY_URL, "https://www.amazon.com/gp/css/order-history");
});

test("日志只保留最近 50 行；错误码都有中文说明", () => {
  let lines = [];
  for (let i = 0; i < 60; i++) lines = appendLog(lines, { at: i, level: "info", text: `第 ${i} 行` });
  assert.equal(lines.length, 50);
  assert.equal(lines[0].text, "第 10 行");
  assert.match(codeLabel("ACCOUNT_BUSY"), /另一个浏览器/);
  assert.match(codeLabel("DEVICE_BUSY"), /这个浏览器上一个批次还没有结束/);
  assert.match(codeLabel("PRICE_OVER_CAP"), /限价/);
  assert.equal(codeLabel("SOMETHING_NEW"), "其他原因（插件不认识这个错误码）", "认不出的码也不原样显示");
});
