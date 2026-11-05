/** 标签页级执行租约与两道闸（移植自早期实现 test/unit.test.mjs 的对应几节）。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { LEASE_DEFAULTS, decideLease, releaseLease } from "../build/core/lease.js";
import { Serial, SingleFlight, withTimeout } from "../build/core/gates.js";

const NOW = 1_000_000_000;
const ask = (o = {}) => ({ tabId: 1, busy: false, now: NOW, holderAlive: true, ...o });

test("没有租约时谁问谁拿到", () => {
  const v = decideLease(null, ask());
  assert.equal(v.granted, true);
  assert.equal(v.reason, "fresh");
  assert.equal(v.next.until, NOW + LEASE_DEFAULTS.ttlMs);
});

test("持有者续租，到期时刻往后推", () => {
  const v = decideLease({ tabId: 1, until: NOW + 1000, busy: true }, ask({ busy: true }));
  assert.equal(v.reason, "renew");
  assert.equal(v.next.until, NOW + LEASE_DEFAULTS.ttlMs);
  assert.equal(v.next.busy, true);
});

test("租约没过期时别的标签页拿不到，也不动存储", () => {
  const v = decideLease({ tabId: 2, until: NOW + 1000, busy: false }, ask());
  assert.equal(v.granted, false);
  assert.equal(v.reason, "held");
  assert.equal(v.next, null);
});

test("过期但持有者正在执行（后台节流拖过了有效期）：不换手", () => {
  const v = decideLease({ tabId: 2, until: NOW - 1, busy: true }, ask({ holderAlive: true }));
  assert.equal(v.granted, false);
  assert.equal(v.reason, "held-busy");
});

test("过期且持有者标签页已不在：换手", () => {
  const v = decideLease({ tabId: 2, until: NOW - 1, busy: true }, ask({ holderAlive: false }));
  assert.equal(v.granted, true);
  assert.equal(v.reason, "holder-gone");
});

test("持有者报告忙却一直不续租：宽限期过完照样换手（等待必须有界）", () => {
  const cur = { tabId: 2, until: NOW - LEASE_DEFAULTS.busyGraceMs - 1, busy: true };
  const v = decideLease(cur, ask({ holderAlive: true }));
  assert.equal(v.granted, true);
  assert.equal(v.reason, "taken-over");
});

test("过期且持有者不忙：换手，记新持有者", () => {
  const v = decideLease({ tabId: 2, until: NOW - 1, busy: false }, ask());
  assert.equal(v.reason, "taken-over");
  assert.equal(v.next.tabId, 1);
});

test("两个时间预算由入参决定", () => {
  const rules = { ttlMs: 1000, busyGraceMs: 2000 };
  assert.equal(decideLease(null, ask(), rules).next.until, NOW + 1000);
  assert.equal(decideLease({ tabId: 2, until: NOW - 1, busy: true }, ask(), rules).granted, false);
  assert.equal(decideLease({ tabId: 2, until: NOW - 2001, busy: true }, ask(), rules).granted, true);
});

test("releaseLease 只释放持有者自己的租约", () => {
  const cur = { tabId: 2, until: NOW, busy: true };
  assert.equal(releaseLease(cur, 2), null);
  assert.equal(releaseLease(cur, 1), cur);
  assert.equal(releaseLease(null, 1), null);
});

// 「读租约 → 裁决 → 写租约」夹着两次 await（chrome.storage.session 是异步的），
// 两条请求前后脚进来会读到同一个旧值、双双拿到租约。这里用一个同样异步的假存储复现。
function mkStore() {
  let cell = null;
  return {
    get: async () => { await null; await null; return cell; },
    set: async (v) => { await null; cell = v; },
  };
}
const acquireOn = (store) => async (tabId) => {
  const cur = await store.get();
  const v = decideLease(cur, { tabId, busy: true, now: NOW, holderAlive: true });
  if (v.granted) { await store.set(v.next); return true; }
  return false;
};

test("（反面）不串行时两个标签页会同时拿到租约", async () => {
  const raw = acquireOn(mkStore());
  const [a, b] = await Promise.all([raw(1), raw(2)]);
  assert.equal(a && b, true);
});

test("串行之后同一时刻只有一个标签页拿到租约", async () => {
  const gate = new Serial();
  const raw = acquireOn(mkStore());
  const [a, b] = await Promise.all([gate.run(() => raw(1)), gate.run(() => raw(2))]);
  assert.notEqual(a, b);
});

test("Serial：前一件抛错原样抛回，不堵住后面；顺序是排队不是并发", async () => {
  const gate = new Serial();
  const order = [];
  const p0 = gate.run(async () => { throw new Error("boom"); });
  const p1 = gate.run(async () => { order.push("a1"); await new Promise((r) => setTimeout(r, 5)); order.push("a2"); });
  const p2 = gate.run(async () => { order.push("b1"); return "ok"; });
  await assert.rejects(p0, /boom/);
  await p1;
  assert.equal(await p2, "ok");
  assert.deepEqual(order, ["a1", "a2", "b1"]);
});

test("SingleFlight：在跑时第二件直接被挡下，抛错之后闸也放开", async () => {
  const sf = new SingleFlight();
  let release;
  const gate = new Promise((r) => { release = r; });
  const first = sf.run(async () => { await gate; return 1; });
  assert.equal(sf.busy, true);
  assert.deepEqual(await sf.run(async () => 2), { ran: false });
  release();
  assert.deepEqual(await first, { ran: true, value: 1 });
  assert.equal(sf.busy, false);
  await assert.rejects(sf.run(async () => { throw new Error("x"); }), /x/);
  assert.equal(sf.busy, false);
});

test("withTimeout：要么拿到值，要么按时抛错", async () => {
  assert.equal(await withTimeout(Promise.resolve(3), 50, "超时"), 3);
  await assert.rejects(withTimeout(new Promise(() => {}), 10, "等太久了"), /等太久了/);
});
