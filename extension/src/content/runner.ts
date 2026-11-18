/** 内容脚本里的执行宿主：把引擎、标签页租约、单飞闸接在一起。
 *
 * 三道互斥，各管一层：
 *   · 单飞闸（本标签页）：已经在执行时，再点「开始执行」或定时到点都不再开第二个；
 *   · 标签页租约（本浏览器）：同一时刻只有一个亚马逊标签页去申请执行批次；
 *   · 服务端执行批次（全局权威）：同一个账号跨电脑、跨浏览器只有一个批次（409 ACCOUNT_BUSY），
 *     同一个浏览器也只有一个（409 DEVICE_BUSY）。
 *
 * 续租（标签页租约每 60 秒、批次心跳每 min(35 秒, 租约有效期 / 3)）跑在定时器上、在单飞闸外面：
 * 后台标签页的定时器会被节流到约 60 秒一次；定时器再慢时，引擎在每一步之前也会按时钟补发一次
 * 批次心跳，后台还有一路兜底心跳（只在本页对 pc.ping 有答复时才发）。
 *
 * 开始执行（规格 §3）：**当前页**重读身份 → 交后台对账（其余亚马逊标签页也各自重读：本页可能是几小时前
 * 打开的，别的标签页里已经登录了另一个买家号）；有分歧 → 不开始；否则后台先把结论报给服务端
 * （POST /identity），再申请批次 POST /runs {customer_id: 当前页读到的买家号 ID, trigger, simulated: true}。
 * 这一页自己没读到买家号 ID 就不开始 —— 不拿别的标签页读到的 ID 在这一页上领任务。
 * 执行中引擎还会在提交登记之前、以及每隔几分钟请后台再来一遍（pc.refreshIdentity，不发心跳）：
 * 服务端在领取、进度与提交登记时都要求登录状态 10 分钟内上报过。
 */

import { codeLabel } from "../core/codes.js";
import { RunEngine, type EngineView, type RunSummary } from "../core/engine.js";
import { SingleFlight } from "../core/gates.js";
import type { IdentitySnapshot } from "../core/identity.js";
import type { LogLevel } from "../core/log.js";
import type { LeaseReply, Msg, PrepareReply, RefreshIdentityReply, ScheduledReply } from "../core/messages.js";
import { pageStartProblem, replyForPage, replyFromNotice, replyFromPrepare } from "../core/start.js";
import type { Trigger } from "../core/types.js";
import { fetchState, swLog, toSw } from "../ui/bridge.js";
import { RemoteApi } from "./remote-api.js";

const LEASE_RENEW_MS = 60_000;
/** 执行中请后台重读、上报身份的答复上界（每个标签页 5 秒，并行）。 */
const REFRESH_IDENTITY_MS = 20_000;

/** 输入：无 → 输出：后台的答复。后台没有响应时抛错（引擎记一行，照常走；服务端按身份新鲜度裁决）。 */
function refreshIdentity(): Promise<RefreshIdentityReply> {
  return toSw<RefreshIdentityReply>({ type: "pc.refreshIdentity" }, REFRESH_IDENTITY_MS);
}

/** 定时触发带来的：向服务端申请时带的时间点、定时所属的买家号 ID。 */
export interface ScheduledStart {
  slot: string;
  customerId: string;
}

export class RunHost {
  private readonly flight = new SingleFlight();
  private engine: RunEngine | null = null;
  private lastSummary: RunSummary | null = null;
  private readonly listeners = new Set<() => void>();

  /** readPage：读这一页此刻的身份（不上报）；登录状态读不出来为 null。 */
  constructor(private readonly readPage: () => IdentitySnapshot | null) {}

  get running(): boolean {
    return this.flight.busy;
  }

  /** 输入：无 → 输出：本页引擎的当前状态（没在执行为 null）。 */
  view(): EngineView | null {
    return this.flight.busy ? this.engine?.state() ?? null : null;
  }

  /** 输入：无 → 输出：本页上一次批次的总结。 */
  last(): RunSummary | null {
    return this.lastSummary;
  }

  onChange(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }

  /** 输入：停止原因 → 输出：无。当前这一笔结束后停止。 */
  stop(reason: string): void {
    this.engine?.requestStop(reason);
  }

  /** 输入：触发方式（定时触发时带上时间点与所属的买家号 ID）→ 输出：开始那一步的结论（批次本身在后台继续跑）。 */
  start(trigger: Trigger, sched: ScheduledStart | null = null): Promise<ScheduledReply> {
    return new Promise<ScheduledReply>((resolve) => {
      let replied = false;
      const reply = (r: ScheduledReply): void => {
        if (!replied) {
          replied = true;
          resolve(r);
        }
      };
      this.flight
        .run(() => this.runOnce(trigger, sched, reply))
        .then(
          (res) => {
            if (!res.ran) reply({ result: "busy", code: "ALREADY_RUNNING", message: codeLabel("ALREADY_RUNNING") });
            else reply({ result: "rejected", code: null, message: res.value?.message ?? "执行已结束" });
          },
          (e: unknown) => {
            console.error("采购助手执行出错", e); // 原始的英文报错只进控制台，不进界面
            reply({ result: "rejected", code: null, message: "开始执行失败：页面内部错误，请刷新页面后再试" });
          },
        );
    });
  }

  private async lease(busy: boolean): Promise<LeaseReply> {
    try {
      const r = await toSw<LeaseReply>({ type: "pc.lease", busy });
      return r && typeof r.granted === "boolean" ? r : { granted: false, held_by: null, reason: "held" };
    } catch {
      return { granted: false, held_by: null, reason: "held" };
    }
  }

  private sendView(view: EngineView | null, active: boolean): void {
    const msg: Msg = { type: "pc.runView", view: view as EngineView, active };
    void toSw(msg).catch(() => undefined);
    this.emit();
  }

  private async runOnce(trigger: Trigger, sched: ScheduledStart | null, reply: (r: ScheduledReply) => void): Promise<RunSummary | null> {
    const state = await fetchState();
    if (!state) {
      reply({ result: "not_ready", code: "SW_UNAVAILABLE", message: `开始执行失败：${codeLabel("SW_UNAVAILABLE")}` });
      return null;
    }

    // 当前页重读身份。这一页自己读不到买家号 ID 就不开始；定时触发时请后台换下一个标签页。
    const page = this.readPage();
    const problem = pageStartProblem(page, sched ? sched.customerId : null);
    if (problem || !page?.customer_id) {
      reply(replyForPage(problem ?? { code: "NO_CUSTOMER_ID", why: codeLabel("NO_CUSTOMER_ID") }, sched !== null));
      return null;
    }
    const customerId = page.customer_id;

    // 定时触发没开始成时，这边不写日志：后台对同一个时间点只写一次原因。占用（ACCOUNT_BUSY / DEVICE_BUSY）
    // 要每分钟再问一次，每次都写的话几十分钟就把 50 行日志刷没了。开始成功之后把攒下的几行按顺序补上。
    let began = sched === null;
    const held: Array<[LogLevel, string]> = [];
    const log = (level: LogLevel, text: string): void => {
      if (began) swLog(level, text);
      else held.push([level, text]);
    };

    const lease = await this.lease(true);
    if (!lease.granted) {
      const message = `开始执行失败：${codeLabel("TAB_BUSY")}`;
      log("warn", message);
      reply({ result: "busy", code: "TAB_BUSY", message });
      return null;
    }

    let renew: ReturnType<typeof setInterval> | undefined;
    const engine = new RunEngine({
      api: new RemoteApi(),
      outcome: state.settings.simulationOutcome,
      log,
      onView: (v) => this.sendView(v, v.phase !== "ended"),
      ownDeviceId: state.device.device_id,
      refreshIdentity,
      // 护栏事实里的买家号 ID：取事实那一刻这一页上的读数（模拟执行不打开结算页，就是当前页面）。
      readCustomerId: () => this.readPage()?.customer_id ?? null,
    });
    this.engine = engine;
    this.emit();
    try {
      // 交后台对账并上报：其余亚马逊标签页也重读；有分歧、没报上去、本浏览器上一个批次还没结束，都不开始。
      const prep = await toSw<PrepareReply>({ type: "pc.prepareStart", identity: page }, 45_000).catch(() => null);
      const refused = replyFromPrepare(prep, customerId);
      if (refused) {
        log("err", refused.message);
        reply(refused);
        return null;
      }
      renew = setInterval(() => {
        void this.lease(true).then((l) => {
          if (!l.granted) engine.requestStop("本页的执行租约被另一个标签页取得");
        });
      }, LEASE_RENEW_MS);
      const summary = await engine.run(
        trigger,
        customerId,
        (n) => {
          if (n.started && !began) {
            began = true;
            for (const [level, text] of held.splice(0)) swLog(level, text);
          }
          reply(replyFromNotice(n));
        },
        sched?.slot ?? null,
      );
      this.lastSummary = summary;
      return summary;
    } finally {
      if (renew !== undefined) clearInterval(renew);
      await toSw({ type: "pc.leaseRelease" }).catch(() => undefined);
      this.sendView(engine.state(), false);
    }
  }
}
