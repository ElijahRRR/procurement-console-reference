"""插件执行协议（契约 §5，前缀 /plugin/v1）：配对、心跳、身份、执行批次、领取、进度、护栏、提交登记、等待验证、结果、结束。

- 除 /pair 外都要 `Authorization: Bearer <设备令牌>`（缺失、未知或已撤销 → 401 UNAUTHORIZED）。
- 不要求控制台的防跨站请求头（插件从扩展的 service worker 带令牌调用）。
- 工作区由设备决定，请求不能指定；批次 / 尝试不属于这台设备 → 404。
- 业务判断全部在 services/devices 与 services/execution；这里只做参数解析与事务边界。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from server import deps
from server.serialize import error_response, respond
from services import devices, execution

router = APIRouter(tags=["plugin"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PairBody(_Body):
    code: Any = None
    device_name: Any = None
    browser: Any = None
    ext_version: Any = None


class HeartbeatBody(_Body):
    ext_version: Any = None
    identity: Any = None


class IdentityBody(_Body):
    signed_in: Any = None
    customer_id: Any = None
    site: Any = None
    page_url: Any = None
    ext_version: Any = None


class StartRunBody(_Body):
    trigger: Any = "manual"
    simulated: Any = None


class FinishBody(_Body):
    reason: Any = "finished"
    detail: Any = None


class ProgressBody(_Body):
    stage: Any = None
    detail: Any = None


class GuardFacts(BaseModel):
    """结算页事实（契约 §4）：交给服务端护栏；服务层先规范化（只留契约的键、卡号只留后 4 位）再判定与入库，
    缺项、类型不对由护栏判为数据不可靠。"""

    model_config = ConfigDict(extra="allow")


class VerifyingBody(_Body):
    note: Any = None


class ResultBody(_Body):
    outcome: Any = None
    amazon_order_no: Any = None
    amounts: Any = None
    card_last4: Any = None
    delivery: Any = None
    error_code: Any = None
    error_detail: Any = None


@router.post("/pair")
def pair(body: PairBody, request: Request, conn=Depends(deps.db_conn)):
    """输入：{code, device_name, browser, ext_version}（不需要令牌）→ 输出：{device_id, device_token, workspace_name}。"""
    return respond(devices.pair(conn, body.code, device_name=body.device_name, browser=body.browser,
                                ext_version=body.ext_version, user_agent=request.headers.get("user-agent")))


@router.post("/heartbeat")
def heartbeat(body: HeartbeatBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：{ext_version, identity?} → 输出：{device, account, identity_status, server_time, bj_date, simulation_only,
    active_run}（契约 §10「插件心跳 / 配置」）。"""
    b = body or HeartbeatBody()
    return respond(devices.heartbeat(ctx.conn, ctx.device, ext_version=b.ext_version, identity=b.identity))


@router.post("/identity")
def identity(body: IdentityBody, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：{signed_in, customer_id, site, page_url, ext_version?} → 输出：{identity_status}（单独上报身份；
    契约 §5 第 3 步；等价于心跳捎带 identity）。"""
    ident = body.model_dump(include={"signed_in", "customer_id", "site", "page_url"})
    return respond({"identity_status": devices.report_identity(ctx.conn, ctx.device, ident)})


@router.post("/runs")
def start_run(body: StartRunBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：{trigger: manual | scheduled, simulated: true} → 输出：{run_id, lease_ttl_s, lease_expires_at}；
    账号已有未结束批次 → 409 ACCOUNT_BUSY（detail 为持有者）；非模拟 → 403 REAL_PURCHASE_DISABLED。"""
    b = body or StartRunBody()
    return respond(execution.start_run(ctx.conn, ctx.device, trigger=b.trigger, simulated=b.simulated))


@router.post("/runs/{run_id}/heartbeat")
def run_heartbeat(run_id: int, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：批次 id → 输出：{lease_expires_at, stop, stop_reason}（续租）；批次已失效 → 409 LEASE_LOST。"""
    return respond(execution.run_heartbeat(ctx.conn, ctx.device, run_id))


@router.post("/runs/{run_id}/next")
def next_attempt(run_id: int, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：批次 id → 输出：{attempt: 载荷 | null, reason}；本批次已有在途尝试 → 409 ATTEMPT_IN_FLIGHT。"""
    return respond(execution.next_attempt(ctx.conn, ctx.device, run_id))


@router.post("/runs/{run_id}/finish")
def finish_run(run_id: int, body: FinishBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：批次 id、{reason, detail?} → 输出：{run_id, ended_at, end_reason, stats}。

    detail（可选）记进 run.ended 事件的 plugin_detail：字符串去掉首尾空白后截到 200 字，其他 JSON 先转成文本再截。
    """
    b = body or FinishBody()
    return respond(execution.finish_run(ctx.conn, ctx.device, run_id, b.reason, b.detail))


@router.post("/attempts/{attempt_id}/progress")
def progress(attempt_id: int, body: ProgressBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：尝试 id、{stage, detail} → 输出：{continue, cancel_reason, cancel_code}。

    continue=true 时 cancel_code 为 null；continue=false 时 cancel_code 是 UPSTREAM_CANCELLED / ACCOUNT_ARCHIVED /
    ACCOUNT_PAUSED / IDENTITY_MISMATCH / IDENTITY_STALE / SOURCE_HOLD / SOURCE_CHANGED（都只在未登记提交时出现）
    或 ATTEMPT_FINISHED（尝试已结束）。插件随后上报：UPSTREAM_CANCELLED → ABORTED_UPSTREAM_CANCELLED，
    ATTEMPT_FINISHED → 不再上报，其余 → ABORTED_BY_SERVER。
    """
    b = body or ProgressBody()
    return respond(execution.progress(ctx.conn, ctx.device, attempt_id, stage=b.stage, detail=b.detail))


@router.post("/attempts/{attempt_id}/guard")
def guard(attempt_id: int, body: GuardFacts | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：尝试 id、结算页事实 → 输出：{verdict: pass | block, code, reasons, checks, …}（服务端护栏裁决）。"""
    facts = body.model_dump() if body is not None else {}
    return respond(execution.submit_guard(ctx.conn, ctx.device, attempt_id, facts))


@router.post("/attempts/{attempt_id}/submitting")
def submitting(attempt_id: int, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：尝试 id → 输出：{ok: true, submitted_at}（200 才允许点击下单；护栏未放行、批次失效、任务已取消 → 409）。"""
    return respond(execution.mark_submitting(ctx.conn, ctx.device, attempt_id))


@router.post("/attempts/{attempt_id}/verifying")
def verifying(attempt_id: int, body: VerifyingBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：尝试 id、{note} → 输出：{ok: true, attempt_status: 'verifying'}（银行人工验证，任务与名额保留）。"""
    b = body or VerifyingBody()
    return respond(execution.mark_verifying(ctx.conn, ctx.device, attempt_id, b.note))


@router.post("/attempts/{attempt_id}/result")
def result(attempt_id: int, body: ResultBody, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：尝试 id、结果 {outcome, amazon_order_no, amounts, …} → 输出：{attempt_status, order_status}。

    结果体字段：outcome、amazon_order_no、amounts、card_last4、delivery、error_code、error_detail（契约 §5 第 11 步）；
    重复上报相同结果时另有 duplicate: true。尝试已是「结果待核实」、或已由控制台核实（已下单 / 未下单）时收到的
    不同结果（迟到的回执）：回执照样保存（事务提交），响应为 409 CONFLICT，detail = {attempt_status, order_status,
    late_receipt_recorded: true}。
    """
    out = execution.report_result(ctx.conn, ctx.device, attempt_id, body.model_dump())
    if out.get("late_receipt"):
        return error_response("CONFLICT", out["message"], 409,
                              {"attempt_status": out["attempt_status"], "order_status": out["order_status"],
                               "late_receipt_recorded": True})
    return respond(out)
