/** 构建最后一步：把 manifest、两个页面的 HTML 拷进 dist/，生成图标，并核对产物齐全。 */

import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

mkdirSync("dist", { recursive: true });
const manifest = JSON.parse(readFileSync("public/manifest.json", "utf8"));
const pkg = JSON.parse(readFileSync("package.json", "utf8"));
if (manifest.version !== pkg.version) {
  console.error(`manifest.json 的版本 ${manifest.version} 与 package.json 的 ${pkg.version} 不一致`);
  process.exit(1);
}
copyFileSync("public/manifest.json", "dist/manifest.json");
copyFileSync("src/options/options.html", "dist/options.html");
copyFileSync("src/popup/popup.html", "dist/popup.html");
execFileSync(process.execPath, ["tools/make-icons.mjs"], { stdio: "inherit" });

const required = [
  "manifest.json", "background.js", "content.js", "options.html", "options.js", "popup.html", "popup.js",
  "icons/icon-16.png", "icons/icon-32.png", "icons/icon-48.png", "icons/icon-128.png",
];
const missing = required.filter((f) => !existsSync(`dist/${f}`));
if (missing.length) {
  console.error(`dist/ 缺少：${missing.join("、")}`);
  process.exit(1);
}
console.log("dist/ 已就绪，可在 Chrome「加载已解压的扩展程序」里选择 extension/dist");
