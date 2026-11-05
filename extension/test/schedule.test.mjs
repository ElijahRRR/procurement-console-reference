/** 定时执行的时间算术：执行时间一律按北京时间理解，与电脑所在时区无关。 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import {
  dayState,
  dueSlots,
  markSlots,
  nextFirePlan,
  nextFireTimes,
  normalizeSlots,
  parseSlot,
  slotEpoch,
} from "../build/core/schedule.js";
import { addDays, bjClock, bjCompact, bjDate, bjShort, laDate } from "../build/core/time.js";

const iso = (ms) => new Date(ms).toISOString();
const at = (s) => Date.parse(s);

test("nextFireTimes：北京 13:59 时，14:00 就是今天 14:00（UTC 06:00）", () => {
  const now = at("2026-11-05T05:59:00Z"); // 北京 13:59
  assert.deepEqual(nextFireTimes(["14:00"], now).map(iso), ["2026-11-05T06:00:00.000Z"]);
});

test("nextFireTimes：恰好到点时给的是明天（今天这一次由 dueSlots 判到期）", () => {
  const now = at("2026-11-05T06:00:00Z"); // 北京 14:00:00.000
  assert.deepEqual(nextFireTimes(["14:00"], now).map(iso), ["2026-11-06T06:00:00.000Z"]);
  assert.deepEqual(dueSlots(["14:00"], now, null), ["14:00"]);
});

test("nextFireTimes：北京日期已经翻到第二天、UTC 还是前一天", () => {
  const now = at("2026-11-05T17:00:00Z"); // 北京 11-06 01:00
  assert.equal(bjDate(now), "2026-11-06");
  assert.deepEqual(nextFireTimes(["09:00", "00:30"], now).map(iso), [
    "2026-11-06T01:00:00.000Z", // 北京 11-06 09:00
    "2026-11-06T16:30:00.000Z", // 北京 00:30 已过 → 北京 11-07 00:30
  ]);
});

test("nextFireTimes：日界附近（北京 23:59 与 00:00）", () => {
  const now = at("2026-11-05T15:59:30Z"); // 北京 23:59:30
  assert.deepEqual(nextFireTimes(["00:00", "23:59"], now).map(iso), [
    "2026-11-05T16:00:00.000Z", // 北京 11-06 00:00，30 秒后
    "2026-11-06T15:59:00.000Z", // 北京 23:59 已过 → 明天
  ]);
});

test("nextFireTimes：按触发时刻升序，去重、跳过不合法项", () => {
  const now = at("2026-11-05T02:00:00Z"); // 北京 10:00
  const got = nextFirePlan(["18:30", "9:05", "bad", "24:00", "12:60", "18:30", 7, null], now);
  assert.deepEqual(got.map((p) => p.slot), ["18:30", "09:05"]);
  assert.deepEqual(got.map((p) => iso(p.at)), ["2026-11-05T10:30:00.000Z", "2026-11-06T01:05:00.000Z"]);
  assert.deepEqual(nextFireTimes("14:00", now), []);
  assert.deepEqual(nextFireTimes([], now), []);
});

test("nextFireTimes：北京时间没有夏令时，跨美国与欧洲夏令时切换日仍然恰好相隔 24 小时", () => {
  for (const day of ["2026-03-07T12:00:00Z", "2026-03-28T12:00:00Z", "2026-10-31T12:00:00Z", "2026-11-01T00:00:00Z"]) {
    let t = at(day);
    let prev = null;
    for (let i = 0; i < 3; i++) {
      const [fire] = nextFireTimes(["14:00"], t);
      if (prev !== null) assert.equal(fire - prev, 24 * 3600_000);
      assert.equal(bjClock(fire), "14:00:00");
      prev = fire;
      t = fire;
    }
  }
});

test("nextFireTimes 与本机时区无关：在洛杉矶、伦敦、上海时区的进程里结果一致", () => {
  const mod = new URL("../build/core/schedule.js", import.meta.url).href;
  const script = `import(${JSON.stringify(mod)}).then(m => {
    const now = Date.parse("2026-11-01T09:30:00Z");
    console.log(JSON.stringify(m.nextFireTimes(["14:00", "02:15"], now)));
  });`;
  const outs = ["America/Los_Angeles", "Europe/London", "Asia/Shanghai", "UTC"].map((tz) =>
    execFileSync(process.execPath, ["--input-type=module", "-e", script], {
      env: { ...process.env, TZ: tz },
      cwd: fileURLToPath(new URL(".", import.meta.url)),
    }).toString().trim(),
  );
  assert.equal(new Set(outs).size, 1, outs.join(" | "));
  assert.deepEqual(JSON.parse(outs[0]).map(iso), ["2026-11-01T18:15:00.000Z", "2026-11-02T06:00:00.000Z"]);
});

test("parseSlot / normalizeSlots / slotEpoch", () => {
  assert.deepEqual(parseSlot("9:05"), { h: 9, m: 5 });
  assert.deepEqual(parseSlot("14:00:00"), { h: 14, m: 0 });
  assert.equal(parseSlot("24:00"), null);
  assert.equal(parseSlot("14:7"), null);
  assert.equal(parseSlot(1400), null);
  assert.deepEqual(normalizeSlots(["18:00", "9:00", "09:00"]), ["09:00", "18:00"]);
  assert.equal(iso(slotEpoch("2026-11-05", "14:00")), "2026-11-05T06:00:00.000Z");
  assert.equal(slotEpoch("2026/11/05", "14:00"), null);
});

test("dueSlots：错过的时间点在同一个北京日期内补执行一次", () => {
  // 电脑 15:20（北京）才开机，14:00 已过 → 到期；18:00 还没到 → 不到期
  const now = at("2026-11-05T07:20:00Z");
  const times = ["14:00", "18:00"];
  assert.deepEqual(dueSlots(times, now, null), ["14:00"]);
  const st = markSlots(null, ["14:00"], now);
  assert.equal(st.bj_date, "2026-11-05");
  assert.deepEqual(dueSlots(times, now, st), [], "补执行过一次之后不再到期");
  // 18:00 到点
  const evening = at("2026-11-05T10:00:30Z");
  assert.deepEqual(dueSlots(times, evening, st), ["18:00"]);
});

test("dueSlots：跨天不补 —— 昨天没跑的，今天到点之前不到期", () => {
  const yesterday = markSlots(null, [], at("2026-11-05T07:00:00Z"));
  const nextMorning = at("2026-11-06T01:00:00Z"); // 北京 11-06 09:00
  assert.deepEqual(dueSlots(["14:00"], nextMorning, yesterday), []);
  // 昨天的记录整份作废：今天 14:00 照常到期
  const done = markSlots(null, ["14:00"], at("2026-11-05T07:00:00Z"));
  assert.deepEqual(dueSlots(["14:00"], at("2026-11-06T06:01:00Z"), done), ["14:00"]);
  assert.deepEqual(dayState(done, nextMorning), { bj_date: "2026-11-06", done: [], waiting_logged: [] });
});

test("markSlots 不修改入参，另记「等待环境」只记一次", () => {
  const now = at("2026-11-05T07:00:00Z");
  const a = markSlots(null, ["14:00"], now, "waiting_logged");
  const b = markSlots(a, ["14:00"], now, "waiting_logged");
  assert.deepEqual(a.waiting_logged, ["14:00"]);
  assert.deepEqual(b.waiting_logged, ["14:00"]);
  assert.deepEqual(a.done, []);
});

test("时间工具：北京日期、洛杉矶日期（含夏令时）、日期加减", () => {
  assert.equal(bjCompact(at("2026-10-31T03:00:00Z")), "20261031");
  assert.equal(bjShort("2026-10-31T13:31:00+00:00"), "10-31 21:31");
  assert.equal(bjShort("not a date"), null);
  // 洛杉矶 2026-11-01 02:00 夏令时结束
  assert.equal(laDate(at("2026-11-01T06:59:00Z")), "2026-10-31"); // PDT 23:59
  assert.equal(laDate(at("2026-11-02T07:59:00Z")), "2026-11-01"); // PST 23:59
  assert.equal(laDate(at("2026-11-02T08:00:00Z")), "2026-11-02");
  assert.equal(addDays("2026-12-30", 3), "2027-01-02");
  assert.equal(addDays("2028-02-28", 1), "2028-02-29");
});
