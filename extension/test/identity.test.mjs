/** 身份识别：导航栏登录状态与 customerId（解析器对着夹具，含往标题、输入框、商品标题里植入 customerId 的夹具），
 *  身份快照工具，以及多标签页读数的对账（基准 = 进行中批次的买家号 ID，或没有）。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parseHTML } from "linkedom";

import { isSignInUrl, readCustomerId, readLoginState } from "../build/dom/parse.js";
import {
  emptyIdentityStore,
  identityFromPage,
  isFresh,
  isOrderHistoryPath,
  normalizeIdentityStore,
  observeIdentity,
  reconcileIdentity,
  retireIdentityTab,
  sameIdentity,
  maskCustomerId,
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

test("customerId：参考项目的两种写法与键上带引号的写法都认；空串、非 A 开头、太短、小写都不算", () => {
  const cid = (s) => readCustomerId(html(s));
  // 参考项目（小蜜蜂）依次试的两条：customerId: "…" 与 customerId: '…'
  assert.equal(cid('<script>var x = { customerId: "A1DEMO00000" };</script>'), "A1DEMO00000");
  assert.equal(cid("<script>var x = { customerId: 'A1DEMO00000' };</script>"), "A1DEMO00000");
  // 键上带引号的写法
  assert.equal(cid("<script>var x = { 'customerId': 'A9DEMO8Y7X6' };</script>"), "A9DEMO8Y7X6");
  assert.equal(cid('<script>{"customerId": "ADEMOQQQWW1"}</script>'), "ADEMOQQQWW1");
  assert.equal(cid('<script type="application/json">{"customerId":"A2TEST00000"}</script>'), "A2TEST00000", "JSON 数据块也是内联脚本");
  assert.equal(cid('<script>customerId: ""</script>'), undefined);
  assert.equal(cid('<script>customerId: "1234567890"</script>'), undefined);
  assert.equal(cid('<script>customerId: "A12"</script>'), undefined);
  assert.equal(cid('<script>customerId: "a1b2c3d4e5"</script>'), undefined);
  assert.equal(cid("<p>nothing here</p>"), undefined);
  assert.equal(cid(`<script>var a={customerId:""},b={"customerId":"0"},c={ 'customerId': 'ADEMOREAL12' };</script>`), "ADEMOREAL12");
});

test("customerId 只在内联脚本里找：正文、属性、注释、标题里的写法一律不读", () => {
  const cid = (s) => readCustomerId(html(s));
  assert.equal(cid("<p>customerId: 'A9PLANT00003'</p>"), undefined, "正文");
  assert.equal(cid('<input value="customerId: \'A9PLANT00002\'">'), undefined, "输入框的 value");
  assert.equal(cid("<div data-x=\"customerId: 'A9PLANT00004'\"></div>"), undefined, "属性");
  assert.equal(cid("<!-- customerId: 'A9PLANT00005' -->"), undefined, "注释");
  assert.equal(readCustomerId(parseHTML("<!doctype html><html><head><title>customerId: 'A9PLANT00001'</title></head><body></body></html>").document), undefined, "标题");
  // 正文里有植入、内联脚本里有真值：读出真值
  assert.equal(cid("<h2>Demo customerId: 'A9PLANT00003'</h2><script>var c = { customerId: 'A1DEMO00000' };</script>"), "A1DEMO00000");
});

test("customerId：所有形状合格的匹配必须是同一个值 —— 出现第二个值就当没读到，不猜哪个是真的", () => {
  const cid = (s) => readCustomerId(html(s));
  assert.equal(cid("<script>a = { customerId: 'A1DEMO00000' }; b = { customerId: \"A1DEMO00000\" };</script>"), "A1DEMO00000", "同一个值出现两次算一致");
  assert.equal(cid("<script>a = { customerId: 'A1DEMO00000' };</script><script>b = { 'customerId': 'A1DEMO00000' };</script>"), "A1DEMO00000", "跨脚本也算");
  assert.equal(cid("<script>a = { customerId: 'A1DEMO00000' }; b = { customerId: 'A2TEST00000' };</script>"), undefined, "同一段脚本里两个值");
  assert.equal(cid("<script>a = { customerId: 'A2TEST00000' };</script><script>b = { customerId: 'A1DEMO00000' };</script>"), undefined, "两段脚本里两个值");
  // 被回显进脚本的文本（JSON 字符串里的单引号不转义）与真值并存：读不出，而不是读成植入的那个
  assert.equal(cid(`<script>var q = {"keywords":"customerId: 'A9PLANT00001'"}; var c = { customerId: "A1DEMO00000" };</script>`), undefined);
  // 形状不对的不参与比较
  assert.equal(cid("<script>a = { customerId: '' }; b = { customerId: 'A12' }; c = { customerId: 'A1DEMO00000' };</script>"), "A1DEMO00000");
});

test("植入夹具：标题、搜索框 value、商品标题、属性、注释里的 customerId 都读不到；内联脚本里有真值就读出真值", () => {
  const doc = fixture("planted-customer-id.html");
  const page = doc.documentElement.innerHTML;
  // 干扰项自己也得在，而且排在真值前面：整页跑正则、取第一个匹配的实现会读到 A9PLANT00001
  for (const fake of ["A9PLANT00001", "A9PLANT00002", "A9PLANT00003", "A9PLANT00004"]) {
    assert.ok(page.includes(fake), `${fake} 确实在页面里`);
    assert.ok(page.indexOf(fake) < page.indexOf("'customerId': 'A1DEMO00000'"), `${fake} 排在真值前面`);
  }
  assert.match(doc.querySelector("title").textContent, /customerId: 'A9PLANT00001'/);
  assert.match(doc.querySelector("#twotabsearchtextbox").getAttribute("value"), /customerId: "A9PLANT00002"/);
  const naive = /['"]?customerId['"]?\s*:\s*['"](A[0-9A-Z]{8,})['"]/.exec(page);
  assert.equal(naive[1], "A9PLANT00001", "对照：整页 HTML 上取第一个合格匹配会中招");
  assert.equal(readLoginState(doc, "https://www.amazon.com/s?k=demo"), "ok");
  assert.equal(readCustomerId(doc), "A1DEMO00000");
});

test("植入夹具：内联脚本里没有真值时读不出（已登录、没读到买家号 ID），绝不把植入的值读成买家号 ID", () => {
  const doc = fixture("planted-no-script.html");
  assert.match(doc.documentElement.innerHTML, /customerId: 'A9PLANT00001'/);
  assert.equal(readLoginState(doc, "https://www.amazon.com/s?k=demo"), "ok");
  assert.equal(readCustomerId(doc), undefined);
  const snap = identityFromPage("ok", readCustomerId(doc) ?? null, "https://www.amazon.com/s?k=demo", Date.parse("2026-11-18T06:00:00Z"));
  assert.deepEqual(toIdentity(snap), { signed_in: true, customer_id: null, site: "US", page_url: "https://www.amazon.com/s", conflict: false });
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
  assert.deepEqual(toIdentity(out), { signed_in: false, customer_id: null, site: "US", page_url: "https://www.amazon.com/gp/cart/view.html", conflict: false });
  const a = identityFromPage("ok", "A1DEMO0000", "https://www.amazon.com/a", now);
  const b = identityFromPage("ok", "A1DEMO0000", "https://www.amazon.com/b", now + 5000);
  assert.equal(sameIdentity(a, b), true);
  assert.equal(sameIdentity(a, out), false);
  assert.equal(isFresh(a, now + 4 * 60_000), true);
  assert.equal(isFresh(a, now + 6 * 60_000), false, "几分钟前读的快照不能当成新上报");
  assert.equal(isFresh(null, now), false);
});

test("订单历史页识别；日志里的买家号 ID 用遮掩形式（前 4 位 + … + 后 2 位）", () => {
  for (const p of ["/your-orders", "/your-orders/orders", "/gp/css/order-history", "/gp/css/order-history/ref=x", "/gp/your-account/order-history"]) {
    assert.equal(isOrderHistoryPath(p), true, p);
  }
  for (const p of ["/", "/dp/B0DEMO0001", "/gp/cart/view.html", "/your-ordersx", null]) {
    assert.equal(isOrderHistoryPath(p), false, String(p));
  }
  assert.equal(maskCustomerId("A1DEMO00000"), "A1DE…00");
  assert.equal(maskCustomerId(null), "—");
});

// ── 多个标签页的身份读数对账（按页面加载先后）──────────────────────────────

const T0 = Date.parse("2026-11-05T01:00:00Z");
/** 进行中批次的买家号 ID（对账的基准）与另一个买家号 ID。没有批次在跑时基准是 null。 */
const BOUND = "A1DEMO00000";
const OTHER = "A9OTHER00000";
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

test("旧标签页（批次的买家号）+ 新标签页（另一个买家号）：报另一个买家号、有分歧；旧页面重读不能翻回批次的买家号", () => {
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
  // 旧标签页刷新（新的加载时刻），而浏览器其实已经换回批次的买家号：分歧消除，报批次的买家号
  const s2 = observeIdentity(s, obs(1, BOUND, 80));
  assert.equal(s2.newest_gone, null, "比它新的开着的页面出现后，已关闭的读数作废");
  const r2 = reconcileIdentity(s2, BOUND, T0 + 80 * 60_000);
  assert.equal(r2.customer_id, BOUND);
  assert.equal(r2.conflict, false);
});

test("分歧时绝不报「是批次的买家号」：新页面是批次的买家号、旧页面是别人，也按别人报（刷新或关掉旧页面后才一致）", () => {
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

// 「已登录、但没读到 ID」的读数（弱证据）不取代、也不作废读到 ID 或显示未登录的读数（强证据）
const weak = (tab, loadedMin, readMin = loadedMin) => ({ ...obs(tab, BOUND, loadedMin, readMin), customer_id: null });

test("同一标签页从另一个买家号的页面跳到读不出 ID 的页面：另一个买家号仍是证据，有分歧，不报批次的买家号", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)), see(weak(2, 90)));
  assert.equal(s.newest_gone?.customer_id, OTHER, "上一页的读数留作证据");
  const r = reconcileIdentity(s, BOUND, T0 + 91 * 60_000);
  assert.equal(r.conflict, true);
  assert.equal(r.signed_in, true);
  assert.equal(r.customer_id, OTHER);
  assert.equal(r.at, T0 + 91 * 60_000, "有分歧：新鲜度取当前时刻");
});

test("同上，但旧页面先说了 bye（标签页读数先退出、再来新页面的弱读数）：结论一样", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)), gone(2), see(weak(2, 90)));
  assert.equal(s.newest_gone?.customer_id, OTHER, "弱读数不作废已关闭页面上的强证据");
  const r = reconcileIdentity(s, BOUND, T0 + 91 * 60_000);
  assert.equal(r.conflict, true);
  assert.equal(r.customer_id, OTHER);
});

test("另一个买家号的标签页关掉后开了一张读不出 ID 的新标签页：仍有分歧；再关掉它，证据还是另一个买家号", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)), gone(2), see(weak(3, 90)));
  const r = reconcileIdentity(s, BOUND, T0 + 91 * 60_000);
  assert.equal(r.conflict, true);
  assert.equal(r.customer_id, OTHER);
  const s2 = retireIdentityTab(s, 3);
  assert.equal(s2.newest_gone.customer_id, OTHER, "弱读数关掉时不顶替强证据");
  assert.equal(s2.newest_gone.tab_id, 2);
  const r2 = reconcileIdentity(s2, BOUND, T0 + 92 * 60_000);
  assert.equal(r2.conflict, true);
  assert.equal(r2.customer_id, OTHER);
});

test("同一标签页从未登录的页面跳到读不出 ID 的页面：仍报未登录、有分歧；旧标签页重新加载出批次的买家号后一致", () => {
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, null, 30)), see(weak(2, 40)));
  const r = reconcileIdentity(s, BOUND, T0 + 41 * 60_000);
  assert.equal(r.signed_in, false);
  assert.equal(r.customer_id, null);
  assert.equal(r.conflict, true);
  const s2 = observeIdentity(s, obs(1, BOUND, 100));
  assert.equal(s2.newest_gone, null, "比它新的强证据出现后，已关闭的读数作废");
  const r2 = reconcileIdentity(s2, BOUND, T0 + 100 * 60_000);
  assert.equal(r2.conflict, false);
  assert.equal(r2.signed_in, true);
  assert.equal(r2.customer_id, BOUND);
});

test("弱读数之间照常互相取代；只有一个标签页时，上一页读到的批次的买家号在读不出 ID 的新页面上仍算数", () => {
  const s = fold(see(weak(1, 10)), gone(1), see(weak(2, 20)));
  assert.equal(s.newest_gone, null, "已关闭的弱读数被不比它旧的读数作废");
  const one = fold(see(obs(1, BOUND, 0)), see(weak(1, 10, 12)));
  const r = reconcileIdentity(one, BOUND, T0 + 12 * 60_000);
  assert.equal(r.conflict, false);
  assert.equal(r.customer_id, BOUND);
  assert.equal(r.at, T0 + 12 * 60_000, "新鲜度取最近一次读取");
});

test("同一页重读时没读到 ID、或较早加载的页面迟到的读数：不抹掉另一个买家号的证据", () => {
  // 重读同一页（加载时刻没变）这一次没读到 ID
  const s = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)), see(weak(2, 60, 65)));
  const r = reconcileIdentity(s, BOUND, T0 + 66 * 60_000);
  assert.equal(r.conflict, true);
  assert.equal(r.customer_id, OTHER);
  // 标签页 2 已经在读不出 ID 的新页面上，之前那一页（另一个买家号）的读数这时才到
  const late = fold(see(obs(1, BOUND, 0)), see(weak(2, 90)), see(obs(2, OTHER, 60, 91)));
  assert.equal(late.live["2"].customer_id, null, "不覆盖标签页现在的读数");
  const r2 = reconcileIdentity(late, BOUND, T0 + 92 * 60_000);
  assert.equal(r2.conflict, true);
  assert.equal(r2.customer_id, OTHER);
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

// ── 对账的基准：进行中批次的买家号 ID，或没有 ─────────────────────────────

test("没有批次在跑（基准 null）：分歧时报加载最新的强证据，带 conflict；新的是谁就报谁", () => {
  const s1 = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)));
  const r1 = reconcileIdentity(s1, null, T0 + 61 * 60_000);
  assert.deepEqual([r1.conflict, r1.customer_id, r1.source_tab], [true, OTHER, 2]);
  const s2 = fold(see(obs(1, OTHER, 0)), see(obs(2, BOUND, 60)));
  const r2 = reconcileIdentity(s2, null, T0 + 61 * 60_000);
  assert.deepEqual([r2.conflict, r2.customer_id, r2.source_tab], [true, BOUND, 2], "没有基准：不避开任何一个买家号");
  // 最新的是一张读不出 ID 的页面：它是弱证据，报最新的**强**证据
  const s3 = fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60)), see({ ...obs(3, BOUND, 90), customer_id: null }));
  const r3 = reconcileIdentity(s3, null, T0 + 91 * 60_000);
  assert.deepEqual([r3.conflict, r3.customer_id], [true, OTHER]);
  // 最新的强证据是未登录
  const s4 = fold(see(obs(1, BOUND, 0)), see(obs(2, null, 60)));
  const r4 = reconcileIdentity(s4, null, T0 + 61 * 60_000);
  assert.deepEqual([r4.conflict, r4.signed_in, r4.customer_id], [true, false, null]);
});

test("有批次在跑（基准 = 批次的买家号）：分歧时绝不报基准，哪怕它是加载最新的那一份", () => {
  const s = fold(see(obs(1, OTHER, 0)), see(obs(2, BOUND, 60)));
  assert.equal(reconcileIdentity(s, BOUND, T0 + 61 * 60_000).customer_id, OTHER, "有基准：避开它");
  assert.equal(reconcileIdentity(s, null, T0 + 61 * 60_000).customer_id, BOUND, "同样的读数，没有基准：报最新的");
  // 基准之外只有「未登录」：报未登录
  const out = fold(see(obs(1, null, 0)), see(obs(2, BOUND, 60)));
  const r = reconcileIdentity(out, BOUND, T0 + 61 * 60_000);
  assert.deepEqual([r.conflict, r.signed_in, r.customer_id], [true, false, null]);
});

test("基准不是「上一次上报解析出来的账号」：一新一旧两张标签页反复对账，上报不在两个买家号之间来回翻", () => {
  // 标签页 1 较早加载、显示 Y（早已登出的旧页面），标签页 2 较新、显示 X（真实登录）。内容脚本每 2 分钟重读一次。
  let store = fold(see(obs(1, OTHER, 0)), see(obs(2, BOUND, 60)));
  const reported = [];
  for (let i = 0; i < 6; i++) {
    const t = 61 + i * 2;
    store = observeIdentity(observeIdentity(store, obs(1, OTHER, 0, t)), obs(2, BOUND, 60, t));
    const r = reconcileIdentity(store, null, T0 + t * 60_000);
    reported.push(r.customer_id);
    assert.equal(r.conflict, true);
  }
  assert.deepEqual(reported, Array(6).fill(BOUND), "每一轮报的都是同一份（加载最新的强证据）");
  // 对照：拿上一次报的那个当基准（旧做法），X / Y 严格交替
  let last = null;
  const flip = [];
  for (let i = 0; i < 6; i++) {
    const r = reconcileIdentity(store, last, T0 + (80 + i) * 60_000);
    flip.push(r.customer_id);
    last = r.customer_id;
  }
  assert.deepEqual(flip, [BOUND, OTHER, BOUND, OTHER, BOUND, OTHER]);
});

test("上报体带 conflict：有分歧时为 true（toIdentity 不丢掉它），一致时为 false；单页读数恒为 false", () => {
  const split = reconcileIdentity(fold(see(obs(1, BOUND, 0)), see(obs(2, OTHER, 60))), null, T0 + 61 * 60_000);
  assert.deepEqual(toIdentity(split), { signed_in: true, customer_id: OTHER, site: "US", page_url: "https://www.amazon.com/gp/css/order-history", conflict: true });
  const same = reconcileIdentity(fold(see(obs(1, BOUND, 0)), see(obs(2, BOUND, 60))), null, T0 + 61 * 60_000);
  assert.equal(toIdentity(same).conflict, false);
  assert.equal(toIdentity(same).customer_id, BOUND);
  assert.equal(toIdentity(identityFromPage("ok", BOUND, "https://www.amazon.com/", T0)).conflict, false);
  assert.equal(sameIdentity(split, { ...split, conflict: false }), true, "sameIdentity 只比登录状态与 ID，不比有没有分歧");
});
