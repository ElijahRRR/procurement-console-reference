/** 身份识别用的页面解析（移植自早期实现 flow/dom/parse.ts 的导航栏与 customerId 两节）。
 *
 * 纯函数：只读 Document，不点、不等、不发请求、不改页面。对着 test/fixtures/ 里的
 * 导航栏夹具离线验证（夹具里塞满了隐藏模板副本、页脚 signin 链接、标题里的 "sign in"
 * 等干扰项，选择器写松了当场露馅）。
 *
 * 本 PR 只移植身份识别需要的部分；购物车、结算、下单那一套页面流程随真实驱动在后续 PR 迁入。
 */

/** 导航栏选择器。登录状态只能从页面上看出来（插件不申请 cookies 权限），判据按可信度排：
 *    1. URL 落在 /ap/signin —— 对**这一页**最硬，不需要解析任何 DOM。已登录时的重新验证页也落在这里，
 *       所以它只说明「这一页要求登录」；多个标签页之间谁说了算由后台按页面加载先后对账
 *       （core/identity.reconcileIdentity），有分歧时往安全的一侧（不报「是绑定账号」）；
 *    2. #nav-item-signout 在不在 —— 结构判据，只有签入的页面才发这个节点；
 *    3. #nav-link-accountList 的 href 指向哪 —— 结构判据；
 *    4. 问候语文案 —— **只做辅助**，只能把结论往「已登出」推，从不用来断定「已登录」。 */
export const NAV = {
  /** 账户入口。未签入时 href 指向 /ap/signin，签入时指向账户首页。 */
  accountLink: "#nav-link-accountList",
  /** 问候语：签入是 "Hello, <名字>"，未签入是 "Hello, sign in"。 */
  accountGreeting: "#nav-link-accountList-nav-line-1",
  /** 「Sign Out」。只有签入的页面才有；在默认 display:none 的账户浮层里，不要求可见。 */
  signOut: "#nav-item-signout",
  /** 账户入口指向这里 = 已签入。 */
  accountHrefHints: ["/gp/css/homepage.html", "/gp/your-account"],
  signInPath: "/ap/signin",
} as const;

/** `ok` 已登录 / `signed_out` 已登出 / `unknown` 读不出来。**unknown 不是 ok。** */
export type LoginState = "ok" | "signed_out" | "unknown";

const text = (el: Element | null | undefined): string => (el?.textContent ?? "").replace(/\s+/g, " ").trim();

function styleHidden(el: Element): boolean {
  const st = (el as unknown as HTMLElement).style;
  if (st && st.display === "none") return true;
  const attr = el.getAttribute("style");
  return !!attr && /display\s*:\s*none/i.test(attr);
}

/** 亚马逊页面上同一个 id 常有多份（隐藏的模板副本排在真身前面）。
 *  这里判断节点是不是被标为隐藏：自身或任一祖先带 [hidden] / aria-hidden / display:none。 */
function isHidden(el: Element): boolean {
  if (el.closest('[aria-hidden="true"]')) return true;
  if (el.closest("[hidden]")) return true;
  for (let cur: Element | null = el; cur; cur = cur.parentElement) {
    if (styleHidden(cur)) return true;
  }
  return false;
}

/** 输入：根节点与选择器 → 输出：第一个没有被标为隐藏的命中节点。 */
function visible<T extends Element>(root: Document | Element, selector: string): T | null {
  for (const el of Array.from(root.querySelectorAll<T>(selector))) {
    if (!isHidden(el)) return el;
  }
  return null;
}

/** 输入：页面或链接地址 → 输出：是不是亚马逊登录页。**这条判据的唯一定义处。**
 *  读不到地址（空串、null）时返回 false：「读不到」不是「不在登录页」，由别的判据接着说。 */
export function isSignInUrl(url: string | null | undefined): boolean {
  return !!url && url.includes(NAV.signInPath);
}

/** 输入：一张亚马逊页面与它的地址（默认取 doc.URL）→ 输出：这个浏览器此刻的登录状态。
 *
 *  三个信号一个都读不到 → `unknown`。**不许兜底成 ok**：判不出来时放行，这道闸就等于不存在。 */
export function readLoginState(doc: Document, url?: string | null): LoginState {
  let href = url ?? "";
  if (url === undefined) {
    try {
      href = doc.URL ?? "";
    } catch {
      href = "";
    }
  }
  // 最硬的一条：落在登录页上就是被登出了，DOM 里长什么样都不改变这个结论。
  if (isSignInUrl(href)) return "signed_out";

  // 隐藏副本不算数：亚马逊常把整套导航模板塞进 display:none 的壳里。
  const link = visible<HTMLAnchorElement>(doc, NAV.accountLink);
  const greet = visible(doc, NAV.accountGreeting);
  // signOut 不要求可见：账户浮层默认 display:none，要求可见的话每张签入页都会读成 unknown。
  const signOut = doc.querySelector(NAV.signOut);

  if (!link && !greet && !signOut) return "unknown"; // 这页没有导航栏

  const linkHref = link?.getAttribute("href") ?? "";
  if (isSignInUrl(linkHref)) return "signed_out";

  // 文案既是判据也是否决票：问候语明写着 sign in 时，残留的 signout 节点不算数。
  const greeting = text(greet).toLowerCase();
  if (/^(hello,?\s*)?sign\s*in\b/.test(greeting)) return "signed_out";

  if (signOut) return "ok";
  if (linkHref && NAV.accountHrefHints.some((h) => linkHref.includes(h))) return "ok";

  // 有导航栏，但没有一条判据说得准。宁可说「不知道」。
  return "unknown";
}

/** 买家号 ID 的形态：`A` 开头 + 至少 8 位大写字母或数字。形状不对就当没读到 ——
 *  一个空串或半截值送上去，会被当成「这个浏览器登着谁」。 */
const CUSTOMER_ID_RE = /^A[0-9A-Z]{8,}$/;

/** 页面 HTML 里那一处 `customerId`。`customerId: "A…"`、`'customerId': 'A…'`、
 *  `"customerId": "A…"` 都认。`g` 标志是关键：要扫全部匹配，不是只取第一个。 */
const CUSTOMER_ID_SCAN = /['"]?customerId['"]?\s*:\s*['"]([^'"]*)['"]/g;

/** 输入：一张亚马逊页面 → 输出：这个浏览器登着的买家号 ID；抠不到返回 undefined。
 *
 *  它是内联脚本里的一个字段，没有稳定的 DOM 节点带着它，只能在整页 HTML 上跑正则。
 *  比参考插件紧的两处：
 *    1. 扫全部匹配，取第一个**形状对得上**的 —— 页面上先出现的 `customerId: ""`
 *       （未登录模板、埋点占位）不能把结论废掉；
 *    2. 形状不对就当没读到，不把空串、"0"、半截值送上去。 */
export function readCustomerId(doc: Document): string | undefined {
  let html = "";
  try {
    html = doc.documentElement?.innerHTML ?? "";
  } catch {
    return undefined;
  }
  // lastIndex 是有状态的：正则是模块级常量，每次都从头扫。
  CUSTOMER_ID_SCAN.lastIndex = 0;
  for (let m = CUSTOMER_ID_SCAN.exec(html); m; m = CUSTOMER_ID_SCAN.exec(html)) {
    const v = (m[1] ?? "").trim();
    if (CUSTOMER_ID_RE.test(v)) return v;
  }
  return undefined;
}
