"""执行协议测试用的数据工厂（虚构数据：DEMO 名称、假买家号 ID、假地址、SIM- 单号）。

在 tests/factories.py 之上补：可执行的账号、经真实配对流程得到的设备（带令牌、已绑定、身份已上报）、
已分配的任务、能通过护栏的结算页事实，以及把一笔尝试推进到某一步的快捷函数。
全部走业务服务（devices / execution / orders），只有「直接造数据」的场景才用 tests/factories。
"""

from __future__ import annotations

import itertools
from datetime import timedelta
from decimal import Decimal
from typing import Any

from services import clock, devices, execution, orders
from tests import factories as f

_seq = itertools.count(1)


def _n() -> int:
    return next(_seq)


def customer_id(n: int | None = None) -> str:
    """输入：序号 → 输出：虚构的亚马逊买家号 ID（形如 A1DEMO000001，满足 ^A[0-9A-Z]{8,}$）。"""
    return f"A1DEMO{(n if n is not None else _n()):06d}"


def exec_account(conn, ws_id: int, **kw: Any) -> dict[str, Any]:
    """输入：工作区、buyer_accounts 列 → 输出：登记了虚构买家号 ID 的账号行（缺省金额 0–不限、每日 20 单）。"""
    kw.setdefault("amazon_customer_id", customer_id())
    return f.make_account(conn, ws_id, **kw)


def identity(cid: str | None, *, signed_in: bool = True) -> dict[str, Any]:
    """输入：买家号 ID、是否已登录 → 输出：插件上报的身份。"""
    return {"signed_in": signed_in, "customer_id": cid, "site": "US",
            "page_url": "https://www.amazon.com/gp/your-account/order-history"}


def paired_device(conn, ws: dict[str, Any], *, account: dict[str, Any] | None = None, name: str | None = None,
                  report: str | None = "match") -> dict[str, Any]:
    """输入：工作区 {id, user_id}、要绑定的账号、身份上报方式（match / mismatch / signed_out / None = 不上报）
    → 输出：设备行（另附 'token' 键：明文设备令牌）。走真实的配对码 → 配对 → 绑定 → 上报身份流程。"""
    code = devices.create_pairing_code(conn, ws["id"], ws["user_id"])["code"]
    out = devices.pair(conn, code, device_name=name or f"Mac-AdsPower-DEMO-{_n():03d}", browser="adspower",
                       ext_version="0.1.0", user_agent="DEMO-UA")
    dev = devices.authenticate(conn, out["device_token"])
    if account is not None:
        devices.update_device(conn, ws["id"], dev["id"], bound_account_id=account["id"], actor_user_id=ws["user_id"])
        if report == "match":
            devices.report_identity(conn, dev, identity(account["amazon_customer_id"]))
        elif report == "mismatch":
            devices.report_identity(conn, dev, identity(customer_id(999_999)))
        elif report == "signed_out":
            devices.report_identity(conn, dev, identity(None, signed_in=False))
    row = devices.authenticate(conn, out["device_token"])
    return {**row, "token": out["device_token"]}


def refresh_identity(conn, dev: dict[str, Any], account: dict[str, Any]) -> None:
    """输入：设备、账号 → 输出：无（以当前时刻重新上报匹配的身份，时钟前移后开始批次前用）。"""
    devices.report_identity(conn, dev, identity(account["amazon_customer_id"]))


def assigned_order(conn, ws_id: int, account: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """输入：工作区、账号、记录字段（缺省 $25 × 1、限价 $40、建议采购日期今天）→ 输出：已分配给该账号的任务行。"""
    o = f.ingest_ready_order(conn, ws_id, **kw)
    if o["status"] != "queued":
        return o
    return orders.transition(conn, o, "assigned", actor_kind="system",
                             updates={"assigned_account_id": account["id"], "assignment_kind": "auto",
                                      "assigned_at": clock.now(), "assignment_note": "DEMO 分配"})


def good_facts(order: dict[str, Any], **over: Any) -> dict[str, Any]:
    """输入：任务行、要覆盖的事实键 → 输出：能通过护栏的结算页事实（总额 = 小计，3 天送达，邮编一致）。"""
    sub = orders.subtotal_eff(order)
    facts: dict[str, Any] = {
        "items": [{"asin": orders.asin_eff(order), "quantity": orders.quantity_eff(order),
                   "unit_price": f"{order['unit_price']:.2f}"}],
        "subtotal": f"{sub:.2f}", "shipping": "0.00", "tax": "0.00", "total_before_tax": f"{sub:.2f}",
        "total": f"{sub:.2f}", "gift_card_amount": None, "currency": "USD",
        "delivery": {"text": "Arriving in 3 days (DEMO)", "date": (clock.site_today() + timedelta(days=3)).isoformat(),
                     "days": None},
        "ship_to": {"postal_code": order["postal_code"]}, "card_last4": None, "fulfillment": "amazon",
    }
    facts.update(over)
    return facts


def start(conn, dev: dict[str, Any], *, trigger: str = "manual") -> int:
    """输入：设备 → 输出：新开的模拟批次 id。"""
    return execution.start_run(conn, dev, trigger=trigger, simulated=True)["run_id"]


def claim(conn, dev: dict[str, Any], run_id: int) -> dict[str, Any]:
    """输入：设备、批次 → 输出：领到的尝试载荷（领不到时断言失败并给出原因）。"""
    out = execution.next_attempt(conn, dev, run_id)
    assert out["attempt"] is not None, out["reason"]
    return out["attempt"]


def to_guard_passed(conn, dev: dict[str, Any], attempt: dict[str, Any], **facts_over: Any) -> dict[str, Any]:
    """输入：设备、尝试载荷 → 输出：护栏裁决（先报一次进度，再用 good_facts 过护栏，断言放行）。"""
    execution.progress(conn, dev, attempt["attempt_id"], stage="checkout")
    order = orders.get_row(conn, dev["workspace_id"], attempt["order_id"])
    verdict = execution.submit_guard(conn, dev, attempt["attempt_id"], good_facts(order, **facts_over))
    assert verdict["verdict"] == "pass", verdict
    return verdict


def to_submitted(conn, dev: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    """输入：设备、尝试载荷 → 输出：mark_submitting 的结果（已过护栏并登记提交）。"""
    to_guard_passed(conn, dev, attempt)
    return execution.mark_submitting(conn, dev, attempt["attempt_id"])


def sim_order_no(attempt: dict[str, Any]) -> str:
    """输入：尝试载荷 → 输出：模拟单号 SIM-<北京日期>-<尝试 id>。"""
    return f"SIM-{clock.bj_today():%Y%m%d}-{attempt['attempt_id']}"


def success_payload(attempt: dict[str, Any], order_no: str | None = None, **over: Any) -> dict[str, Any]:
    """输入：尝试载荷、单号（缺省模拟单号）→ 输出：成功结果体。"""
    sub = Decimal(attempt["subtotal"])
    body = {"outcome": "succeeded", "amazon_order_no": order_no or sim_order_no(attempt),
            "amounts": {"subtotal": f"{sub:.2f}", "shipping": "0.00", "tax": "1.50",
                        "total_before_tax": f"{sub:.2f}", "total": f"{sub + Decimal('1.50'):.2f}"},
            "card_last4": "0000", "delivery": {"text": "Arriving (DEMO)", "date": None, "days": 3},
            "error_code": None, "error_detail": None}
    body.update(over)
    return body


def failure_payload(code: str | None, detail: str | None = None, outcome: str = "failed") -> dict[str, Any]:
    """输入：错误码、说明、结局 → 输出：失败 / 结果不明的结果体。"""
    return {"outcome": outcome, "amazon_order_no": None, "amounts": None, "card_last4": None, "delivery": None,
            "error_code": code, "error_detail": detail}


def complete_success(conn, dev: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    """输入：设备、尝试载荷 → 输出：report_result 的结果（过护栏、登记提交、上报模拟成功）。"""
    to_submitted(conn, dev, attempt)
    return execution.report_result(conn, dev, attempt["attempt_id"], success_payload(attempt))


def attempt_row(conn, attempt_id: int) -> dict[str, Any]:
    """输入：尝试 id → 输出：purchase_attempts 行。"""
    return conn.execute("SELECT * FROM procure.purchase_attempts WHERE id = %s", (attempt_id,)).fetchone()


def run_row(conn, run_id: int) -> dict[str, Any]:
    """输入：批次 id → 输出：execution_runs 行。"""
    return conn.execute("SELECT * FROM procure.execution_runs WHERE id = %s", (run_id,)).fetchone()


def account_row(conn, account_id: int) -> dict[str, Any]:
    """输入：账号 id → 输出：buyer_accounts 行。"""
    return conn.execute("SELECT * FROM procure.buyer_accounts WHERE id = %s", (account_id,)).fetchone()


def order_row(conn, order_id: int) -> dict[str, Any]:
    """输入：任务 id → 输出：purchase_orders 行。"""
    return conn.execute("SELECT * FROM procure.purchase_orders WHERE id = %s", (order_id,)).fetchone()
