/** 「开始执行」那一步的对照表（core/start.ts）：这一页能不能开始、后台对账的答复、引擎开始那一步的结论，
 *  以及定时触发拿到答复之后记不记「处理过」。 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { IDENTITY_CODES, pageStartProblem, replyForPage, replyFromNotice, replyFromPrepare, scheduleVerdict } from "../build/core/start.js";

const CID = "A1DEMO00000";
const page = (extra = {}) => ({ signed_in: true, customer_id: CID, site: "US", page_url: "https://www.amazon.com/gp/css/order-history", conflict: false, at: 0, loaded_at: 0, ...extra });
const err = (status, code, kind = "http") => ({ kind, status, code, message: code, detail: null });

test("当前页必须自己读到买家号 ID 才能开始：没读到登录状态、未登录、没读到 ID、不是定时所属的买家号都不行", () => {
  assert.equal(pageStartProblem(page()), null);
  assert.equal(pageStartProblem(page(), CID), null);
  assert.equal(pageStartProblem(null).code, "NO_IDENTITY");
  assert.equal(pageStartProblem(page({ signed_in: false, customer_id: null })).code, "SIGNED_OUT");
  const noId = pageStartProblem(page({ customer_id: null }));
  assert.equal(noId.code, "NO_CUSTOMER_ID");
  assert.match(noId.why, /这一页没有读到买家号 ID。打开订单页后插件会自动读取/);
  assert.equal(pageStartProblem(page(), "A2TEST00000").code, "IDENTITY_MISMATCH");
  // 手动：提示所有者；定时：请后台换下一个标签页
  assert.deepEqual(replyForPage(noId, false), { result: "identity", code: "NO_CUSTOMER_ID", message: "开始执行失败：这一页没有读到买家号 ID。打开订单页后插件会自动读取" });
  assert.equal(replyForPage(noId, true).result, "next_tab");
});

test("后台对账的答复：可以开始 → 继续；有分歧、没登录 → identity；本浏览器未结束 → busy；没报上去、后台没响应 → not_ready；对账结论不是这一页的 ID → 不开始", () => {
  const ok = { ok: true, conflict: false, customer_id: CID, login_state: "ready", code: null, message: null };
  assert.equal(replyFromPrepare(ok, CID), null);
  assert.deepEqual(replyFromPrepare({ ...ok, customer_id: "A2TEST00000" }, CID).code, "IDENTITY_MISMATCH");
  const no = (code, message = `开始执行失败：${code}`) => ({ ok: false, conflict: code === "IDENTITY_CONFLICT", customer_id: null, login_state: null, code, message });
  assert.deepEqual(replyFromPrepare(no("IDENTITY_CONFLICT"), CID), { result: "identity", code: "IDENTITY_CONFLICT", message: "开始执行失败：IDENTITY_CONFLICT" });
  assert.equal(replyFromPrepare(no("SIGNED_OUT"), CID).result, "identity");
  assert.equal(replyFromPrepare(no("DEVICE_BUSY"), CID).result, "busy");
  assert.equal(replyFromPrepare(no("NOT_REGISTERED"), CID).result, "not_ready");
  assert.equal(replyFromPrepare(no("PLUGIN_OUTDATED"), CID).result, "not_ready");
  const dead = replyFromPrepare(null, CID);
  assert.deepEqual([dead.result, dead.code], ["not_ready", "SW_UNAVAILABLE"]);
  assert.match(dead.message, /^开始执行失败：插件后台没有响应$/);
});

test("引擎开始那一步的结论 → 答复：已开始、占用、没说上话、登录状态对不上、别的拒绝", () => {
  const n = (extra) => ({ started: false, run_id: null, status: "rejected", error: null, message: "m", busy_mine: false, ...extra });
  assert.deepEqual(replyFromNotice(n({ started: true, run_id: 9, status: null })), { result: "started", code: null, message: "m" });
  assert.deepEqual(replyFromNotice(n({ status: "busy", error: err(409, "ACCOUNT_BUSY") })), { result: "busy", code: "ACCOUNT_BUSY", message: "m" });
  assert.equal(replyFromNotice(n({ status: "busy", error: err(409, "DEVICE_BUSY"), busy_mine: true })).result, "busy");
  assert.equal(replyFromNotice(n({ error: err(null, "NETWORK", "network") })).result, "network");
  assert.equal(replyFromNotice(n({ error: err(null, "NOT_REGISTERED", "network") })).result, "network", "还没连上服务端按没说上话算");
  assert.equal(replyFromNotice(n({ error: err(403, "IDENTITY_MISMATCH") })).result, "identity");
  assert.equal(replyFromNotice(n({ error: err(null, "IDENTITY_CONFLICT") })).result, "identity");
  for (const code of ["SLOT_DONE", "ACCOUNT_NOT_REGISTERED", "ACCOUNT_PAUSED", "VALIDATION", "UNAUTHORIZED"]) {
    assert.deepEqual(replyFromNotice(n({ error: err(409, code) })), { result: "rejected", code, message: "m" }, code);
  }
  assert.deepEqual([...IDENTITY_CODES].sort(), ["IDENTITY_CONFLICT", "IDENTITY_MISMATCH", "NO_CUSTOMER_ID", "NO_IDENTITY", "SIGNED_OUT"]);
});

test("定时触发记不记「处理过」：已开始、SLOT_DONE、账号已暂停 → 记；422 → 刷新配置后记；ACCOUNT_BUSY / DEVICE_BUSY 与其余 → 不记，下一分钟再问", () => {
  const v = (result, code = null) => scheduleVerdict({ result, code });
  assert.equal(v("started"), "done");
  assert.equal(v("rejected", "SLOT_DONE"), "done");
  assert.equal(v("rejected", "ACCOUNT_PAUSED"), "done");
  assert.equal(v("rejected", "VALIDATION"), "refresh");
  assert.equal(v("busy", "ACCOUNT_BUSY"), "later");
  assert.equal(v("busy", "DEVICE_BUSY"), "later");
  assert.equal(v("busy", "TAB_BUSY"), "later");
  assert.equal(v("busy", "ALREADY_RUNNING"), "later");
  assert.equal(v("network", "NETWORK"), "later");
  assert.equal(v("not_ready", "NOT_REGISTERED"), "later");
  assert.equal(v("identity", "IDENTITY_CONFLICT"), "later");
  assert.equal(v("rejected", "ACCOUNT_NOT_REGISTERED"), "later", "拿不准的拒绝不记：去重的权威在服务端");
  assert.equal(v("rejected", "IDENTITY_MISMATCH"), "later");
  assert.equal(v("rejected", "UNAUTHORIZED"), "later");
  assert.equal(v("rejected", null), "later");
  assert.equal(v("next_tab", "NO_CUSTOMER_ID"), "next_tab");
});
