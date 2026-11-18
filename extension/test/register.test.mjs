/** 自动登记的纯逻辑（core/register.ts）与共享单飞闸（core/gates.SharedFlight）：
 *  令牌生成、登记失败后的退避、哪些失败不自动重试、谁在要求登记时发不发请求。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEVICE_TOKEN_RE,
  MANUAL_RETRY_MIN_GAP_MS,
  RATE_LIMIT_DEFAULT_S,
  REGISTER_BACKOFF_S,
  base64url,
  generateDeviceToken,
  isValidDeviceToken,
  registerAllowed,
  registerFailed,
  registerHalts,
  registerRetryDelayS,
  registerSucceeded,
} from "../build/core/register.js";
import { SharedFlight } from "../build/core/gates.js";

const NOW = Date.parse("2026-11-18T06:00:00Z");
const net = { kind: "network", status: null, code: "NETWORK", message: "无法连接服务端", detail: null };
const http = (status, code, detail = {}, extra = {}) => ({ kind: "http", status, code, message: code, detail, ...extra });

test("令牌：32 字节随机数的 base64url，43 个字符，形状是服务端接受的那种；每次都不一样", () => {
  const t = generateDeviceToken();
  assert.equal(t.length, 43);
  assert.match(t, /^[A-Za-z0-9_-]{43}$/);
  assert.match(t, DEVICE_TOKEN_RE);
  assert.equal(isValidDeviceToken(t), true);
  const many = new Set(Array.from({ length: 50 }, () => generateDeviceToken()));
  assert.equal(many.size, 50);
  // 用的是传进来的随机源（浏览器里是 crypto.getRandomValues），而且要了正好 32 字节
  let asked = 0;
  const fixed = generateDeviceToken((buf) => { asked = buf.length; buf.fill(0xff); return buf; });
  assert.equal(asked, 32);
  assert.equal(fixed, "_".repeat(42) + "8", "全 0xff → base64url 不带 + / =");
  assert.equal(base64url(new Uint8Array([0xfb, 0xff, 0xfe])), "-__-");
});

test("令牌形状：32 – 128 个字母、数字、下划线、短横；旧版配对得到的令牌（43 个字符的 urlsafe）也合格", () => {
  assert.equal(isValidDeviceToken("a".repeat(32)), true);
  assert.equal(isValidDeviceToken("a".repeat(128)), true);
  assert.equal(isValidDeviceToken("Zx0_-" + "b".repeat(38)), true, "旧令牌原样沿用");
  for (const bad of ["a".repeat(31), "a".repeat(129), "a".repeat(40) + "+", "a".repeat(40) + "=", "", null, 42]) {
    assert.equal(isValidDeviceToken(bad), false, String(bad).slice(0, 12));
  }
});

test("登记失败的退避：5 / 15 / 60 秒，之后一直 60 秒；成功后归零", () => {
  assert.deepEqual([...REGISTER_BACKOFF_S], [5, 15, 60]);
  let st = registerFailed(null, net, NOW);
  assert.deepEqual([st.ok, st.fails, st.next_at, st.halted, st.code], [false, 1, NOW + 5_000, false, "NETWORK"]);
  st = registerFailed(st, net, NOW + 5_000);
  assert.deepEqual([st.fails, st.next_at], [2, NOW + 5_000 + 15_000]);
  st = registerFailed(st, net, NOW + 20_000);
  assert.deepEqual([st.fails, st.next_at], [3, NOW + 20_000 + 60_000]);
  st = registerFailed(st, http(500, "INTERNAL"), NOW + 80_000);
  assert.deepEqual([st.fails, st.next_at], [4, NOW + 80_000 + 60_000], "之后一直 60 秒；5xx 同样退避");
  const ok = registerSucceeded(NOW + 90_000);
  assert.deepEqual(ok, { ok: true, at: NOW + 90_000, fails: 0, next_at: null, halted: false, code: null, message: null });
  assert.equal(registerFailed(ok, net, NOW + 100_000).fails, 1, "成功之后再失败从头数");
});

test("429 按服务端给的 retry_after_s（detail 优先，其次 Retry-After 头），没有则 60 秒", () => {
  assert.equal(registerRetryDelayS(http(429, "RATE_LIMITED", { retry_after_s: 42 }), 1), 42);
  assert.equal(registerRetryDelayS(http(429, "RATE_LIMITED", {}, { retry_after_s: 17 }), 3), 17);
  assert.equal(registerRetryDelayS(http(429, "RATE_LIMITED"), 1), RATE_LIMIT_DEFAULT_S);
  assert.equal(RATE_LIMIT_DEFAULT_S, 60);
  assert.equal(registerRetryDelayS(http(429, "RATE_LIMITED", { retry_after_s: 0 }), 1), 60, "0 秒不算数");
  const st = registerFailed(null, http(429, "RATE_LIMITED", { retry_after_s: 42 }), NOW);
  assert.deepEqual([st.next_at, st.halted], [NOW + 42_000, false]);
});

test("WORKSPACE_REQUIRED、PLUGIN_OUTDATED、403、密钥库写不进去：不自动重试，等所有者处理", () => {
  for (const e of [
    http(409, "WORKSPACE_REQUIRED"),
    http(409, "PLUGIN_OUTDATED"),
    http(403, "FORBIDDEN"),
    http(403, "SOMETHING_ELSE"),
    http(422, "VALIDATION"),
    { kind: "http", status: null, code: "TOKEN_STORE", message: "插件的密钥库写不进去", detail: null },
  ]) {
    assert.equal(registerHalts(e), true, e.code);
    assert.equal(registerRetryDelayS(e, 1), null, e.code);
    const st = registerFailed(null, e, NOW);
    assert.deepEqual([st.halted, st.next_at], [true, null], e.code);
  }
  for (const e of [net, http(500, "INTERNAL"), http(409, "CONFLICT"), http(429, "RATE_LIMITED")]) assert.equal(registerHalts(e), false, e.code);
});

test("发不发登记请求：自动的受退避约束、停下时不发；手动「重试」不等退避也重试停下的，但 429 的等待照旧；强制的立即发", () => {
  // 还没试过、上一次成功：都发
  for (const mode of ["auto", "manual", "force"]) {
    assert.equal(registerAllowed(null, NOW, mode), true, mode);
    assert.equal(registerAllowed(registerSucceeded(NOW), NOW, mode), true, mode);
  }
  const failed = registerFailed(null, net, NOW); // 5 秒后才自动重试
  assert.equal(registerAllowed(failed, NOW + 4_999, "auto"), false, "心跳闹钟、面板展开：退避没到不发");
  assert.equal(registerAllowed(failed, NOW + 5_000, "auto"), true);
  assert.equal(registerAllowed(failed, NOW + MANUAL_RETRY_MIN_GAP_MS, "manual"), true, "点「重试」不等退避");
  assert.equal(registerAllowed(failed, NOW + 500, "manual"), false, "但不能连点成一片");
  assert.equal(registerAllowed(failed, NOW + 1, "force"), true);

  const halted = registerFailed(null, http(409, "WORKSPACE_REQUIRED"), NOW);
  assert.equal(registerAllowed(halted, NOW + 3_600_000, "auto"), false, "停下等人处理：自动的永远不发");
  assert.equal(registerAllowed(halted, NOW + 3_000, "manual"), true, "所有者处理完点「重试」");
  assert.equal(registerAllowed(halted, NOW + 1, "force"), true, "保存了工作区 ID、点「重新登记」");

  const limited = registerFailed(null, http(429, "RATE_LIMITED", { retry_after_s: 42 }), NOW);
  assert.equal(registerAllowed(limited, NOW + 10_000, "manual"), false, "被限速：手动重试也等服务端说的那么久");
  assert.equal(registerAllowed(limited, NOW + 41_999, "auto"), false);
  assert.equal(registerAllowed(limited, NOW + 42_000, "auto"), true);
  assert.equal(registerAllowed(limited, NOW + 42_000, "manual"), true);
});

test("共享单飞闸：有一件在跑时后到的不另起一件，拿同一个结果；跑完之后下一件照常起；失败也放开", async () => {
  const flight = new SharedFlight();
  let runs = 0;
  let release;
  const job = () => new Promise((resolve) => { runs++; release = () => resolve(`第 ${runs} 次`); });
  const a = flight.run(job);
  const b = flight.run(job);
  const c = flight.run(() => { throw new Error("后到的这件不该被执行"); });
  assert.equal(flight.busy, true);
  await new Promise((r) => setImmediate(r));
  assert.equal(runs, 1, "三个调用只起了一件");
  release();
  assert.deepEqual(await Promise.all([a, b, c]), ["第 1 次", "第 1 次", "第 1 次"]);
  assert.equal(flight.busy, false);
  const d = flight.run(job);
  await new Promise((r) => setImmediate(r));
  release();
  assert.equal(await d, "第 2 次");
  await assert.rejects(flight.run(async () => { throw new Error("boom"); }), /boom/);
  await assert.rejects(flight.run(() => { throw new Error("同步抛错"); }), /同步抛错/);
  assert.equal(flight.busy, false, "失败之后闸放开");
  assert.equal(await flight.run(async () => "又能跑了"), "又能跑了");
});
