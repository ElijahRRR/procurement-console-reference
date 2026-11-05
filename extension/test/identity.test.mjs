/** 身份识别：导航栏登录状态与 customerId（移植的解析器对着移植的夹具），以及身份快照工具。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHTML } from "linkedom";

import { isSignInUrl, readCustomerId, readLoginState } from "../build/dom/parse.js";
import {
  IDENTITY_STATUS_TEXT,
  emptyIdentityStore,
  identityFromPage,
  isFresh,
  isOrderHistoryPath,
  normalizeIdentityStore,
  observeIdentity,
  reconcileIdentity,
  retireIdentityTab,
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

// ── 多个标签页的身份读数对账（按页面加载先后）──────────────────────────────

const T0 = Date.parse("2026-11-05T01:00:00Z");
const BOUND = "A1DEMO0000";
const OTHER = "A9OTHER0000";
const obs = (tab, cid, loadedMin, readMin = loadedMin) => ({
  tab_id: tab,
  signed_in: cid !== null,
  customer_id: cid,
  page_url: "https://www.amazon.com/gp/css/order-history",
  loaded_at: T0 + loadedMin * 60_000,
  read_at: T0 + readMin * 60_000,
});
const fold = (...steps) => steps.reduce((s, f) => f(s), emptyIdentityStore());
const see = (o) => (s) => observeIdentity(s, o);
const gone = (tab) => (s) => retireIdentityTab(s, tab);

test("旧标签页（绑定账号）+ 新标签页（另一个买家号）：报另一个买家号、有分歧；旧页面重读不能翻回绑定账号", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)));
  const r = reconcileIdentity(s, BOUND, T0 + 61 * 60_000);
  assert.equal(r.customer_id, OTHER);
  assert.equal(r.conflict, true);
  assert.equal(r.source_tab, 2);
  // 用户回到旧标签页点开始：旧页面（加载时刻没变）重读一次
  const s2 = observeIdentity(s, obs(1, BOUND, 0, 62));
  const r2 = reconcileIdentity(s2, BOUND, T0 + 62 * 60_000);
  assert.equal(r2.customer_id, OTHER, "重读不改变加载先后");
  assert.equal(r2.conflict, true);
});

test("新标签页里登录了另一个买家号又关掉：旧标签页不能因此算一致，直到它重新加载", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)), gone(2));
  assert.equal(Object.keys(s.live).length, 1);
  assert.equal(s.newest_gone.tab_id, 2, "最新加载的那一份留作证据");
  const r = reconcileIdentity(s, BOUND, T0 + 70 * 60_000);
  assert.equal(r.customer_id, OTHER);
  assert.equal(r.conflict, true);
  // 旧标签页刷新（新的加载时刻），而浏览器其实已经换回绑定账号：分歧消除，报绑定账号
  const s2 = observeIdentity(s, obs(1, BOUND, 80));
  assert.equal(s2.newest_gone, null, "比它新的开着的页面出现后，已关闭的读数作废");
  const r2 = reconcileIdentity(s2, BOUND, T0 + 80 * 60_000);
  assert.equal(r2.customer_id, BOUND);
  assert.equal(r2.conflict, false);
});

test("分歧时绝不报「是绑定账号」：新页面是绑定账号、旧页面是别人，也按别人报（刷新或关掉旧页面后才一致）", () => {
  const s = fold(see(obs(1, OTHER, 0)), see(obs(2, BOUND, 60)));
  const r = reconcileIdentity(s, BOUND, T0 + 61 * 60_000);
  assert.equal(r.customer_id, OTHER);
  assert.equal(r.conflict, true);
  const r2 = reconcileIdentity(retireIdentityTab(s, 1), BOUND, T0 + 62 * 60_000);
  assert.equal(r2.customer_id, BOUND, "旧页面关掉（且比新页面旧）之后一致");
  assert.equal(r2.conflict, false);
});

test("任一标签页显示未登录（例如停在 /ap/signin 的重新验证页）：报未登录（安全的一侧），关掉它之后仍保留到下一次页面加载", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, null, 30)));
  const r = reconcileIdentity(s, BOUND, T0 + 31 * 60_000);
  assert.equal(r.signed_in, false);
  assert.equal(r.customer_id, null);
  assert.equal(r.conflict, true);
  const closed = retireIdentityTab(s, 2);
  assert.equal(reconcileIdentity(closed, BOUND, T0 + 32 * 60_000).signed_in, false, "最新加载的读数说未登录");
  const reloaded = observeIdentity(closed, obs(1, BOUND, 40));
  const r3 = reconcileIdentity(reloaded, BOUND, T0 + 40 * 60_000);
  assert.equal(r3.signed_in, true);
  assert.equal(r3.conflict, false);
});

test("已登录但没读到买家号 ID 的页面是弱证据：不和读到 ID 的页面制造分歧", () => {
  const s = fold(see(obs(1, BOUND, 0)), see({ ...obs(2, BOUND, 10), customer_id: null }));
  const r = reconcileIdentity(s, BOUND, T0 + 11 * 60_000);
  assert.equal(r.conflict, false);
  assert.equal(r.customer_id, BOUND, "有 ID 的读数优先");
});

test("同一标签页：较早加载的页面迟到的读数不能覆盖较新页面的；新鲜度取最近一次读取，不取加载时刻", () => {
  let s = fold(see(obs(1, BOUND, 50)));
  s = observeIdentity(s, obs(1, OTHER, 10, 51)); // 刷新之前那一页的消息迟到了
  assert.equal(s.live["1"].customer_id, BOUND);
  const r = reconcileIdentity(observeIdentity(s, obs(1, BOUND, 50, 55)), BOUND, T0 + 56 * 60_000);
  assert.equal(r.at, T0 + 55 * 60_000);
  assert.equal(isFresh(r, T0 + 56 * 60_000), true);
  // 只剩一个已关闭的页面：它的读数不再被重读，新鲜度随时间衰减（心跳不再捎带）
  const lone = reconcileIdentity(retireIdentityTab(s, 1), BOUND, T0 + 120 * 60_000);
  assert.equal(isFresh(lone, T0 + 120 * 60_000), false);
});

test("对账：没有读数为 null；存储被改坏时退回空仓库", () => {
  assert.equal(reconcileIdentity(emptyIdentityStore(), BOUND, T0), null);
  assert.deepEqual(normalizeIdentityStore({ live: { 1: { tab_id: "x" } }, newest_gone: 5 }), emptyIdentityStore());
  assert.deepEqual(normalizeIdentityStore(null), emptyIdentityStore());
});
