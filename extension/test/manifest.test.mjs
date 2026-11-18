/** 清单：权限最小（不申请 cookies、tabs），主机权限只有亚马逊美国站与本机服务端；版本与说明；
 *  源码里不读 Cookie、不上传页面 HTML；界面文案里不出现「配对」「绑定」「设备」。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const manifest = JSON.parse(readFileSync(new URL("../public/manifest.json", import.meta.url), "utf8"));
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

/** 输入：目录 → 输出：其下全部 .ts 文件的 [相对路径, 内容]。 */
function sources(dir = fileURLToPath(new URL("../src", import.meta.url)), base = dir) {
  const out = [];
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...sources(p, base));
    else if (name.endsWith(".ts")) out.push([p.slice(base.length + 1), readFileSync(p, "utf8")]);
  }
  return out;
}

test("权限恰好是 storage、alarms（不申请 tabs、cookies、scripting、webRequest）", () => {
  assert.deepEqual([...manifest.permissions].sort(), ["alarms", "storage"]);
  assert.equal(manifest.optional_permissions, undefined);
});

test("主机权限：www.amazon.com 与本机服务端，没有 <all_urls>", () => {
  assert.deepEqual([...manifest.host_permissions].sort(), ["http://127.0.0.1/*", "http://localhost/*", "https://www.amazon.com/*"]);
  assert.deepEqual(manifest.content_scripts.map((c) => c.matches), [["https://www.amazon.com/*"]]);
  assert.equal(manifest.manifest_version, 3);
});

test("版本 0.2.0（与 package.json 一致，服务端的版本闸要求不低于它）；说明里没有配对；安装后不靠设置页也能用", () => {
  assert.equal(manifest.version, "0.2.0");
  assert.equal(pkg.version, manifest.version);
  assert.doesNotMatch(manifest.description, /配对|绑定|设备/);
  assert.match(manifest.description, /自动登记/);
  assert.match(manifest.description, /不产生真实订单/);
  assert.equal(manifest.externally_connectable, undefined, "别的扩展与网页发不进消息");
  assert.equal(manifest.web_accessible_resources, undefined, "扩展页面不能被网页嵌进去");
  assert.deepEqual(manifest.options_ui, { page: "options.html", open_in_tab: true });
});

test("源码里不读 Cookie、不上传页面 HTML、不申请额外权限的接口（cookies、webRequest、scripting、debugger）", () => {
  const files = sources();
  assert.ok(files.length > 20);
  for (const [name, text] of files) {
    assert.doesNotMatch(text, /chrome\.cookies|document\.cookie|chrome\.webRequest|chrome\.scripting|chrome\.debugger|chrome\.declarativeNetRequest/, name);
    // 读页面只在 dom/parse.ts 里做，而且只读内联脚本的文本与导航栏节点；整页 HTML 不离开页面
    if (name !== "dom/parse.ts") assert.doesNotMatch(text, /outerHTML|documentElement\.innerHTML/, name);
    assert.doesNotMatch(text, /navigator\.webdriver|userAgentData|Object\.defineProperty\(navigator/, `${name}：不做反检测`);
  }
  const parse = files.find(([n]) => n === "dom/parse.ts")[1];
  assert.doesNotMatch(parse, /outerHTML|innerHTML/, "买家号 ID 只从内联脚本的文本里读，不在整页 HTML 上跑正则");
  assert.match(parse, /script:not\(\[src\]\)/);
});

test("界面与日志的文案里不出现「配对」「绑定」「设备」，不用感叹号（代码注释不算）", () => {
  for (const [name, text] of sources()) {
    // 去掉注释，只看字符串里的文案
    const code = text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
    const strings = code.match(/"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/g) ?? [];
    for (const str of strings) {
      if (!/[\u3400-\u9fff]/.test(str)) continue;
      assert.doesNotMatch(str, /配对|绑定/, `${name}: ${str.slice(0, 60)}`);
      assert.doesNotMatch(str, /设备/, `${name}: ${str.slice(0, 60)}`);
      assert.doesNotMatch(str, /！/, `${name}: ${str.slice(0, 60)}`);
    }
  }
});
