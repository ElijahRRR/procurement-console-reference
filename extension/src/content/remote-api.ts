/** 内容脚本里的 RunApi：每一次协议调用都是发给后台的一条 `pc.api` 消息，由后台带上设备令牌转发。
 *
 * 消息层的超时比后台单次请求的超时（15 秒 × 最多 3 次 + 退避）更长。消息层超时或后台没有响应时，
 * 一律按「没说上话」（network）返回 —— 对提交登记来说这意味着「可能已登记」，引擎会停下、不重试；
 * 可以原样再发的操作（结果、进度、批次心跳、结束批次）由 core/remote.callSw 有界地再发几次。 */

import type { ApiResult, RunApi } from "../core/client.js";
import type { ApiOp } from "../core/messages.js";
import { callSw } from "../core/remote.js";
import type { CheckoutFacts, GuardOut, NextOut, ProgressOut, ResultIn, RunHeartbeatOut, RunStartOut, Trigger } from "../core/types.js";
import { toSw } from "../ui/bridge.js";

const MESSAGE_TIMEOUT_MS = 70_000;

const send = (op: ApiOp, args: unknown[]): Promise<unknown> => toSw({ type: "pc.api", op, args }, MESSAGE_TIMEOUT_MS);

function call<T>(op: ApiOp, args: unknown[]): Promise<ApiResult<T>> {
  return callSw<T>(send, op, args);
}

export class RemoteApi implements RunApi {
  startRun(trigger: Trigger): Promise<ApiResult<RunStartOut>> {
    return call("startRun", [trigger]);
  }
  runHeartbeat(runId: number): Promise<ApiResult<RunHeartbeatOut>> {
    return call("runHeartbeat", [runId]);
  }
  next(runId: number): Promise<ApiResult<NextOut>> {
    return call("next", [runId]);
  }
  progress(attemptId: number, stage: string, detail: Record<string, unknown>): Promise<ApiResult<ProgressOut>> {
    return call("progress", [attemptId, stage, detail]);
  }
  guard(attemptId: number, facts: CheckoutFacts): Promise<ApiResult<GuardOut>> {
    return call("guard", [attemptId, facts]);
  }
  submitting(attemptId: number): Promise<ApiResult<unknown>> {
    return call("submitting", [attemptId]);
  }
  verifying(attemptId: number, note: string): Promise<ApiResult<unknown>> {
    return call("verifying", [attemptId, note]);
  }
  result(attemptId: number, body: ResultIn): Promise<ApiResult<unknown>> {
    return call("result", [attemptId, body]);
  }
  finishRun(runId: number, reason: string): Promise<ApiResult<unknown>> {
    return call("finishRun", [runId, reason]);
  }
}
