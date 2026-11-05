/** 身份识别：导航栏登录状态与 customerId（移植的解析器对着移植的夹具），以及身份快照工具。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHTML } from "linkedom";

import { isSignInUrl, readCustomerId, readLoginState } from "../build/dom/parse.js";
import {
  IDENTITY_STATUS_TEXT,
  identityFromPage,
  isFresh,
  isOrderHistoryPath,
  sameIdentity,
  sanitizePageUrl,
  toIdentity,
} from "../build/core/identity.js";

const fixture = (name) => parseHTML(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf8")).document;
const html = (s) => parseHTML(`<!doctype html><html><head></head><body>${s}</body></html>`).document;

test("已登录夹具：隐藏的「未登录」模板排在前面也读成 ok；customerId 跳过三个假值", () => {
  const doc = fixture("nav-signed-in.html");
  // 干扰项自己也得在：隐藏模板确实排在真导航栏前面
  const links = [...doc.querySelectorAll("#nav-link-accountList")];
  assert.equal(links.length, 2);
  assert.match(links[0].getAttribute("href"), /\/ap\/signin/);
  assert.equal(readLoginState(doc, "https://www.amazon.com/gp/cart/view.html"), "ok");
  assert.equal(readCustomerId(doc), "A3DEMO7PLQ92XY");
  const h = doc.documentElement.innerHTML;
  assert.ok(h.indexOf('customerId: ""') < h.indexOf("'customerId': 'A3DEMO7PLQ92XY'"), "空串假值确实排在真值前面");
});

test("已登出夹具：隐藏的「已登录」模板（连 signout 都有）不能把结论翻成 ok", () => {
  const doc = fixture("nav-signed-out.html");
  assert.ok(doc.querySelector('[style*="display:none"] #nav-item-signout'), "干扰项确实存在");
  assert.equal(readLoginState(doc, "https://www.amazon.com/gp/cart/view.html"), "signed_out");
  assert.equal(readCustomerId(doc), undefined);
});

test("URL 落在 /ap/signin：哪怕 DOM 是一整套已登录导航栏也是 signed_out", () => {
  const doc = fixture("nav-signed-in.html");
  assert.equal(readLoginState(doc, "https://www.amazon.com/ap/signin?openid.pape=x"), "signed_out");
});

test("没有导航栏 → unknown，不兜底成 ok", () => {
  assert.equal(readLoginState(html("<div id='checkout'>Place your order</div>"), "https://www.amazon.com/gp/buy/spc"), "unknown");
});

test("登录状态边界：隐藏浮层里的 signout 算已登录；问候语 sign in 是否决票；认不出的入口不猜", () => {
  const u = "https://www.amazon.com/";
  assert.equal(readLoginState(html('<div style="display:none"><a id="nav-item-signout" href="#">Sign Out</a></div>'), u), "ok");
  assert.equal(
    readLoginState(html('<span id="nav-link-accountList-nav-line-1">Hello, sign in</span><a id="nav-item-signout" href="#">Sign Out</a>'), u),
    "signed_out",
  );
  assert.equal(readLoginState(html('<a id="nav-link-accountList" href="/gp/something-new"></a>'), u), "unknown");
  assert.equal(
    readLoginState(html('<a id="nav-link-accountList" href="/gp/x"><span id="nav-link-accountList-nav-line-1">你好，Demo</span></a>'), u),
    "unknown",
    "非英文问候语判不出就是判不出",
  );
  assert.equal(readLoginState(html('<a id="nav-link-accountList" href="/gp/css/homepage.html?ref_=nav_ya"></a>'), u), "ok");
  assert.equal(readLoginState(html('<a id="nav-link-accountList" href="/ap/signin?openid.pape=0"></a>'), u), "signed_out");
});

test("customerId：三种写法都认；空串、非 A 开头、太短、小写都不算", () => {
  const cid = (s) => readCustomerId(html(s));
  assert.equal(cid('<script>var x = { customerId: "A1DEMO0000" };</script>'), "A1DEMO0000");
  assert.equal(cid("<script>var x = { 'customerId': 'A9DEMO8Y7X6' };</script>"), "A9DEMO8Y7X6");
  assert.equal(cid('<script>{"customerId": "ADEMOQQQWW1"}</script>'), "ADEMOQQQWW1");
  assert.equal(cid('<script>customerId: ""</script>'), undefined);
  assert.equal(cid('<script>customerId: "1234567890"</script>'), undefined);
  assert.equal(cid('<script>customerId: "A12"</script>'), undefined);
  assert.equal(cid('<script>customerId: "a1b2c3d4e5"</script>'), undefined);
  assert.equal(cid("<p>nothing here</p>"), undefined);
  assert.equal(cid(`<script>var a={customerId:""},b={"customerId":"0"},c={ 'customerId': 'ADEMOREAL12' };</script>`), "ADEMOREAL12");
});

test("isSignInUrl：只认登录页；空串与 null 不算", () => {
  assert.equal(isSignInUrl("https://www.amazon.com/ap/signin?openid.pape=x"), true);
  assert.equal(isSignInUrl("https://www.amazon.com/ap/signin/attempt"), true);
  assert.equal(isSignInUrl("https://www.amazon.com/gp/cart/view.html"), false);
  assert.equal(isSignInUrl(""), false);
  assert.equal(isSignInUrl(null), false);
});

test("page_url 只留 origin + pathname，不带查询串与片段", () => {
  assert.equal(
    sanitizePageUrl("https://www.amazon.com/gp/your-account/order-details?orderID=000-0000000-0000000&ref=x#frag"),
    "https://www.amazon.com/gp/your-account/order-details",
  );
  assert.equal(sanitizePageUrl("not a url"), null);
  assert.equal(sanitizePageUrl("javascript:alert(1)"), null);
  assert.equal(sanitizePageUrl(""), null);
});

test("身份快照：unknown 不上报；登出时不带 customer_id；只比登录状态与 ID", () => {
  const now = Date.parse("2026-11-05T06:00:00Z");
  assert.equal(identityFromPage("unknown", "A1DEMO0000", "https://www.amazon.com/", now), null);
  const out = identityFromPage("signed_out", "A1DEMO0000", "https://www.amazon.com/gp/cart/view.html?x=1", now);
  assert.deepEqual(toIdentity(out), { signed_in: false, customer_id: null, site: "US", page_url: "https://www.amazon.com/gp/cart/view.html" });
  const a = identityFromPage("ok", "A1DEMO0000", "https://www.amazon.com/a", now);
  const b = identityFromPage("ok", "A1DEMO0000", "https://www.amazon.com/b", now + 5000);
  assert.equal(sameIdentity(a, b), true);
  assert.equal(sameIdentity(a, out), false);
  assert.equal(isFresh(a, now + 4 * 60_000), true);
  assert.equal(isFresh(a, now + 6 * 60_000), false, "几分钟前读的快照不能当成新上报");
  assert.equal(isFresh(null, now), false);
});

test("订单历史页识别；四种身份状态都有中文说明", () => {
  for (const p of ["/your-orders", "/your-orders/orders", "/gp/css/order-history", "/gp/css/order-history/ref=x", "/gp/your-account/order-history"]) {
    assert.equal(isOrderHistoryPath(p), true, p);
  }
  for (const p of ["/", "/dp/B0DEMO0001", "/gp/cart/view.html", "/your-ordersx", null]) {
    assert.equal(isOrderHistoryPath(p), false, String(p));
  }
  for (const k of ["match", "mismatch", "signed_out", "unknown"]) assert.ok(IDENTITY_STATUS_TEXT[k].short);
});
