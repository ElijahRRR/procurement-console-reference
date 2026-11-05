/** 内存假服务端：按开发契约 v2 §4（护栏）与 §5（执行协议）实现插件接口，供引擎与客户端测试用。
 *
 * 只实现插件这一侧会碰到的规则，不是服务端的替身：
 *   · 设备令牌校验（Bearer）、配对码一次性；
 *   · 开始批次的前置条件（已绑定、身份 match、未暂停、只允许 simulated）与 ACCOUNT_BUSY；
 *   · 领取（同一批次已有在途尝试 → 409 ATTEMPT_IN_FLIGHT）；
 *   · 进度（上游取消且未登记提交 → continue=false）；
 *   · 护栏七项检查；提交登记（护栏未通过、批次失效、任务已取消 → 409）；
 *   · 结果处理表（账号级错误暂停账号、PAGE_LOAD_FAILED 首次重试、登记后非成功 → unknown）。
 *
 * 所有数据都是虚构的演示数据。
 */

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const err = (status, code, message, detail = null) => json(status, { code, message, detail });

function cents(v) {
  if (v === null || v === undefined) return null;
  const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(String(v).trim());
  if (!m) return null;
  const c = Number(m[2]) * 100 + Number((m[3] ?? "").padEnd(2, "0"));
  return m[1] === "-" ? -c : c;
}

function laDate(ms) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit" });
  const p = Object.fromEntries(f.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

function daysBetween(a, b) {
  return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86400000);
}

const ACCOUNT_LEVEL = new Set(["LOGIN_REQUIRED", "ACCOUNT_RESTRICTED", "CARD_DECLINED", "CAPTCHA_ENCOUNTERED", "IDENTITY_MISMATCH"]);
const IN_FLIGHT = new Set(["claimed", "checking", "guard_passed", "submitting", "verifying"]);

/** 输入：初始数据与选项 → 输出：{ fetch, state, calls, count(path 片段) }。 */
export function createFakeServer(opts = {}) {
  let clock = opts.now ?? Date.parse("2026-11-05T06:00:00Z");
  const state = {
    token: "tok-demo-1",
    pairingCodes: new Set(opts.pairingCodes ?? ["DEMO2345"]),
    device: { id: 5, name: "Mac-AdsPower-DEMO", bound_account_id: 3 },
    account: {
      id: 3,
      name: "环境-DEMO",
      amazon_customer_id: "A1DEMO0000",
      min_amount: "20.00",
      max_amount: "50.00",
      daily_limit: 20,
      max_delivery_days: 10,
      expected_card_last4: null,
      execution_paused: false,
      pause_reason: null,
      purchase_mode: "scheduled",
      schedule_times: ["14:00"],
      ...(opts.account ?? {}),
    },
    identityStatus: opts.identityStatus ?? "match",
    tasks: (opts.tasks ?? []).map((t, i) => ({
      id: i + 1,
      status: "assigned",
      cancelled: false,
      page_load_failures: 0,
      postal_code: "32960",
      quantity: 1,
      ...t,
    })),
    runs: new Map(),
    attempts: new Map(),
    nextRunId: 9,
    nextAttemptId: 7,
    /** 别的设备正占着这个账号时设置：{device_id, device_name, since}。 */
    foreignRun: opts.foreignRun ?? null,
  };
  const calls = [];
  /** 让匹配的请求「没说上话」：{ match: 正则, times: 次数 }。 */
  const faults = [...(opts.faults ?? [])];
  /** 每次请求后推进时钟（毫秒），用来驱动引擎的「到点补心跳」。 */
  const tickMs = opts.tickMs ?? 0;
  const hooks = opts.hooks ?? {};

  const runOf = (id) => state.runs.get(Number(id));
  const attemptOf = (id) => state.attempts.get(Number(id));
  const taskOf = (a) => state.tasks.find((t) => t.id === a.task_id);

  function guardEval(a, facts) {
    const t = taskOf(a);
    const acc = state.account;
    const checks = [];
    const fail = (code, reason) => checks.push({ code, ok: false, reason });
    const items = Array.isArray(facts.items) ? facts.items : [];
    if (items.length !== 1 || items[0].asin !== t.asin || items[0].quantity !== t.quantity) fail("ITEM_MISMATCH", "商品或数量不符");
    const zip = String(facts.ship_to?.postal_code ?? "").slice(0, 5);
    if (zip !== String(t.postal_code).slice(0, 5)) fail("ZIP_MISMATCH", "邮编不符");
    const sub = cents(facts.subtotal);
    const total = cents(facts.total);
    if (sub === null || total === null || facts.currency !== "USD") fail("DATA_UNRELIABLE", "金额不可解析或币种不是 USD");
    if (sub !== null && (sub < cents(acc.min_amount) || (acc.max_amount !== null && sub > cents(acc.max_amount)))) {
      fail("SUBTOTAL_OUT_OF_RANGE", "商品小计不在区间内");
    }
    const gift = cents(facts.gift_card_amount) ?? 0;
    if (total !== null && total + gift > cents(t.price_cap)) fail("PRICE_OVER_CAP", `总额 ${facts.total} 超过限价 ${t.price_cap}`);
    let days = null;
    if (facts.delivery?.date) days = daysBetween(laDate(clock), facts.delivery.date);
    else if (Number.isInteger(facts.delivery?.days)) days = facts.delivery.days;
    if (days === null) fail("DATA_UNRELIABLE", "配送天数不可得");
    else if (days > acc.max_delivery_days) fail("DELIVERY_TOO_LONG", `配送 ${days} 天超过上限 ${acc.max_delivery_days} 天`);
    if (acc.expected_card_last4 && facts.card_last4 !== acc.expected_card_last4) fail("CARD_MISMATCH", "卡尾号不符");
    const order = ["ITEM_MISMATCH", "ZIP_MISMATCH", "SUBTOTAL_OUT_OF_RANGE", "PRICE_OVER_CAP", "DELIVERY_TOO_LONG", "DATA_UNRELIABLE", "CARD_MISMATCH"];
    const first = order.find((c) => checks.some((x) => x.code === c)) ?? null;
    return { verdict: first ? "block" : "pass", code: first, reasons: checks.map((c) => c.reason), checks };
  }

  function finishAttempt(a, status) {
    a.status = status;
    a.finished_at = clock;
  }

  function route(path, body) {
    let m;
    if (path === "/plugin/v1/pair") {
      if (!state.pairingCodes.has(body.code)) return err(400, "VALIDATION", "配对码无效或已过期");
      state.pairingCodes.delete(body.code);
      return json(200, { device_id: state.device.id, device_token: state.token, workspace_name: "工作区-DEMO" });
    }
    if (path === "/plugin/v1/heartbeat") {
      const acc = state.account;
      return json(200, {
        device: state.device,
        account: {
          id: acc.id, name: acc.name, amazon_customer_id: acc.amazon_customer_id, purchase_mode: acc.purchase_mode,
          schedule_times: acc.schedule_times, effective_max_delivery_days: acc.max_delivery_days, purchase_enabled: true,
          execution_paused: acc.execution_paused, pause_reason: acc.pause_reason, min_amount: acc.min_amount,
          max_amount: acc.max_amount, daily_limit: acc.daily_limit, expected_card_last4: acc.expected_card_last4,
          quota_today: { limit: acc.daily_limit, used: 0, planned: state.tasks.length, available: acc.daily_limit },
        },
        identity_status: state.identityStatus,
        server_time: new Date(clock).toISOString(),
        bj_date: "2026-11-05",
        simulation_only: true,
        active_run: null,
      });
    }
    if (path === "/plugin/v1/identity") return json(200, { identity_status: state.identityStatus });

    if (path === "/plugin/v1/runs") {
      if (!state.device.bound_account_id) return err(403, "NOT_BOUND", "设备未绑定账号");
      if (body.simulated !== true) return err(403, "REAL_PURCHASE_DISABLED", "本阶段只允许模拟执行");
      if (state.identityStatus !== "match") return err(403, "IDENTITY_MISMATCH", "身份不一致");
      if (state.account.execution_paused) return err(403, "ACCOUNT_PAUSED", "账号已暂停执行");
      if (state.foreignRun) {
        return err(409, "ACCOUNT_BUSY", "账号正在其他设备执行", {
          holder_device_id: state.foreignRun.device_id,
          holder_device_name: state.foreignRun.device_name,
          since: state.foreignRun.since,
        });
      }
      const active = [...state.runs.values()].find((r) => !r.ended);
      if (active) {
        return err(409, "ACCOUNT_BUSY", "账号正在其他设备执行", {
          holder_device_id: state.device.id,
          holder_device_name: state.device.name,
          since: new Date(active.started_at).toISOString(),
        });
      }
      const run = { id: state.nextRunId++, trigger: body.trigger, started_at: clock, ended: false, stop: false, stop_reason: null, end_reason: null, beats: 0 };
      state.runs.set(run.id, run);
      return json(200, { id: run.id, lease_expires_at: new Date(clock + 180_000).toISOString() });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/heartbeat$/.exec(path))) {
      const run = runOf(m[1]);
      if (!run || run.ended) return err(409, "LEASE_LOST", "批次已结束");
      run.beats += 1;
      return json(200, { lease_expires_at: new Date(clock + 180_000).toISOString(), stop: run.stop, stop_reason: run.stop_reason });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/next$/.exec(path))) {
      const run = runOf(m[1]);
      if (!run || run.ended) return err(409, "LEASE_LOST", "批次已结束");
      const inflight = [...state.attempts.values()].find((a) => a.run_id === run.id && IN_FLIGHT.has(a.status));
      if (inflight) return err(409, "ATTEMPT_IN_FLIGHT", "本批次已有在途尝试");
      if (state.account.execution_paused) return json(200, { attempt: null, reason: "账号已暂停执行" });
      const t = state.tasks.find((x) => x.status === "assigned" && !x.cancelled);
      if (!t) return json(200, { attempt: null, reason: "今天没有可执行的任务" });
      t.status = "executing";
      const a = { id: state.nextAttemptId++, task_id: t.id, run_id: run.id, status: "claimed", submitted_at: null, guard: null, submits: 0, results: [] };
      state.attempts.set(a.id, a);
      if (hooks.afterClaim) hooks.afterClaim(t, a, state);
      return json(200, {
        attempt: {
          attempt_id: a.id, upstream_order_no: t.upstream_order_no, asin: t.asin, quantity: t.quantity,
          unit_price: t.unit_price, price_cap: t.price_cap, max_delivery_days: state.account.max_delivery_days,
          amount_range: { min: state.account.min_amount, max: state.account.max_amount },
          recipient_name: "Demo Buyer", address1: "1 Demo Way", address2: null, city: "Demo City", state: "FL",
          postal_code: t.postal_code, phone: "000-000-0000", country: "US",
          expected_card_last4: state.account.expected_card_last4, simulated: true,
        },
      });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/finish$/.exec(path))) {
      const run = runOf(m[1]);
      if (!run || run.ended) return err(409, "LEASE_LOST", "批次已结束");
      run.ended = true;
      run.end_reason = body.reason;
      for (const a of state.attempts.values()) {
        if (a.run_id === run.id && IN_FLIGHT.has(a.status)) {
          const t = taskOf(a);
          if (a.submitted_at) { finishAttempt(a, "unknown"); t.status = "result_unknown"; state.account.execution_paused = true; }
          else { finishAttempt(a, "aborted"); t.status = "assigned"; }
        }
      }
      return json(200, { ok: true });
    }

    if ((m = /^\/plugin\/v1\/attempts\/(\d+)\/(progress|guard|submitting|verifying|result)$/.exec(path))) {
      const a = attemptOf(m[1]);
      if (!a) return err(404, "NOT_FOUND", "尝试不存在");
      const run = runOf(a.run_id);
      const t = taskOf(a);
      const op = m[2];
      if (op === "progress") {
        if (!IN_FLIGHT.has(a.status)) return err(409, "CONFLICT", "尝试已结束");
        a.stage = body.stage;
        if (t.cancelled && !a.submitted_at) return json(200, { continue: false, cancel_reason: "上游已取消" });
        return json(200, { continue: true, cancel_reason: null });
      }
      if (op === "guard") {
        if (!IN_FLIGHT.has(a.status)) return err(409, "CONFLICT", "尝试已结束");
        a.guard = guardEval(a, body);
        a.status = a.guard.verdict === "pass" ? "guard_passed" : "checking";
        return json(200, a.guard);
      }
      if (op === "submitting") {
        a.submits += 1;
        if (!run || run.ended) return err(409, "LEASE_LOST", "批次已失效");
        if (!a.guard || a.guard.verdict !== "pass") return err(409, "CONFLICT", "护栏未通过");
        if (t.cancelled) return err(409, "CONFLICT", "任务已取消");
        a.submitted_at = clock;
        a.status = "submitting";
        return json(200, { ok: true, submitted_at: new Date(clock).toISOString() });
      }
      if (op === "verifying") return json(200, { ok: true });
      // result
      a.results.push(body);
      if (!IN_FLIGHT.has(a.status)) return err(409, "CONFLICT", "尝试已结束");
      if (body.outcome === "succeeded" && body.amazon_order_no) {
        if (!a.submitted_at) return err(409, "CONFLICT", "未登记提交不能报成功");
        finishAttempt(a, "succeeded"); a.amazon_order_no = body.amazon_order_no; t.status = "purchased";
      } else if (a.submitted_at) {
        finishAttempt(a, "unknown"); t.status = "result_unknown"; state.account.execution_paused = true; state.account.pause_reason = "结果待核实";
        if (run) { run.stop = true; run.stop_reason = "account_paused"; }
      } else if (ACCOUNT_LEVEL.has(body.error_code)) {
        finishAttempt(a, "failed"); t.status = "assigned"; state.account.execution_paused = true;
        if (run) { run.stop = true; run.stop_reason = "account_paused"; }
      } else if (body.error_code === "PAGE_LOAD_FAILED") {
        finishAttempt(a, "failed"); t.page_load_failures += 1; t.status = t.page_load_failures >= 2 ? "exception" : "assigned";
      } else if (body.error_code === "ABORTED_UPSTREAM_CANCELLED") {
        finishAttempt(a, "aborted"); t.status = "cancelled";
      } else {
        finishAttempt(a, "failed"); t.status = "exception"; t.status_reason = body.error_code;
      }
      return json(200, { ok: true });
    }
    return err(404, "NOT_FOUND", "Not Found");
  }

  async function fakeFetch(url, init = {}) {
    const u = new URL(url);
    const path = u.pathname;
    let body = {};
    try { body = init.body ? JSON.parse(init.body) : {}; } catch { body = {}; }
    const auth = (init.headers ?? {})["Authorization"] ?? (init.headers ?? {})["authorization"] ?? null;
    calls.push({ path, body, auth });
    const f = faults.find((x) => x.times > 0 && x.match.test(path));
    if (f) {
      f.times -= 1;
      if (f.after) { route(path, body); } // 请求到了服务端，但响应丢了
      throw new TypeError("fetch failed");
    }
    if (path !== "/plugin/v1/pair" && auth !== `Bearer ${state.token}`) return err(401, "UNAUTHORIZED", "设备令牌无效");
    const res = route(path, body);
    clock += tickMs;
    return res;
  }

  return {
    fetch: fakeFetch,
    state,
    calls,
    now: () => clock,
    setNow: (ms) => { clock = ms; },
    count: (re) => calls.filter((c) => re.test(c.path)).length,
  };
}
