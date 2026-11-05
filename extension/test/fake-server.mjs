/** 内存假服务端：按开发契约 v2 §4（护栏）与 §5（执行协议）、对照 backend/services/execution.py 实现插件接口，
 *  供引擎、客户端与后台测试用。
 *
 * 只实现插件这一侧会碰到的规则，但**边角路径照真实服务端的样子回**（这些地方模型一偏，引擎测试就会
 * 「过了，真跑不对」）：
 *   · 设备令牌校验（Bearer）、配对码一次性；
 *   · 开始批次的前置条件（已绑定、身份 match、未暂停、只允许 simulated）与 ACCOUNT_BUSY；
 *     返回 {run_id, lease_ttl_s, lease_expires_at}，租约按 lease_ttl_s 过期，只有批次心跳续租；
 *   · 批次心跳 stop_reason 取 account_paused / account_archived / device_unbound / identity_mismatch；
 *   · 领取（同一批次已有在途尝试 → 409 ATTEMPT_IN_FLIGHT）；
 *   · 进度：尝试已结束、上游取消、账号暂停 / 归档、身份不符 → continue=false（cancel_reason 是中文原话；
 *     cancelCodes 选项打开时另给机读的 cancel_code —— 契约 §5.7 补充，真实服务端是否已给见 README）；
 *   · 护栏：放行 → guard_passed；拦截 → 尝试**结束为 failed**、任务转异常；对已结束的尝试再判 → 409
 *     detail.attempt_status；
 *   · 提交登记：不在 guard_passed → 409（detail.attempt_status，已登记的是 submitting）、任务已取消 → 409、
 *     账号暂停 → 409 ACCOUNT_PAUSED、复核护栏不过 → 409 GUARD_BLOCKED（detail.block_code）；
 *   · 结果处理表（账号级错误暂停账号、PAGE_LOAD_FAILED 首次重试、登记后非成功 → unknown、
 *     中止类回已分配，未知错误码按任务级失败）；已结束的尝试再报同一份 → 200 duplicate；
 *     已是 unknown 而这次不同 → 409 detail.late_receipt_recorded；其他不同 → 409；
 *     未登记提交、批次已失效 → LEASE_LOST；
 *   · 结束批次：在途尝试按租约过期处理（已登记 → unknown + 暂停账号，否则 aborted）。
 *
 * 故障注入（faults：{match: 路径正则, times}）：
 *   · 默认：请求没到服务端就断了（network）；
 *   · after: true：请求到了服务端、处理了，响应丢了（network）；
 *   · resend: true：请求到了、处理了、响应丢了，**浏览器把同一个 POST 原样重发**，返回第二次的答复；
 *   · status: 500：服务端回 5xx（事务回滚，什么都没改）。
 *
 * 所有数据都是虚构的演示数据。
 */

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const err = (status, code, message, detail = null) => json(status, { code, message, detail: detail ?? {} });

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

/** 服务端 services/models.ERROR_CODES 的类别（插件会碰到的那些）。 */
const CATEGORY = {
  OUT_OF_STOCK: "task", PAGE_LOAD_FAILED: "retry_once", LOGIN_REQUIRED: "account", ACCOUNT_RESTRICTED: "account",
  CARD_DECLINED: "account", CAPTCHA_ENCOUNTERED: "account", IDENTITY_MISMATCH: "account",
  ABORTED_UPSTREAM_CANCELLED: "abort", ABORTED_BY_USER: "abort",
  ITEM_MISMATCH: "guard", ZIP_MISMATCH: "guard", SUBTOTAL_OUT_OF_RANGE: "guard", PRICE_OVER_CAP: "guard",
  DELIVERY_TOO_LONG: "guard", DATA_UNRELIABLE: "guard", CARD_MISMATCH: "guard", GUARD_BLOCKED: "guard",
  SUBMIT_REJECTED: "task", PLUGIN_SIMULATION_ONLY: "account",
};
const IN_FLIGHT = new Set(["claimed", "checking", "guard_passed", "submitting", "verifying"]);
const PRE_SUBMIT = new Set(["claimed", "checking", "guard_passed"]);
const FINAL = new Set(["succeeded", "failed", "unknown", "aborted"]);
const STATUS_ZH = { claimed: "已领取", checking: "结算中", guard_passed: "护栏已放行", submitting: "已登记提交", verifying: "等待验证", succeeded: "成功", failed: "失败", unknown: "结果待核实", aborted: "已中止" };

/** 输入：初始数据与选项 → 输出：{ fetch, state, calls, count(path 片段), now, setNow }。 */
export function createFakeServer(opts = {}) {
  let clock = opts.now ?? Date.parse("2026-11-05T06:00:00Z");
  const leaseTtlS = opts.leaseTtlS ?? 180;
  const cancelCodes = opts.cancelCodes ?? false;
  const categories = { ...CATEGORY, ...(opts.knowsAbortedByServer ? { ABORTED_BY_SERVER: "abort" } : {}) };
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
      archived: false,
      purchase_mode: "scheduled",
      schedule_times: ["14:00"],
      ...(opts.account ?? {}),
    },
    identityStatus: opts.identityStatus ?? "match",
    /** 最近一次上报的身份（心跳捎带或单独上报）。 */
    identity: null,
    identityReports: [],
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
    lateReceipts: [],
    nextRunId: 9,
    nextAttemptId: 7,
    /** 别的设备正占着这个账号时设置：{device_id, device_name, since}。 */
    foreignRun: opts.foreignRun ?? null,
  };
  const calls = [];
  /** 故障注入：见文件头。 */
  const faults = [...(opts.faults ?? [])];
  /** 每次请求后推进时钟（毫秒），用来驱动引擎的「到点补心跳」与租约过期。 */
  const tickMs = opts.tickMs ?? 0;
  const hooks = opts.hooks ?? {};

  const runOf = (id) => state.runs.get(Number(id));
  const attemptOf = (id) => state.attempts.get(Number(id));
  const taskOf = (a) => state.tasks.find((t) => t.id === a.task_id);
  const runActive = (run) => run && !run.ended && clock <= run.lease_until;
  const leaseLost = (run) =>
    err(409, "LEASE_LOST", run && run.ended ? `执行批次已结束（${run.end_reason ?? "已结束"}），请重新开始批次` : "执行权已超时（租约过期），请重新开始批次",
      { run_id: run?.id ?? null });
  const stopReason = () => {
    const acc = state.account;
    if (acc.archived) return "account_archived";
    if (acc.execution_paused) return "account_paused";
    if (state.device.bound_account_id !== acc.id) return "device_unbound";
    if (state.identityStatus !== "match") return "identity_mismatch";
    return null;
  };

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

  function finishAttempt(a, status, fields = {}) {
    Object.assign(a, fields);
    a.status = status;
    a.finished_at = clock;
  }

  /** 尝试被迫结束（结束批次、清扫）：已登记 → unknown + 暂停；否则 aborted、任务回已分配。 */
  function settleInflight(a, cause) {
    const t = taskOf(a);
    if (a.submitted_at) {
      finishAttempt(a, "unknown", { error_detail: cause });
      t.status = "result_unknown";
      state.account.execution_paused = true;
      state.account.pause_reason = "结果待核实";
    } else {
      finishAttempt(a, "aborted", { error_detail: cause });
      t.status = t.cancelled ? "cancelled" : "assigned";
    }
  }

  /** 输入：尝试、上报体 → 输出：尝试应有的结局（真实服务端 _target_status）。 */
  function targetStatus(a, body) {
    if (body.outcome === "succeeded") {
      const ok = a.submitted_at && typeof body.amazon_order_no === "string" && body.amazon_order_no.startsWith("SIM-");
      return ok ? "succeeded" : "unknown";
    }
    if (body.outcome === "unknown" || a.submitted_at) return "unknown";
    return categories[body.error_code ?? ""] === "abort" ? "aborted" : "failed";
  }

  function route(path, body) {
    let m;
    if (path === "/plugin/v1/pair") {
      if (!state.pairingCodes.has(body.code)) return err(400, "VALIDATION", "配对码无效或已过期");
      state.pairingCodes.delete(body.code);
      return json(200, { device_id: state.device.id, device_token: state.token, workspace_name: "工作区-DEMO" });
    }
    if (path === "/plugin/v1/heartbeat") {
      if (body.identity) { state.identity = body.identity; state.identityReports.push({ via: "heartbeat", ...body.identity }); }
      const acc = state.account;
      const bound = state.device.bound_account_id === acc.id && !acc.archived;
      const active = [...state.runs.values()].find((r) => !r.ended);
      return json(200, {
        device: state.device,
        account: bound ? {
          id: acc.id, name: acc.name, amazon_customer_id: acc.amazon_customer_id, purchase_mode: acc.purchase_mode,
          schedule_times: acc.schedule_times, effective_max_delivery_days: acc.max_delivery_days, purchase_enabled: true,
          execution_paused: acc.execution_paused, pause_reason: acc.pause_reason, min_amount: acc.min_amount,
          max_amount: acc.max_amount, daily_limit: acc.daily_limit, expected_card_last4: acc.expected_card_last4,
          quota_today: { limit: acc.daily_limit, used: 0, planned: state.tasks.length, available: acc.daily_limit },
        } : null,
        identity_status: state.identityStatus,
        server_time: new Date(clock).toISOString(),
        bj_date: "2026-11-05",
        simulation_only: true,
        active_run: active ? { id: active.id, device_id: state.device.id, device_name: state.device.name, is_mine: true } : null,
      });
    }
    if (path === "/plugin/v1/identity") {
      state.identity = body;
      state.identityReports.push({ via: "identity", signed_in: body.signed_in, customer_id: body.customer_id });
      return json(200, { identity_status: state.identityStatus });
    }

    if (path === "/plugin/v1/runs") {
      if (!state.device.bound_account_id) return err(403, "NOT_BOUND", "设备未绑定账号");
      if (state.identityStatus !== "match") return err(403, "IDENTITY_MISMATCH", "身份不一致");
      if (state.account.execution_paused) return err(403, "ACCOUNT_PAUSED", "账号已暂停执行");
      if (body.simulated !== true) return err(403, "REAL_PURCHASE_DISABLED", "本阶段只允许模拟执行");
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
      const run = { id: state.nextRunId++, trigger: body.trigger, started_at: clock, lease_until: clock + leaseTtlS * 1000, ended: false, stop: false, stop_reason: null, end_reason: null, finish_detail: null, beats: 0 };
      state.runs.set(run.id, run);
      return json(200, { run_id: run.id, lease_ttl_s: leaseTtlS, lease_expires_at: new Date(run.lease_until).toISOString() });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/heartbeat$/.exec(path))) {
      const run = runOf(m[1]);
      if (!runActive(run)) return leaseLost(run);
      run.beats += 1;
      run.lease_until = clock + leaseTtlS * 1000;
      const reason = run.stop ? run.stop_reason : stopReason();
      return json(200, { lease_expires_at: new Date(run.lease_until).toISOString(), stop: reason !== null, stop_reason: reason });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/next$/.exec(path))) {
      const run = runOf(m[1]);
      if (!runActive(run)) return leaseLost(run);
      const inflight = [...state.attempts.values()].find((a) => a.run_id === run.id && IN_FLIGHT.has(a.status));
      if (inflight) return err(409, "ATTEMPT_IN_FLIGHT", "本批次已有在途尝试", { attempt_id: inflight.id });
      // 真实服务端 next_attempt：账号暂停 / 归档、设备换绑、身份不符时不领取，返回原因
      const sr = stopReason();
      if (sr === "account_paused") return json(200, { attempt: null, reason: `账号已暂停执行：${state.account.pause_reason ?? "人工暂停"}` });
      if (sr === "account_archived") return json(200, { attempt: null, reason: "账号已归档，停止领取" });
      if (sr !== null) return json(200, { attempt: null, reason: "浏览器登录的买家账号与绑定账号不符，停止领取" });
      if (hooks.beforeClaim) { const r = hooks.beforeClaim(state); if (r) return json(200, r); }
      const t = state.tasks.find((x) => x.status === "assigned" && !x.cancelled);
      if (!t) return json(200, { attempt: null, reason: "今天没有可执行的任务" });
      t.status = "executing";
      const a = { id: state.nextAttemptId++, task_id: t.id, run_id: run.id, status: "claimed", submitted_at: null, guard: null, guards: 0, submits: 0, results: [] };
      state.attempts.set(a.id, a);
      if (hooks.afterClaim) hooks.afterClaim(t, a, state);
      return json(200, {
        attempt: {
          attempt_id: a.id, upstream_order_no: t.upstream_order_no, asin: t.asin, quantity: t.quantity,
          unit_price: t.unit_price, price_cap: t.price_cap, max_delivery_days: state.account.max_delivery_days,
          amount_range: { min: state.account.min_amount, max: state.account.max_amount },
          recipient_name: "Demo Buyer", address1: "1 Demo Way", address2: null, city: "Demo City", state: "FL",
          postal_code: t.postal_code, phone: "000-000-0000", country: "US",
          expected_card_last4: state.account.expected_card_last4, simulated: t.simulated ?? true,
        },
        reason: null,
      });
    }
    if ((m = /^\/plugin\/v1\/runs\/(\d+)\/finish$/.exec(path))) {
      const run = runOf(m[1]);
      if (!runActive(run)) return leaseLost(run);
      run.ended = true;
      run.end_reason = body.reason;
      run.finish_detail = body.detail ?? null;
      for (const a of state.attempts.values()) {
        if (a.run_id === run.id && IN_FLIGHT.has(a.status)) settleInflight(a, "插件结束了执行批次");
      }
      return json(200, { run_id: run.id, end_reason: run.end_reason });
    }

    if ((m = /^\/plugin\/v1\/attempts\/(\d+)\/(progress|guard|submitting|verifying|result)$/.exec(path))) {
      const a = attemptOf(m[1]);
      if (!a) return err(404, "NOT_FOUND", "采购尝试不存在");
      const run = runOf(a.run_id);
      const t = taskOf(a);
      const op = m[2];
      if (op === "progress") {
        if (!runActive(run)) return leaseLost(run);
        const stop = (code, reason) => json(200, { continue: false, cancel_reason: reason, ...(cancelCodes ? { cancel_code: code } : {}) });
        if (!IN_FLIGHT.has(a.status)) return stop("ATTEMPT_FINISHED", `本次采购尝试已结束（${STATUS_ZH[a.status]}）`);
        a.stage = body.stage;
        if (a.status === "claimed") a.status = "checking";
        if (!a.submitted_at) {
          if (t.cancelled) return stop("UPSTREAM_CANCELLED", "上游已取消");
          if (state.account.archived) return stop("ACCOUNT_ARCHIVED", "账号已归档");
          if (state.account.execution_paused) return stop("ACCOUNT_PAUSED", `账号已暂停执行：${state.account.pause_reason ?? "人工暂停"}`);
          if (state.identityStatus === "mismatch") return stop("IDENTITY_MISMATCH", "浏览器登录的买家账号（A9OTHER0000）与绑定账号 环境-DEMO（A1DEMO0000）不符，拒绝领取");
          if (state.identityStatus === "signed_out") return stop("IDENTITY_MISMATCH", "浏览器没有登录亚马逊买家账号，请先登录绑定账号 环境-DEMO");
        }
        return json(200, { continue: true, cancel_reason: null, ...(cancelCodes ? { cancel_code: null } : {}) });
      }
      if (op === "guard") {
        if (!runActive(run)) return leaseLost(run);
        a.guards += 1;
        if (a.status === "submitting" || a.status === "verifying") return err(409, "CONFLICT", "已登记提交，不能再次做护栏判定", { attempt_status: a.status });
        if (!PRE_SUBMIT.has(a.status)) return err(409, "CONFLICT", `本次采购尝试已结束（${STATUS_ZH[a.status]}）`, { attempt_status: a.status });
        a.guard = guardEval(a, body);
        if (a.guard.verdict === "pass") {
          a.status = "guard_passed";
        } else {
          // 拦截：尝试结束为 failed（错误码 = 拦截码），任务转异常 —— 和真实服务端一样，不等插件上报。
          finishAttempt(a, "failed", { error_code: a.guard.code });
          t.status = t.cancelled ? "cancelled" : "exception";
          t.status_reason = a.guard.code;
        }
        return json(200, a.guard);
      }
      if (op === "submitting") {
        a.submits += 1;
        if (!runActive(run)) return leaseLost(run);
        if (a.status !== "guard_passed" || !a.guard || a.guard.verdict !== "pass") {
          const msg = a.status === "submitting" || a.status === "verifying" ? "该尝试已登记提交，不能重复登记"
            : FINAL.has(a.status) ? `本次采购尝试已结束（${STATUS_ZH[a.status]}），不能提交` : "护栏还没有放行，不能提交";
          return err(409, "CONFLICT", msg, { attempt_status: a.status });
        }
        if (t.cancelled) return err(409, "CONFLICT", "上游已取消，不能提交", { order_status: t.status });
        if (state.account.execution_paused) return err(409, "ACCOUNT_PAUSED", `账号已暂停执行（${state.account.pause_reason ?? "人工暂停"}），不能提交`);
        if (state.identityStatus !== "match") return err(409, "IDENTITY_MISMATCH", "身份不符，不能提交", { identity_status: state.identityStatus });
        const refused = hooks.submitRefusal ? hooks.submitRefusal(a, t) : null;
        if (refused) return err(409, refused.code ?? "CONFLICT", refused.message, refused.detail ?? {});
        const recheck = hooks.recheck ? hooks.recheck(a, t) : null;
        if (recheck) return err(409, "GUARD_BLOCKED", `按最新配置复核护栏未通过：${recheck}，不能提交`, { block_code: recheck, reasons: [recheck] });
        a.submitted_at = clock;
        a.status = "submitting";
        return json(200, { ok: true, submitted_at: new Date(clock).toISOString() });
      }
      if (op === "verifying") {
        if (!runActive(run)) return leaseLost(run);
        if (a.status !== "submitting" && a.status !== "verifying") return err(409, "CONFLICT", "只有已登记提交的尝试才能进入等待验证", { attempt_status: a.status });
        a.status = "verifying";
        return json(200, { ok: true, attempt_status: "verifying" });
      }
      // result
      a.results.push(body);
      const target = targetStatus(a, body);
      if (FINAL.has(a.status)) {
        const same = a.status === target && (target === "succeeded" ? a.amazon_order_no === body.amazon_order_no : (a.error_code ?? null) === (body.error_code ?? null));
        if (same) return json(200, { attempt_status: a.status, order_status: t.status, duplicate: true });
        if (a.status === "unknown") {
          state.lateReceipts.push({ attempt_id: a.id, body });
          return err(409, "CONFLICT", "该采购尝试已登记为「结果待核实」，迟到的结果已记录，请在控制台核实",
            { attempt_status: "unknown", order_status: t.status, late_receipt_recorded: true });
        }
        return err(409, "CONFLICT", `该采购尝试的结果已登记为「${STATUS_ZH[a.status]}」，与本次上报不一致`, { attempt_status: a.status, reported: body.outcome });
      }
      if (!a.submitted_at && !runActive(run)) return leaseLost(run);
      if (target === "succeeded") {
        finishAttempt(a, "succeeded", { amazon_order_no: body.amazon_order_no }); t.status = "purchased";
        return json(200, { attempt_status: "succeeded", order_status: t.status });
      }
      if (target === "unknown") {
        finishAttempt(a, "unknown", { error_code: body.error_code ?? null }); t.status = "result_unknown";
        state.account.execution_paused = true; state.account.pause_reason = "结果待核实";
        return json(200, { attempt_status: "unknown", order_status: t.status });
      }
      if (target === "aborted") {
        finishAttempt(a, "aborted", { error_code: body.error_code }); t.status = t.cancelled ? "cancelled" : "assigned";
        return json(200, { attempt_status: "aborted", order_status: t.status });
      }
      const category = categories[body.error_code ?? ""] ?? "task";
      finishAttempt(a, "failed", { error_code: body.error_code });
      if (category === "account") {
        t.status = "assigned"; state.account.execution_paused = true; state.account.pause_reason = body.error_code;
      } else if (category === "retry_once") {
        t.page_load_failures += 1; t.status = t.page_load_failures >= 2 ? "exception" : "assigned";
      } else {
        t.status = "exception"; t.status_reason = body.error_code;
      }
      return json(200, { attempt_status: "failed", order_status: t.status });
    }
    return err(404, "NOT_FOUND", "Not Found");
  }

  async function fakeFetch(url, init = {}) {
    const u = new URL(url);
    const path = u.pathname;
    let body = {};
    try { body = init.body ? JSON.parse(init.body) : {}; } catch { body = {}; }
    const auth = (init.headers ?? {})["Authorization"] ?? (init.headers ?? {})["authorization"] ?? null;
    calls.push({ path, body, auth, at: clock });
    const f = faults.find((x) => x.times > 0 && x.match.test(path));
    const authed = path === "/plugin/v1/pair" || auth === `Bearer ${state.token}`;
    if (f) {
      f.times -= 1;
      if (f.status) return err(f.status, "INTERNAL", "服务器内部错误");
      if (f.resend) {
        if (authed) route(path, body); // 第一次：处理了，响应丢了
        calls.push({ path, body, auth, at: clock, resent: true });
        // 浏览器在复用的连接上把同一个 POST 原样重发：返回第二次的答复
      } else {
        if (f.after && authed) route(path, body); // 请求到了服务端，但响应丢了
        throw new TypeError("fetch failed");
      }
    }
    if (!authed) return err(401, "UNAUTHORIZED", "设备令牌无效");
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
    advance: (ms) => { clock += ms; },
    count: (re) => calls.filter((c) => re.test(c.path)).length,
  };
}
