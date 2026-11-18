"""插件执行协议（契约 §5，前缀 /plugin/v1）：登记、心跳、身份、执行批次、领取、进度、护栏、提交登记、等待验证、结果、结束。

- 整个前缀先过入口闸（server/limits.PluginGateMiddleware）：只接受本机对端、本机 Host、非网页来源，POST 必须是 JSON。
- /register 不需要令牌（插件自己生成令牌来登记，没有任何人工步骤）；其余接口都要
  `Authorization: Bearer <浏览器令牌>`，缺失或没登记过 → 401 UNAUTHORIZED，插件收到后用同一个令牌重新登记。
- 令牌只是会话标识（把批次、尝试、回执归到同一个浏览器），不代表身份。买家号的身份是插件从亚马逊页面读到并上报的
  买家号 ID（customerId）；开始批次时按它找账号，浏览器与账号之间没有绑定。
- 不要求控制台的防跨站请求头。
- 工作区是浏览器登记时确定的，之后的请求不能指定；批次 / 尝试不属于这个浏览器 → 404。
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


class RegisterBody(_Body):
    device_token: Any = None
    ext_version: Any = None
    workspace_id: Any = None


class HeartbeatBody(_Body):
    ext_version: Any = None
    identity: Any = None


#: 身份上报体里交给服务层的键（契约 §5；服务层清洗，买家号 ID 形状不对当作没读到）
IDENTITY_KEYS = ("signed_in", "customer_id", "site", "page_url", "conflict")


class IdentityBody(_Body):
    signed_in: Any = None
    customer_id: Any = None
    site: Any = None
    page_url: Any = None
    conflict: Any = None
    ext_version: Any = None


class StartRunBody(_Body):
    """customer_id 在这里是可选的：旧版插件不带它，由服务层回「插件版本过旧，请更新插件」，而不是通用的参数错误。"""

    customer_id: Any = None
    trigger: Any = "manual"
    simulated: Any = None
    slot: Any = None


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


@router.post("/register")
def register(request: Request, body: RegisterBody | None = None, conn=Depends(deps.db_conn)):
    """输入：{device_token, ext_version?, workspace_id?}（不需要令牌头；令牌由插件自己生成，放在请求体里）
    → 输出：{device_id, device_name, workspace_id, workspace_name, created}。同一个令牌重复登记是幂等的（created=false）。

    令牌格式不对 → 422 VALIDATION（detail.field = device_token）；服务端还没有任何工作区 → 409 CONFLICT；有多个
    工作区而没带存在的 workspace_id → 409 WORKSPACE_REQUIRED；插件版本低于 0.2.0 → 409 PLUGIN_OUTDATED；工作区里
    登记的浏览器已到上限 → 429 RATE_LIMITED。
    """
    b = body or RegisterBody()
    return respond(devices.register(conn, b.device_token, ext_version=b.ext_version, workspace_id=b.workspace_id,
                                    user_agent=request.headers.get("user-agent")))


@router.post("/heartbeat")
def heartbeat(body: HeartbeatBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：{ext_version?, identity?} → 输出：{device: {id, name}, login: {state, customer_id, at, stale}, account,
    device_run, server_time, bj_date, simulation_only}（契约 §10「插件心跳」）。

    login.state：unknown / signed_out / no_id / conflict / unregistered / ignored / ready；account 只在 ready 时非空。
    插件版本低于 0.2.0 → 409 PLUGIN_OUTDATED。
    """
    b = body or HeartbeatBody()
    return respond(devices.heartbeat(ctx.conn, ctx.device, ext_version=b.ext_version, identity=b.identity))


@router.post("/identity")
def identity(body: IdentityBody, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：{signed_in, customer_id, site, page_url, conflict?, ext_version?} → 输出：与 /heartbeat 完全相同的形状
    （单独上报身份；等价于心跳捎带 identity）。买家号 ID 形状不对时当作没读到，不报错。"""
    return respond(devices.report_identity(ctx.conn, ctx.device, body.model_dump(include=set(IDENTITY_KEYS)),
                                           ext_version=body.ext_version))


@router.post("/runs")
def start_run(body: StartRunBody | None = None, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：{customer_id, trigger: manual | scheduled, slot?: "HH:MM", simulated: true}
    → 输出：{run_id, lease_ttl_s, lease_expires_at, account: {id, name}}。customer_id 是插件在当前页面读到的买家号 ID。

    拒绝：缺 customer_id → 422 VALIDATION「插件版本过旧，请更新插件」；这个浏览器上一个批次没结束 → 409 DEVICE_BUSY；
    浏览器最近上报的登录状态与 customer_id 不符或已过期 → 403 IDENTITY_MISMATCH；没有账号登记这个 ID → 403
    ACCOUNT_NOT_REGISTERED；账号已暂停 → 403 ACCOUNT_PAUSED；非模拟 → 403 REAL_PURCHASE_DISABLED；定时批次的
    slot 不对 → 422，这个时间点今天已执行过 → 409 SLOT_DONE；账号已有未结束批次 → 409 ACCOUNT_BUSY（detail 为持有者）。
    """
    b = body or StartRunBody()
    return respond(execution.start_run(ctx.conn, ctx.device, customer_id=b.customer_id, trigger=b.trigger,
                                       simulated=b.simulated, slot=b.slot))


@router.post("/runs/{run_id}/heartbeat")
def run_heartbeat(run_id: int, ctx: deps.Plugin = Depends(deps.plugin)):
    """输入：批次 id → 输出：{lease_expires_at, stop, stop_reason}（续租）；批次已失效 → 409 LEASE_LOST。

    stop_reason：account_paused / account_archived / identity_mismatch（浏览器登录的买家号不再是批次账号登记的那个）。
    """
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
    """输入：尝试 id、结算页事实（含取事实那一刻页面上读到的 customer_id）→ 输出：{verdict: pass | block, code,
    reasons, checks, …}（服务端护栏裁决）。"""
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
