/** 内容脚本里的执行宿主：把引擎、标签页租约、单飞闸接在一起。
 *
 * 三道互斥，各管一层：
 *   · 单飞闸（本标签页）：已经在执行时，再点「开始执行」或定时到点都不再开第二个；
 *   · 标签页租约（本浏览器）：同一时刻只有一个亚马逊标签页去申请执行批次；
 *   · 服务端执行批次（全局权威）：同一账号跨电脑、跨浏览器只有一个批次，冲突时 409 ACCOUNT_BUSY。
 *
 * 续租（标签页租约每 60 秒、批次心跳每 35 秒）跑在定时器上、在单飞闸外面：
 * 后台标签页的定时器会被节流到约 60 秒一次，仍在两者的有效期（5 分钟、180 秒）之内；
 * 定时器再慢时，引擎在每次领取之前也会按时钟补发一次批次心跳，后台还有一路兜底心跳。
 */

import { codeLabel } from "../core/codes.js";
import { RunEngine, type EngineView, type RunSummary } from "../core/engine.js";
import { SingleFlight } from "../core/gates.js";
import type { LeaseReply, Msg, ScheduledReply } from "../core/messages.js";
import type { Trigger } from "../core/types.js";
import { fetchState, swLog, toSw } from "../ui/bridge.js";
import { RemoteApi } from "./remote-api.js";

const LEASE_RENEW_MS = 60_000;

export class RunHost {
  private readonly flight = new SingleFlight();
  private engine: RunEngine | null = null;
  private lastSummary: RunSummary | null = null;
  private readonly listeners = new Set<() => void>();

  /** readIdentity：重新读一次本页身份并强制上报（开始批次前要让服务端拿到新鲜身份）。 */
  constructor(private readonly readIdentity: (force: boolean) => Promise<unknown>) {}

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

  /** 输入：触发方式 → 输出：开始那一步的结论（批次本身在后台继续跑）。 */
  start(trigger: Trigger): Promise<ScheduledReply> {
    return new Promise<ScheduledReply>((resolve) => {
      let replied = false;
      const reply = (r: ScheduledReply): void => {
        if (!replied) {
          replied = true;
          resolve(r);
        }
      };
      this.flight
        .run(() => this.runOnce(trigger, reply))
        .then(
          (res) => {
            if (!res.ran) reply({ result: "busy", message: codeLabel("ALREADY_RUNNING") });
            else reply({ result: "rejected", message: res.value?.message ?? "执行已结束" });
          },
          (e: unknown) => reply({ result: "rejected", message: `开始执行失败：${e instanceof Error ? e.message : String(e)}` }),
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

  private async runOnce(trigger: Trigger, reply: (r: ScheduledReply) => void): Promise<RunSummary | null> {
    const state = await fetchState();
    if (!state) {
      reply({ result: "not_ready", message: codeLabel("SW_UNAVAILABLE") });
      return null;
    }
    if (!state.device.paired) {
      reply({ result: "not_ready", message: codeLabel("NOT_PAIRED") });
      return null;
    }
    const lease = await this.lease(true);
    if (!lease.granted) {
      const message = `开始执行失败：${codeLabel("TAB_BUSY")}`;
      swLog("warn", message);
      reply({ result: "busy", message });
      return null;
    }

    let renew: ReturnType<typeof setInterval> | undefined;
    const engine = new RunEngine({
      api: new RemoteApi(),
      outcome: state.settings.simulationOutcome,
      log: (level, text) => swLog(level, text),
      onView: (v) => this.sendView(v, v.phase !== "ended"),
      ownDeviceId: state.device.device_id,
    });
    this.engine = engine;
    this.emit();
    try {
      // 让服务端拿到这一刻的身份（开始批次要求 10 分钟内上报过、且与绑定账号一致）。
      await this.readIdentity(true).catch(() => undefined);
      renew = setInterval(() => {
        void this.lease(true).then((l) => {
          if (!l.granted) engine.requestStop("本页的执行租约被另一个标签页取得");
        });
      }, LEASE_RENEW_MS);
      const summary = await engine.run(trigger, (n) => {
        if (n.started) reply({ result: "started", message: n.message });
        else if (n.status === "busy") reply({ result: "busy", message: n.message });
        else if (n.error?.kind === "network") reply({ result: "network", message: n.message });
        else reply({ result: "rejected", message: n.message });
      });
      this.lastSummary = summary;
      return summary;
    } finally {
      if (renew !== undefined) clearInterval(renew);
      await toSw({ type: "pc.leaseRelease" }).catch(() => undefined);
      this.sendView(engine.state(), false);
    }
  }
}
