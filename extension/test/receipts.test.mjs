/** 结果回执（core/receipts.ts）与内容脚本 → 后台的有界重发（core/remote.ts）。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  RECEIPT_BACKOFF_MS,
  RECEIPT_GRACE_MS,
  RECEIPT_MAX_AGE_MS,
  classifyResultReply,
  dropExpired,
  dueReceipts,
  isLateReceipt,
  isRetryableResultError,
  pendingForRun,
  putReceipt,
  receiptText,
  receiptViews,
  settleReceipt,
} from "../build/core/receipts.js";
import { SW_RETRY_OPS, callSw } from "../build/core/remote.js";

const NOW = Date.parse("2026-11-05T06:00:00Z");
const ok = { outcome: "succeeded", amazon_order_no: "SIM-20261105-7", amounts: null, card_last4: null, delivery: null, error_code: null, error_detail: null };
const httpErr = (status, code, detail = {}) => ({ ok: false, error: { kind: "http", status, code, message: code, detail } });
const netErr = { ok: false, error: { kind: "network", status: null, code: "NETWORK", message: "无法连接服务端", detail: null } };

test("回执的结论：送达（含迟到回执已记下）删、明确不收删、没说上话或 5xx 留着补交", () => {
  assert.equal(classifyResultReply({ ok: true, status: 200, data: { duplicate: true } }), "delivered");
  const late = httpErr(409, "CONFLICT", { attempt_status: "unknown", late_receipt_recorded: true });
  assert.equal(isLateReceipt(late.error), true);
  assert.equal(classifyResultReply(late), "delivered");
  assert.equal(classifyResultReply(httpErr(409, "CONFLICT", { attempt_status: "succeeded" })), "drop");
  assert.equal(classifyResultReply(httpErr(404, "NOT_FOUND")), "drop");
  assert.equal(classifyResultReply(httpErr(401, "UNAUTHORIZED")), "drop");
  assert.equal(classifyResultReply(httpErr(503, "INTERNAL")), "retry");
  assert.equal(classifyResultReply(netErr), "retry");
  assert.equal(classifyResultReply({ ok: false, error: { kind: "network", status: null, code: "SW_UNAVAILABLE", message: "x", detail: null } }), "retry");
  assert.equal(isRetryableResultError(httpErr(null, "NOT_PAIRED").error), false, "本地错误（没配对）不是「没说上话」");
});

test("存回执：同一笔同一份不重置；先由发起的一路自己送，宽限之后后台才补交；补交按退避", () => {
  let s = putReceipt(null, 7, 9, ok, NOW);
  assert.equal(s["7"].next_at, NOW + RECEIPT_GRACE_MS);
  assert.deepEqual(dueReceipts(s, NOW + 1000), []);
  assert.equal(dueReceipts(s, NOW + RECEIPT_GRACE_MS).length, 1);
  s = settleReceipt(s, 7, "retry", "无法连接服务端", NOW + 31_000, true);
  assert.equal(s["7"].tries, 1);
  assert.equal(s["7"].next_at, NOW + 31_000 + RECEIPT_BACKOFF_MS[0]);
  const again = putReceipt(s, 7, 9, ok, NOW + 40_000);
  assert.equal(again["7"].tries, 1, "同一份结果再存一次不重置补交次数");
  const changed = putReceipt(s, 7, 9, { ...ok, amazon_order_no: "SIM-20261105-8" }, NOW + 40_000);
  assert.equal(changed["7"].tries, 0);
  assert.equal(pendingForRun(s, 9), 1);
  assert.equal(pendingForRun(s, 10), 0);
  assert.deepEqual(settleReceipt(s, 7, "delivered", null, NOW, true), {});
  assert.deepEqual(settleReceipt(s, 7, "drop", "x", NOW, true), {});
});

test("回执最多保留 24 小时；界面上一行一条", () => {
  const s = putReceipt(putReceipt(null, 7, 9, ok, NOW), 8, 9, { ...ok, outcome: "failed", amazon_order_no: null, error_code: "OUT_OF_STOCK" }, NOW + 1);
  const { store, expired } = dropExpired(s, NOW + RECEIPT_MAX_AGE_MS + 1);
  assert.deepEqual(expired.map((r) => r.attempt_id), [7]);
  assert.deepEqual(Object.keys(store), ["8"]);
  const lines = receiptViews(settleReceipt(s, 7, "retry", "无法连接服务端", NOW, true)).map(receiptText);
  assert.equal(lines[0], "尝试 #7：成功 SIM-20261105-7，已补交 1 次，最近一次：无法连接服务端");
  assert.equal(lines[1], "尝试 #8：失败（商品缺货）", "错误码换成中文，不原样显示");
});

test("后台没有响应：结果、进度、批次心跳、结束批次有界地原样再发；开始批次、领取、护栏、提交登记绝不", async () => {
  for (const op of ["result", "progress", "runHeartbeat", "finishRun", "verifying"]) {
    const seen = [];
    let n = 0;
    const send = async (o, args) => {
      seen.push(args);
      if (n++ < 2) throw new Error("A listener indicated an asynchronous response by returning true, but the message channel closed before a response was received");
      return { ok: true, status: 200, data: {} };
    };
    const r = await callSw(send, op, [7, { a: 1 }], { sleep: async () => {} });
    assert.equal(r.ok, true, op);
    assert.equal(seen.length, 3, `${op} 再发了两次`);
    assert.deepEqual(seen[2], seen[0], `${op} 再发的是同一份参数`);
    assert.ok(SW_RETRY_OPS.has(op));
  }
  for (const op of ["startRun", "next", "guard", "submitting"]) {
    let n = 0;
    const r = await callSw(async () => { n++; throw new Error("message channel closed"); }, op, [7], { sleep: async () => {} });
    assert.equal(r.ok, false);
    assert.equal(r.error.code, "SW_UNAVAILABLE");
    assert.equal(r.error.kind, "network");
    assert.equal(n, 1, `${op} 只发一次`);
  }
});

test("后台一直没有响应：再发有上限；后台答复了（哪怕是失败）就不再发", async () => {
  let n = 0;
  const r = await callSw(async () => { n++; return undefined; }, "result", [7, ok], { sleep: async () => {}, delaysMs: [0, 0, 0] });
  assert.equal(r.error.code, "SW_UNAVAILABLE");
  assert.equal(n, 4);
  let m = 0;
  const r2 = await callSw(async () => { m++; return netErr; }, "result", [7, ok], { sleep: async () => {} });
  assert.equal(r2.error.code, "NETWORK", "后台说服务端连不上：交给引擎的重发与后台的回执，不在这一层叠加");
  assert.equal(m, 1);
});

test("回执的补交时机：后台启动、心跳成功时不等退避；引擎自己还在送的（没被后台补交过、宽限未到）不抢着发", () => {
  let s = putReceipt(null, 7, 9, ok, NOW); // 引擎刚存的，宽限 30 秒
  s = putReceipt(s, 8, 9, { ...ok, amazon_order_no: "SIM-20261105-8" }, NOW - 600_000);
  s = settleReceipt(s, 8, "retry", "无法连接服务端", NOW - 1_000, true); // 后台补交过一次，退避到 NOW + 29 秒
  assert.deepEqual(dueReceipts(s, NOW).map((r) => r.attempt_id), [], "闹钟按退避：都还没到时间");
  assert.deepEqual(dueReceipts(s, NOW, { ignoreBackoff: true }).map((r) => r.attempt_id), [8], "连得上服务端：后台补交过的立即再发");
  assert.deepEqual(dueReceipts(s, NOW + RECEIPT_GRACE_MS, { ignoreBackoff: true }).map((r) => r.attempt_id), [8, 7], "宽限过后引擎那份也补交");
  assert.deepEqual(dueReceipts(s, NOW - 600_000 + RECEIPT_MAX_AGE_MS + 1, { ignoreBackoff: true }).map((r) => r.attempt_id), [7], "超过 24 小时的不再补交");
});

test("回执的结论覆盖真实服务端的几种答复：200 duplicate 送达；409 迟到回执送达；其他 409 与 401 不再发；5xx 与断网留着", () => {
  assert.equal(classifyResultReply({ ok: true, status: 200, data: { attempt_status: "succeeded", duplicate: true } }), "delivered");
  assert.equal(classifyResultReply(httpErr(409, "CONFLICT", { attempt_status: "unknown", order_status: "result_unknown", late_receipt_recorded: true })), "delivered");
  assert.equal(classifyResultReply(httpErr(409, "CONFLICT", { attempt_status: "succeeded", reported: "failed" })), "drop");
  assert.equal(classifyResultReply(httpErr(409, "LEASE_LOST")), "drop");
  assert.equal(classifyResultReply(httpErr(401, "UNAUTHORIZED")), "drop");
  assert.equal(classifyResultReply(httpErr(500, "INTERNAL")), "retry");
  assert.equal(classifyResultReply(httpErr(503, "HTTP_503")), "retry");
  assert.equal(classifyResultReply(netErr), "retry");
});
