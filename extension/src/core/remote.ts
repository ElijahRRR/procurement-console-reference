/** 内容脚本 → 后台的协议调用（纯逻辑，发送函数注入，Node 里可测）。
 *
 * MV3 后台随时可能被回收或重启：消息通道中途关闭、答复超时都会落到这里，统一按「没说上话」
 * （network，码 SW_UNAVAILABLE）返回。其中**可以原样再发**的几个操作（结果、进度、批次心跳、结束批次、
 * 等待验证）在这里按有界退避再发几次 —— 发的是同一份参数（结果上报体不变，服务端认得出重复）。
 * 开始批次、领取、护栏、提交登记**绝不**在这里重发：它们的响应丢了，服务端那边可能已经变了。
 */

import type { ApiResult } from "./client.js";
import type { ApiOp } from "./messages.js";

/** 后台没有响应时可以原样再发的操作。 */
export const SW_RETRY_OPS: ReadonlySet<ApiOp> = new Set<ApiOp>(["runHeartbeat", "progress", "verifying", "result", "finishRun"]);

/** 后台没有响应后的等待序列（再发 3 次）。新后台实例起来通常不到一秒。 */
export const SW_RETRY_DELAYS_MS: readonly number[] = [500, 2000, 5000];

export type SwSender = (op: ApiOp, args: unknown[]) => Promise<unknown>;

const unavailable = (message: string): ApiResult<never> => ({
  ok: false,
  error: { kind: "network", status: null, code: "SW_UNAVAILABLE", message, detail: null },
});

/** 输入：发送函数、操作、参数、等待函数与等待序列 → 输出：后台的 ApiResult；后台没有响应时按上面的规则再发。 */
export async function callSw<T>(
  send: SwSender,
  op: ApiOp,
  args: unknown[],
  opts: { sleep?: (ms: number) => Promise<void>; delaysMs?: readonly number[] } = {},
): Promise<ApiResult<T>> {
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const once = async (): Promise<ApiResult<T>> => {
    try {
      const r = await send(op, args);
      if (r && typeof r === "object" && "ok" in r) return r as ApiResult<T>;
      return unavailable("插件后台没有给出答复");
    } catch {
      // 浏览器给的原因是英文（Could not establish connection 等），不往界面上放
      return unavailable("插件后台没有响应");
    }
  };
  let r = await once();
  if (!SW_RETRY_OPS.has(op)) return r;
  for (const d of opts.delaysMs ?? SW_RETRY_DELAYS_MS) {
    if (r.ok || r.error.code !== "SW_UNAVAILABLE") break;
    await sleep(d);
    r = await once();
  }
  return r;
}
