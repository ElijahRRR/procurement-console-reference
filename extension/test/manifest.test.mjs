/** 清单：权限最小（不申请 cookies、tabs），主机权限只有亚马逊美国站与本机服务端。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const manifest = JSON.parse(readFileSync(new URL("../public/manifest.json", import.meta.url), "utf8"));

test("权限恰好是 storage、alarms（不申请 tabs、cookies、scripting、webRequest）", () => {
  assert.deepEqual([...manifest.permissions].sort(), ["alarms", "storage"]);
  assert.equal(manifest.optional_permissions, undefined);
});

test("主机权限：www.amazon.com 与本机服务端，没有 <all_urls>", () => {
  assert.deepEqual([...manifest.host_permissions].sort(), ["http://127.0.0.1/*", "http://localhost/*", "https://www.amazon.com/*"]);
  assert.deepEqual(manifest.content_scripts.map((c) => c.matches), [["https://www.amazon.com/*"]]);
  assert.equal(manifest.manifest_version, 3);
});
