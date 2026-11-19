"""HTTP：来源指定买家号（方案 v2「修订二」；契约 §3.2、§7、§8、§10）在接口上露出来的每一个形状——

- 导入预览里买家号两列的问题（占位写法、表格错误值、买家号 ID 格式不对）；提交后的分配摘要（stats.designated、
  unassigned[].designated）；任务列表行与详情的 designated_buyer、assignment_kind = source；
- GET /allocation/last 读旧记录不坏；POST /allocation/run 重复执行结果相同；
- GET /accounts/discovered 并入来源指定的买家号 ID（source_orders、browsers 为空、include_ignored 列出全部忽略的 ID）、
  概览 discovered_buyers；登记（新建账号 / 填到已有账号）后任务立刻分给它；
- 人工改派优先，解除分配后回到来源指定的账号；重新上传改了买家号 → 时间线上有「来源指定的买家号变了」；
- 导出的「买家号」「买家号ID」「分配账号」三列，导出再导入不会把自动分配变成来源指定，也不会丢掉指定里的买家号 ID；
- 飞书读取结果、读取记录与连接的 last_stats 里「找不到买家号列」的问题项，新记录暂不接收；
- 跨工作区：指定只在自己的工作区里解析，待登记的买家号、忽略名单、导出互不可见。

全部数据虚构（环境-DEMO-A、A1DEMO…、PO-DEMO-…）。
"""

from __future__ import annotations

import io
from datetime import timedelta

import openpyxl
import pytest
from psycopg.types.json import Jsonb

from services import clock, exports
from services import feishu_connections as fc
from tests import fake_feishu as ff
from tests.api_helpers import (IMPORT_HEADERS, api, bearer, create_account, err, find_order, good_facts,  # noqa: F401
                               import_orders, import_row, ok, other, owner, plugin_client, read_xlsx,
                               register_device, run_body, tx, upload, xlsx_bytes)

HEADERS = [*IMPORT_HEADERS, "买家号", "买家号ID"]
ID_A, ID_B, ID_NEW = "A1DEMO000001", "A1DEMO000002", "A1DEMO999999"
DESIGNATED_KEYS = {"customer_id", "name", "account"}
DISCOVERED_KEYS = {"customer_id", "ignored", "browsers", "last_reported_at", "name_hint", "source_orders"}
STATS_KEYS = {"considered", "assigned", "coverage", "fill", "designated", "unassigned"}
NOT_REGISTERED = ("来源指定的买家号 ID A1DEMO999999（买家号「环境-DEMO-新」）还没有登记为账号；"
                  "在「账号与浏览器」登记后会自动分配")
BAD_SHAPE = "来源指定的买家号 ID「12345」格式不对（应以 A 开头，至少 9 位大写字母或数字）；在来源里改正或清空后会重新分配"
ERROR_VALUE = "来源的买家号是表格错误值 #N/A，读不出指定的买家号；在来源里改正后会重新分配"
ADVICE = "未采用；要改请在主来源里改，或在控制台改派（这条来源之后再改买家号，会按改后的应用）"


def _row(no: str, name: str | None = None, cid: str | None = None, **kw):
    """一行虚构订单，带「买家号」「买家号ID」两列。"""
    return import_row(no, **{"买家号": name, "买家号ID": cid, **kw})


def _import(client, rows):
    return import_orders(client, data=xlsx_bytes(rows, headers=HEADERS))


def _two_accounts(client):
    a = create_account(client, name="环境-DEMO-A", amazon_customer_id=ID_A, priority=1)
    b = create_account(client, name="环境-DEMO-B", amazon_customer_id=ID_B, priority=5)
    return a, b


def _rows(client) -> dict[str, dict]:
    return {r["upstream_order_no"]: r
            for r in ok(client.get("/api/v1/orders", params={"view": "all", "page_size": 200}))["rows"]}


def _discovered(client, **params) -> list[dict]:
    return ok(client.get("/api/v1/accounts/discovered", params=params))["items"]


SEVEN = [
    _row("PO-DEMO-D0001", cid=" a1demo000002 "),                    # 第 2 行：按买家号 ID（去空白、转大写）
    _row("PO-DEMO-D0002", name="环境－ＤＥＭＯ－b"),                  # 第 3 行：按名称（全半角、大小写不同）
    _row("PO-DEMO-D0003", name="环境-DEMO-新", cid=ID_NEW),          # 第 4 行：买家号 ID 还没有登记
    _row("PO-DEMO-D0004"),                                          # 第 5 行：两列都空 → 自动分配
    _row("PO-DEMO-D0005", name="—"),                                # 第 6 行：占位写法 → 按没写
    _row("PO-DEMO-D0006", cid="12345"),                             # 第 7 行：买家号 ID 格式不对
    _row("PO-DEMO-D0007", name="#N/A"),                             # 第 8 行：表格错误值 → 解析不了的指定
]


# ── 导入：预览的问题、提交后的分配摘要、任务行与详情 ──────────────────────────


def test_import_preview_issues_then_commit_assigns_by_designation(owner):
    """验收第 1、2、4 条（HTTP）：填了买家号 ID / 买家号的行分给对应账号，分配方式「来源指定」；两列都空自动分配；
    没登记、格式不对、表格错误值的留在待分配并写明原因，绝不分给别的账号；占位写法按没写。"""
    a, b = _two_accounts(owner)
    up = upload(owner, xlsx_bytes(SEVEN, headers=HEADERS))
    assert up["suggested_mapping"]["买家号"] == "external_buyer_account"
    assert up["suggested_mapping"]["买家号ID"] == "external_buyer_customer_id"
    pv = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview",
                       json={"sheet": up["sheet"], "mapping": up["suggested_mapping"]}))
    assert pv["stats"]["total"] == 7 and pv["stats"]["new"] == 7 and pv["stats"]["invalid"] == 0
    issues = {r["row_no"]: r["issues"] for r in pv["rows"]["rows"]}
    assert issues == {2: [], 3: [], 4: [], 5: [],
                      6: ["买家号「—」不是买家号，按没有指定处理"],
                      7: ["买家号 ID「12345」格式不对，这笔任务会停在待分配"],
                      8: ["买家号是表格错误值 #N/A，读不出指定的买家号"]}
    problems = ok(owner.get(f"/api/v1/imports/{up['batch_id']}/rows", params={"verdict": "problems"}))
    assert [r["row_no"] for r in problems["rows"]] == [6, 7, 8]
    assert ok(owner.get("/api/v1/orders"))["total"] == 0 and _discovered(owner) == []        # 预览什么都不建

    cm = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/commit"))
    alloc = cm["allocation"]
    assert alloc["trigger"] == "import" and set(alloc["stats"]) == STATS_KEYS
    assert alloc["stats"] == {"considered": 7, "assigned": 4, "coverage": 1, "fill": 1, "designated": 2,
                              "unassigned": 3}
    assert alloc["stats"]["assigned"] == sum(alloc["stats"][k] for k in ("coverage", "fill", "designated"))
    by_no = {u["upstream_order_no"]: u for u in alloc["unassigned"]}
    assert {no: (u["reason"], u["designated"]) for no, u in by_no.items()} == {
        "PO-DEMO-D0003": (NOT_REGISTERED, True), "PO-DEMO-D0006": (BAD_SHAPE, True),
        "PO-DEMO-D0007": (ERROR_VALUE, True)}
    assert all(set(u) == {"order_id", "upstream_order_no", "reason", "designated"} for u in alloc["unassigned"])
    assert ok(owner.get("/api/v1/allocation/last")) == alloc

    rows = _rows(owner)
    acc_b = {"id": b["id"], "name": "环境-DEMO-B"}
    one = rows["PO-DEMO-D0001"]
    assert (one["status"], one["assignment_kind"], one["assigned_account"]) == ("assigned", "source", acc_b)
    assert one["assignment_note"] == f"按来源的买家号 ID {ID_B}"
    assert one["designated_buyer"] == {"customer_id": ID_B, "name": None, "account": acc_b}
    two = rows["PO-DEMO-D0002"]
    assert (two["status"], two["assignment_kind"], two["assigned_account"]) == ("assigned", "source", acc_b)
    assert two["assignment_note"] == "按来源的买家号「环境－ＤＥＭＯ－b」（对应账号 环境-DEMO-B）"
    assert two["designated_buyer"] == {"customer_id": None, "name": "环境－ＤＥＭＯ－b", "account": acc_b}
    three = rows["PO-DEMO-D0003"]
    assert (three["status"], three["assigned_account"], three["assignment_kind"]) == ("queued", None, None)
    assert three["status_reason"] == NOT_REGISTERED
    assert three["designated_buyer"] == {"customer_id": ID_NEW, "name": "环境-DEMO-新", "account": None}
    for no in ("PO-DEMO-D0004", "PO-DEMO-D0005"):                           # 没有指定：自动分配，designated_buyer 为 null
        assert (rows[no]["status"], rows[no]["assignment_kind"], rows[no]["designated_buyer"]) == \
            ("assigned", "auto", None), no
    assert rows["PO-DEMO-D0004"]["assigned_account"]["id"] == a["id"]        # A 当天首单（保底）
    assert (rows["PO-DEMO-D0006"]["status"], rows["PO-DEMO-D0006"]["status_reason"]) == ("queued", BAD_SHAPE)
    assert rows["PO-DEMO-D0006"]["designated_buyer"] == {"customer_id": "12345", "name": None, "account": None}
    assert (rows["PO-DEMO-D0007"]["status"], rows["PO-DEMO-D0007"]["status_reason"]) == ("queued", ERROR_VALUE)
    assert rows["PO-DEMO-D0007"]["designated_buyer"] == {"customer_id": None, "name": "#N/A", "account": None}
    assert all("designated_buyer" in r for r in rows.values())
    assert all(set(r["designated_buyer"]) == DESIGNATED_KEYS for r in rows.values() if r["designated_buyer"])
    # 待分配视图里有这三笔；按账号筛选只看到分给它的
    unassigned = ok(owner.get("/api/v1/orders", params={"view": "unassigned"}))
    assert {r["upstream_order_no"] for r in unassigned["rows"]} == {"PO-DEMO-D0003", "PO-DEMO-D0006", "PO-DEMO-D0007"}
    on_b = ok(owner.get("/api/v1/orders", params={"account_id": b["id"]}))
    assert {r["upstream_order_no"] for r in on_b["rows"]} == {"PO-DEMO-D0001", "PO-DEMO-D0002", "PO-DEMO-D0005"}

    # 详情：同样的 designated_buyer / assignment_kind；分配事件的 kind 是 source
    d = ok(owner.get(f"/api/v1/orders/{one['id']}"))
    assert d["designated_buyer"] == one["designated_buyer"] and d["assignment_kind"] == "source"
    assigned = [e for e in d["events"] if e["kind"] == "order.assigned"]
    assert len(assigned) == 1 and assigned[0]["detail"]["kind"] == "source"
    assert assigned[0]["detail"]["account_id"] == b["id"] and assigned[0]["detail"]["note"] == one["assignment_note"]
    assert set(d["allowed_actions"]) == {"assign", "unassign", "override", "cancel"}
    d3 = ok(owner.get(f"/api/v1/orders/{three['id']}"))
    assert d3["designated_buyer"] == three["designated_buyer"] and d3["assigned_account"] is None
    assert ok(owner.get(f"/api/v1/orders/{rows['PO-DEMO-D0004']['id']}"))["designated_buyer"] is None


def test_allocation_run_twice_gives_the_same_summary_and_old_runs_read_back(owner):
    """验收第 9 条（HTTP）：重复点「重新分配」得到同样的摘要，不产生新的任务事件；有「来源指定」之前写下的分配记录
    没有 designated 这个键，读出来补 0，不报错。"""
    assert ok(owner.get("/api/v1/allocation/last")) is None
    _two_accounts(owner)
    _import(owner, SEVEN)
    first = ok(owner.post("/api/v1/allocation/run"))
    assert first["trigger"] == "manual" and set(first["stats"]) == STATS_KEYS and first["stats"]["designated"] == 2
    events_before = ok(owner.get("/api/v1/events", params={"entity": "order", "page_size": 1}))["total"]
    updated_before = {no: r["updated_at"] for no, r in _rows(owner).items()}
    clock.freeze(clock.now().replace(minute=5))
    second = ok(owner.post("/api/v1/allocation/run"))
    assert second["id"] != first["id"]
    assert (second["stats"], second["unassigned"], second["uncovered"]) == \
        (first["stats"], first["unassigned"], first["uncovered"])
    assert ok(owner.get("/api/v1/events", params={"entity": "order", "page_size": 1}))["total"] == events_before
    assert {no: r["updated_at"] for no, r in _rows(owner).items()} == updated_before
    assert ok(owner.get("/api/v1/overview"))["last_allocation"] == second

    ws = ok(owner.get("/api/v1/auth/me"))["workspace"]["id"]
    with tx() as conn:                                                     # 旧版本留下的一条分配记录
        conn.execute(
            "INSERT INTO procure.allocation_runs (workspace_id, bj_date, at, trigger, stats, uncovered, unassigned) "
            "VALUES (%s, %s, %s, 'manual', %s, %s, %s)",
            (ws, clock.bj_today(), clock.now().replace(minute=30),
             Jsonb({"considered": 1, "assigned": 0, "coverage": 0, "fill": 0, "unassigned": 1}), Jsonb([]),
             Jsonb([{"order_id": 1, "upstream_order_no": "PO-DEMO-OLD", "reason": "没有金额匹配的启用账号"}])))
    old = ok(owner.get("/api/v1/allocation/last"))
    assert old["stats"] == {"considered": 1, "assigned": 0, "coverage": 0, "fill": 0, "designated": 0,
                            "unassigned": 1}
    assert old["unassigned"] == [{"order_id": 1, "upstream_order_no": "PO-DEMO-OLD",
                                  "reason": "没有金额匹配的启用账号"}]              # 旧项没有 designated 键
    assert ok(owner.get("/api/v1/overview"))["last_allocation"]["stats"]["designated"] == 0


# ── 待登记的买家号 ────────────────────────────────────────────────────────────


def test_discovered_merges_source_designated_ids_and_registering_assigns_at_once(owner, api):
    """验收第 8 条（HTTP）：来源指定但未登记的买家号 ID 出现在「待登记的买家号」里（没有浏览器登录也在）；登记
    （新建账号或填到已有账号）后任务立刻分给它。忽略名单里的全部 ID 都能在 include_ignored=1 里看到并恢复。"""
    create_account(owner, name="环境-DEMO-A", amazon_customer_id=ID_A)
    assert _discovered(owner) == [] and ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 0
    _import(owner, [
        _row("PO-DEMO-E0001", name="环境-DEMO-新", cid=ID_NEW),
        _row("PO-DEMO-E0002", cid=ID_NEW),
        _row("PO-DEMO-E0003", cid="A1DEMO888888"),
        _row("PO-DEMO-E0004", cid="12345"),                              # 形状不对：不进待登记
        _row("PO-DEMO-E0005", name="没有这个账号-DEMO"),                  # 只有名称：没有 ID 可登记，不进待登记
        _row("PO-DEMO-E0006", cid=ID_A),                                 # 已登记的 ID：不进待登记
        _row("PO-DEMO-E0007", cid="A1DEMO777777", **{"人工审核": None}),   # 待确认的任务也算未采购
    ])
    items = _discovered(owner)
    assert [set(i) for i in items] == [DISCOVERED_KEYS] * 3
    by_id = {i["customer_id"]: i for i in items}
    assert set(by_id) == {ID_NEW, "A1DEMO888888", "A1DEMO777777"}
    assert by_id[ID_NEW] == {"customer_id": ID_NEW, "ignored": False, "browsers": [], "last_reported_at": None,
                             "name_hint": "环境-DEMO-新", "source_orders": 2}
    assert by_id["A1DEMO888888"] == {"customer_id": "A1DEMO888888", "ignored": False, "browsers": [],
                                     "last_reported_at": None, "name_hint": None, "source_orders": 1}
    assert by_id["A1DEMO777777"]["source_orders"] == 1
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 3

    # 浏览器也登录着其中一个：同一项里并在一起；另一个只有浏览器上报的，source_orders = 0
    plugin = plugin_client(api)
    d1 = register_device(plugin, customer_id="A1DEMO888888")
    d2 = register_device(plugin, customer_id="A1DEMO666666")
    items = _discovered(owner)
    by_id = {i["customer_id"]: i for i in items}
    assert [i["customer_id"] for i in items] == ["A1DEMO666666", "A1DEMO888888", "A1DEMO777777", ID_NEW]
    # ↑ 有浏览器上报的排在前面（按最近上报时间倒序），只来自来源的排在后面
    assert [b["id"] for b in by_id["A1DEMO888888"]["browsers"]] == [d1["device_id"]]
    assert by_id["A1DEMO888888"]["source_orders"] == 1 and by_id["A1DEMO888888"]["last_reported_at"]
    assert by_id["A1DEMO666666"]["source_orders"] == 0
    assert [b["id"] for b in by_id["A1DEMO666666"]["browsers"]] == [d2["device_id"]]
    assert by_id[ID_NEW]["browsers"] == [] and by_id[ID_NEW]["last_reported_at"] is None
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 4

    # 忽略：只来自来源的 ID 也能忽略；include_ignored=1 列出忽略名单里的全部 ID（没有浏览器登录、没有任务指定的也在）
    for cid in (ID_NEW, "A1DEMO555555"):
        assert ok(owner.post("/api/v1/accounts/discovered/ignore", json={"customer_id": cid})) == {"ok": True}
    assert {i["customer_id"] for i in _discovered(owner)} == {"A1DEMO888888", "A1DEMO777777", "A1DEMO666666"}
    with_ignored = {i["customer_id"]: i for i in _discovered(owner, include_ignored=1)}
    assert {cid: i["ignored"] for cid, i in with_ignored.items()} == {
        ID_NEW: True, "A1DEMO555555": True, "A1DEMO888888": False, "A1DEMO777777": False, "A1DEMO666666": False}
    assert with_ignored["A1DEMO555555"] == {"customer_id": "A1DEMO555555", "ignored": True, "browsers": [],
                                            "last_reported_at": None, "name_hint": None, "source_orders": 0}
    assert with_ignored[ID_NEW]["source_orders"] == 2 and with_ignored[ID_NEW]["name_hint"] == "环境-DEMO-新"
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 3
    # 忽略之后重新分配：原因里注明「已被忽略」
    ok(owner.post("/api/v1/allocation/run"))
    assert find_order(owner, "PO-DEMO-E0001")["status_reason"] == NOT_REGISTERED + \
        "（这个买家号 ID 已被忽略，恢复后才能登记）"
    assert ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": ID_NEW})) == {"ok": True}
    assert ok(owner.post("/api/v1/accounts/discovered/unignore", json={"customer_id": "A1DEMO555555"})) == {"ok": True}
    assert "A1DEMO555555" not in {i["customer_id"] for i in _discovered(owner, include_ignored=1)}
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 4

    # 「登记」：新建账号带上这个 ID → 指定它的两笔任务立刻分过去（不用再点重新分配）
    new = create_account(owner, name="环境-DEMO-新", amazon_customer_id=ID_NEW)
    for no in ("PO-DEMO-E0001", "PO-DEMO-E0002"):
        r = find_order(owner, no)
        assert (r["status"], r["assignment_kind"], r["assigned_account"]) == \
            ("assigned", "source", {"id": new["id"], "name": "环境-DEMO-新"}), no
        assert r["designated_buyer"]["account"] == {"id": new["id"], "name": "环境-DEMO-新"}
    assert ID_NEW not in {i["customer_id"] for i in _discovered(owner, include_ignored=1)}
    # 关着采购开关新建的账号也一样会触发分配（来源指定的任务留在待分配并写明原因，不落到别的账号）
    off = create_account(owner, name="环境-DEMO-关", amazon_customer_id="A1DEMO777777", purchase_enabled=False)
    ok(owner.post(f"/api/v1/orders/{find_order(owner, 'PO-DEMO-E0007')['id']}/review", json={"decision": "approve"}))
    ok(owner.post("/api/v1/allocation/run"))
    seven = find_order(owner, "PO-DEMO-E0007")
    assert seven["status"] == "queued" and seven["designated_buyer"]["account"]["id"] == off["id"]
    assert seven["status_reason"] == "来源指定的账号 环境-DEMO-关 已关闭采购开关，不接收新分配；打开开关后会自动分配"

    # 「填到已有账号」：给没填 ID 的账号填上 → 任务立刻分给它
    blank = create_account(owner, name="环境-DEMO-八", amazon_customer_id=None)
    assert find_order(owner, "PO-DEMO-E0003")["status"] == "queued"
    filled = ok(owner.patch(f"/api/v1/accounts/{blank['id']}", json={"amazon_customer_id": "A1DEMO888888"}))
    assert [b["id"] for b in filled["browsers"]] == [d1["device_id"]]
    three = find_order(owner, "PO-DEMO-E0003")
    assert (three["status"], three["assignment_kind"], three["assigned_account"]["id"]) == \
        ("assigned", "source", blank["id"])
    assert {i["customer_id"] for i in _discovered(owner, include_ignored=1)} == {"A1DEMO666666"}
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 1
    # 账号改名：按名称指定的任务跟着重新解析
    ok(owner.patch(f"/api/v1/accounts/{blank['id']}", json={"name": "没有这个账号-DEMO"}))
    five = find_order(owner, "PO-DEMO-E0005")
    assert (five["status"], five["assignment_kind"], five["assigned_account"]["id"]) == \
        ("assigned", "source", blank["id"])


def test_discovered_drops_ids_whose_tasks_are_all_done(owner, api):
    """只算未采购的任务：指定它的任务取消之后，这个 ID 不再是待登记的买家号。"""
    create_account(owner, name="环境-DEMO-A", amazon_customer_id=ID_A)
    _import(owner, [_row("PO-DEMO-G0001", cid=ID_NEW)])
    assert [(i["customer_id"], i["source_orders"]) for i in _discovered(owner)] == [(ID_NEW, 1)]
    ok(owner.post(f"/api/v1/orders/{find_order(owner, 'PO-DEMO-G0001')['id']}/cancel", json={"note": "DEMO"}))
    assert _discovered(owner) == [] and ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 0
    ok(owner.post(f"/api/v1/orders/{find_order(owner, 'PO-DEMO-G0001')['id']}/uncancel", json={}))
    assert [(i["customer_id"], i["source_orders"]) for i in _discovered(owner)] == [(ID_NEW, 1)]


def _where(client, *nos: str) -> dict[str, tuple]:
    rows = _rows(client)
    return {no: (rows[no]["status"], (rows[no]["assigned_account"] or {}).get("name"), rows[no]["assignment_kind"],
                 rows[no]["status_reason"]) for no in nos}


def test_account_changes_reallocate_designated_tasks_without_falling_back_to_another_account(owner):
    """验收第 2 条（HTTP）：指定的账号暂停执行、关闭采购开关、改了买家号 ID、金额区间容不下、当天额度已满、归档——
    每一次改账号都立刻重新分配；接不了的任务留在待分配并写明原因，另一个空着的账号 B 始终没有拿到它们。"""
    a, b = _two_accounts(owner)
    aid = a["id"]
    on_a = ("assigned", "环境-DEMO-A", "source", None)
    _import(owner, [_row("PO-DEMO-P0001", cid=ID_A), _row("PO-DEMO-P0002", name="环境-DEMO-A"),
                    _row("PO-DEMO-P0003")])
    assert _where(owner, "PO-DEMO-P0001", "PO-DEMO-P0002") == {"PO-DEMO-P0001": on_a, "PO-DEMO-P0002": on_a}
    auto_b = ("assigned", "环境-DEMO-B", "auto", None)

    # 暂停执行：已分配在它上面的保留；新来的指定任务留在待分配，恢复后自动分过去
    ok(owner.post(f"/api/v1/accounts/{aid}/pause", json={"reason": "DEMO 暂停"}))
    _import(owner, [_row("PO-DEMO-P0004", cid=ID_A)])
    assert _where(owner, "PO-DEMO-P0001", "PO-DEMO-P0004") == {
        "PO-DEMO-P0001": on_a,
        "PO-DEMO-P0004": ("queued", None, None, "来源指定的账号 环境-DEMO-A 已暂停执行；恢复执行后会自动分配")}
    ok(owner.post(f"/api/v1/accounts/{aid}/resume"))
    assert _where(owner, "PO-DEMO-P0004")["PO-DEMO-P0004"] == on_a

    # 关闭采购开关：已分配在它上面的原样保留；新来的留在待分配
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"purchase_enabled": False}))
    _import(owner, [_row("PO-DEMO-P0005", name="环境-DEMO-A")])
    assert _where(owner, "PO-DEMO-P0001", "PO-DEMO-P0002", "PO-DEMO-P0005") == {
        "PO-DEMO-P0001": on_a, "PO-DEMO-P0002": on_a,
        "PO-DEMO-P0005": ("queued", None, None,
                          "来源指定的账号 环境-DEMO-A 已关闭采购开关，不接收新分配；打开开关后会自动分配")}
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"purchase_enabled": True}))
    assert _where(owner, "PO-DEMO-P0005")["PO-DEMO-P0005"] == on_a

    # 改了账号的买家号 ID：按 ID 指定的回到待分配（这个 ID 成了待登记的买家号），按名称指定的不受影响
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"amazon_customer_id": "A1DEMO000009"}))
    not_registered = f"来源指定的买家号 ID {ID_A} 还没有登记为账号；在「账号与浏览器」登记后会自动分配"
    assert _where(owner, "PO-DEMO-P0001", "PO-DEMO-P0002", "PO-DEMO-P0004") == {
        "PO-DEMO-P0001": ("queued", None, None, not_registered), "PO-DEMO-P0002": on_a,
        "PO-DEMO-P0004": ("queued", None, None, not_registered)}
    assert [(i["customer_id"], i["source_orders"], i["browsers"]) for i in _discovered(owner)] == [(ID_A, 2, [])]
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"amazon_customer_id": ID_A}))
    assert _discovered(owner) == []

    # 金额区间容不下
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"max_amount": "10.00"}))
    out_of_range = ("queued", None, None, "金额 $25.00 不在来源指定的账号 环境-DEMO-A 的区间 $0.00–$10.00 内；"
                                          "调整账号的金额区间，或改派到其他账号")
    assert set(_where(owner, "PO-DEMO-P0001", "PO-DEMO-P0002", "PO-DEMO-P0004", "PO-DEMO-P0005").values()) == \
        {out_of_range}

    # 当天额度只有 1：排在最前面的一笔分过去，其余留在待分配
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"max_amount": "100.00", "daily_limit": 1}))
    full = ("queued", None, None, "来源指定的账号 环境-DEMO-A 在 2026-10-31 的额度已满（上限 1）；有空位后的分配会再安排")
    assert _where(owner, "PO-DEMO-P0001", "PO-DEMO-P0002", "PO-DEMO-P0004", "PO-DEMO-P0005") == {
        "PO-DEMO-P0001": on_a, "PO-DEMO-P0002": full, "PO-DEMO-P0004": full, "PO-DEMO-P0005": full}
    last = ok(owner.get("/api/v1/allocation/last"))
    assert last["trigger"] == "account_change" and last["stats"]["designated"] == 1
    assert [u["designated"] for u in last["unassigned"]] == [True, True, True]
    ok(owner.patch(f"/api/v1/accounts/{aid}", json={"daily_limit": 10}))

    # 归档：全部回到待分配并写明原因；这个买家号 ID 又成了待登记的
    ok(owner.post(f"/api/v1/accounts/{aid}/archive"))
    by_id = (f"来源指定的买家号 ID {ID_A} 对应的账号 环境-DEMO-A 已归档；重新登记这个买家号，或改派到其他账号")
    by_name = "来源指定的买家号「环境-DEMO-A」对应的账号已归档；在来源里改成其他买家号，或改派到其他账号"
    assert _where(owner, "PO-DEMO-P0001", "PO-DEMO-P0002", "PO-DEMO-P0004", "PO-DEMO-P0005") == {
        "PO-DEMO-P0001": ("queued", None, None, by_id), "PO-DEMO-P0002": ("queued", None, None, by_name),
        "PO-DEMO-P0004": ("queued", None, None, by_id), "PO-DEMO-P0005": ("queued", None, None, by_name)}
    assert [(i["customer_id"], i["source_orders"]) for i in _discovered(owner)] == [(ID_A, 2)]
    # 自始至终，B 上只有那一笔自动分配的任务
    assert _where(owner, "PO-DEMO-P0003")["PO-DEMO-P0003"] == auto_b
    assert {r["upstream_order_no"] for r in ok(owner.get("/api/v1/orders", params={"account_id": b["id"]}))["rows"]} \
        == {"PO-DEMO-P0003"}
    assigned_to_b = [e for e in ok(owner.get("/api/v1/events", params={"entity": "order", "page_size": 200}))["rows"]
                     if e["kind"] in ("order.assigned", "order.reassigned") and e["detail"]["account_id"] == b["id"]]
    assert len(assigned_to_b) == 1


# ── 人工改派、解除分配、来源改了指定 ──────────────────────────────────────────


def test_manual_reassign_wins_and_unassign_returns_to_the_designated_account(owner):
    """验收第 5 条（HTTP）：人工改派后留在改派账号期间不跟随来源；解除分配后回到来源指定的账号。"""
    a, b = _two_accounts(owner)
    _import(owner, [_row("PO-DEMO-M0001", cid=ID_B)])
    oid = find_order(owner, "PO-DEMO-M0001")["id"]
    d = ok(owner.post(f"/api/v1/orders/{oid}/assign", json={"account_id": a["id"]}))
    assert (d["assignment_kind"], d["assigned_account"]["id"]) == ("manual", a["id"])
    assert d["designated_buyer"] == {"customer_id": ID_B, "name": None,
                                     "account": {"id": b["id"], "name": "环境-DEMO-B"}}     # 指定还在，只是暂不生效
    run = ok(owner.post("/api/v1/allocation/run"))
    assert run["stats"]["designated"] == 0 and run["stats"]["considered"] == 0
    row = find_order(owner, "PO-DEMO-M0001")
    assert (row["assignment_kind"], row["assigned_account"]["id"]) == ("manual", a["id"])
    # 留在改派账号期间来源又改了指定：照样记下来，预览里提示「暂不生效」，任务不动
    again = _import(owner, [_row("PO-DEMO-M0001", name="环境-DEMO-B")])
    pv = again["preview"]["rows"]["rows"][0]
    assert pv["verdict"] == "update" and pv["reason"] == f"来源指定的买家号由「{ID_B}」改为「环境-DEMO-B」"
    assert pv["issues"] == ["任务已人工改派到 环境-DEMO-A，来源的指定暂不生效；解除分配后按来源指定分配"]
    row = find_order(owner, "PO-DEMO-M0001")
    assert (row["assignment_kind"], row["assigned_account"]["id"]) == ("manual", a["id"])
    assert row["designated_buyer"] == {"customer_id": None, "name": "环境-DEMO-B",
                                       "account": {"id": b["id"], "name": "环境-DEMO-B"}}
    d = ok(owner.post(f"/api/v1/orders/{oid}/unassign"))
    assert d["status"] == "queued" and d["assigned_account"] is None and d["designated_buyer"]["name"] == "环境-DEMO-B"
    run = ok(owner.post("/api/v1/allocation/run"))
    assert run["stats"]["designated"] == 1
    row = find_order(owner, "PO-DEMO-M0001")
    assert (row["status"], row["assignment_kind"], row["assigned_account"]["id"]) == ("assigned", "source", b["id"])


def test_reuploading_a_changed_buyer_moves_the_task_and_shows_on_the_timeline(owner):
    """验收第 4 条（HTTP，文件为主来源）：重新上传并填上新的买家号 → 任务改到新账号，时间线上有
    order.designation_changed（扁平的 changes）；空着重新上传不清除指定。"""
    a, b = _two_accounts(owner)
    _import(owner, [_row("PO-DEMO-T0001", cid=ID_A), _row("PO-DEMO-T0002", name="环境-DEMO-A")])
    assert find_order(owner, "PO-DEMO-T0001")["assigned_account"]["id"] == a["id"]
    again = _import(owner, [_row("PO-DEMO-T0001", cid=ID_B), _row("PO-DEMO-T0002")])
    verdicts = {r["row_no"]: (r["verdict"], r["reason"]) for r in again["preview"]["rows"]["rows"]}
    assert verdicts[2] == ("update", f"来源指定的买家号由「{ID_A}」改为「{ID_B}」")
    assert verdicts[3][0] == "unchanged"                                    # 空着重新上传：不清除指定
    assert again["commit"]["allocation"]["stats"]["designated"] == 2
    one = find_order(owner, "PO-DEMO-T0001")
    assert (one["assignment_kind"], one["assigned_account"]["id"]) == ("source", b["id"])
    assert one["designated_buyer"] == {"customer_id": ID_B, "name": None, "account": {"id": b["id"], "name": b["name"]}}
    two = find_order(owner, "PO-DEMO-T0002")
    assert (two["assignment_kind"], two["assigned_account"]["id"]) == ("source", a["id"])
    assert two["designated_buyer"]["name"] == "环境-DEMO-A"
    d = ok(owner.get(f"/api/v1/orders/{one['id']}"))
    changed = [e for e in d["events"] if e["kind"] == "order.designation_changed"]
    assert len(changed) == 1
    assert changed[0]["detail"]["changes"] == {"designated_customer_id": [ID_A, ID_B]}
    assert changed[0]["detail"]["source"]["kind"] == "file"
    # 接收时原分配按「已不符合来源指定」解除，同一个事务里的分配再按新指定落到 B（事件新的在前）
    assigned = [e["detail"] for e in d["events"] if e["kind"] == "order.assigned"]
    assert [(x["account_id"], x["kind"]) for x in assigned] == [(b["id"], "source"), (a["id"], "source")]
    assert f"来源指定的买家号已不是账号 {a['name']}，已解除分配" in [
        e["detail"].get("reason") for e in d["events"] if e["kind"] == "order.status_changed"]
    listed = ok(owner.get("/api/v1/events", params={"order_id": one["id"], "page_size": 100}))
    assert "order.designation_changed" in {e["kind"] for e in listed["rows"]}


def test_plugin_cannot_claim_a_task_whose_designation_moved_away(owner, api):
    """验收第 6 条（HTTP）：采购中来源把买家号改到了别的账号——这一次不受影响（预览里写明），但中止回到已分配之后，
    原账号的插件领不到它：领取时复核把它解除分配，领取答复里说明原因；随后的分配按新指定落到新账号。"""
    a, b = _two_accounts(owner)
    _import(owner, [_row("PO-DEMO-C0001", cid=ID_A)])
    plugin = plugin_client(api)
    dev = register_device(plugin, a)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(a), headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    assert att["upstream_order_no"] == "PO-DEMO-C0001" and att["account"] == {"id": a["id"], "name": a["name"]}

    again = _import(owner, [_row("PO-DEMO-C0001", cid=ID_B)])                 # 采购中改了指定
    pv = again["preview"]["rows"]["rows"][0]
    assert pv["verdict"] == "update"
    assert pv["reason"] == f"来源指定的买家号由「{ID_A}」改为「{ID_B}」；任务已开始采购，不影响这一次"
    row = find_order(owner, "PO-DEMO-C0001")
    assert row["status"] == "executing" and row["assigned_account"]["id"] == a["id"]
    assert row["designated_buyer"] == {"customer_id": ID_B, "name": None, "account": {"id": b["id"], "name": b["name"]}}
    pr = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/progress", json={"stage": "checkout"}, headers=h))
    assert pr["continue"] is True                                             # 指定不是执行相关字段：不叫停这一次

    res = ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                         json={"outcome": "failed", "error_code": "ABORTED_BY_USER", "error_detail": "DEMO 中止"},
                         headers=h))
    assert res == {"attempt_status": "aborted", "order_status": "assigned"}
    assert find_order(owner, "PO-DEMO-C0001")["assigned_account"]["id"] == a["id"]     # 中止不触发分配：还在 A 上
    nxt = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))
    assert nxt["attempt"] is None
    assert nxt["reason"] == "今天没有可执行的任务（1 笔来源指定的买家号已不是本账号、已解除分配）"
    row = find_order(owner, "PO-DEMO-C0001")
    assert (row["status"], row["assigned_account"]) == ("queued", None)
    assert row["status_reason"] == f"来源指定的买家号已不是账号 {a['name']}，领取时解除分配"
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={}, headers=h))
    assert ok(owner.post("/api/v1/allocation/run"))["stats"]["designated"] == 1
    row = find_order(owner, "PO-DEMO-C0001")
    assert (row["status"], row["assignment_kind"], row["assigned_account"]["id"]) == ("assigned", "source", b["id"])


def test_repurchase_goes_back_to_the_designated_account(owner, api):
    """验收第 3 条（HTTP，同意重采的一半）：买完之后亚马逊取消、同意重采——任务沿用原来的指定，下一次分配仍分给
    指定的账号，而不是自动分配会先选的那个。"""
    a = create_account(owner, name="环境-DEMO-A", amazon_customer_id=ID_A, priority=9)       # 自动分配会先选它
    b = create_account(owner, name="环境-DEMO-B", amazon_customer_id=ID_B, priority=1)
    _import(owner, [_row("PO-DEMO-R0001", cid=ID_B)])
    oid = find_order(owner, "PO-DEMO-R0001")["id"]
    plugin = plugin_client(api)
    dev = register_device(plugin, b)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(b), headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att, customer_id=ID_B),
                   headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                   json={"outcome": "succeeded", "amazon_order_no": f"SIM-20261031-{att['attempt_id']}",
                         "amounts": {"total": "25.00"}}, headers=h))
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={}, headers=h))
    d = ok(owner.get(f"/api/v1/orders/{oid}"))
    assert d["status"] == "purchased" and d["designated_buyer"]["customer_id"] == ID_B     # 买完了，指定还记着

    d = ok(owner.post(f"/api/v1/orders/{oid}/mark-amazon-cancelled", json={"note": "DEMO 亚马逊取消"}))
    assert d["status"] == "repurchase_pending"
    d = ok(owner.post(f"/api/v1/orders/{oid}/repurchase", json={"decision": "approve", "note": "DEMO 重采"}))
    assert d["status"] == "queued" and d["assigned_account"] is None
    assert d["designated_buyer"] == {"customer_id": ID_B, "name": None, "account": {"id": b["id"], "name": b["name"]}}
    alloc = ok(owner.post("/api/v1/allocation/run"))
    assert alloc["stats"]["designated"] == 1 and alloc["stats"]["coverage"] == 0 and alloc["stats"]["fill"] == 0
    row = find_order(owner, "PO-DEMO-R0001")
    assert (row["status"], row["assignment_kind"], row["assigned_account"]["id"]) == ("assigned", "source", b["id"])
    assert row["assigned_account"]["id"] != a["id"]


# ── 导出 ─────────────────────────────────────────────────────────────────────


def _export(client, **params) -> list[dict]:
    resp = client.get("/api/v1/exports/orders.xlsx", params=params)
    assert resp.status_code == 200, resp.text
    table = read_xlsx(resp.content)
    assert table[0] == exports.EXPORT_HEADERS
    return [dict(zip(table[0], r)) for r in table[1:]]


def test_export_buyer_column_is_the_designation_and_assigned_account_is_separate(owner, api):
    """导出（契约 §7）：没有采购结果时「买家号」「买家号ID」只写来源指定的两列，已分配的账号在另一列「分配账号」；
    有采购结果后这两列是采购账号的名称与它登记的买家号 ID。导出文件原样再上传，自动分配的任务不会变成来源指定。"""
    a, b = _two_accounts(owner)
    imp = _import(owner, [_row("PO-DEMO-X0001", name="环境-DEMO-B"), _row("PO-DEMO-X0002"),
                          _row("PO-DEMO-X0003", cid=ID_B), _row("PO-DEMO-X0004", name="环境-DEMO-无")])
    assert exports.EXPORT_HEADERS[:6] == ["平台订单号", "状态", "买家号", "买家号ID", "分配账号", "AMZ订单号"]
    by_no = {r["平台订单号"]: r for r in _export(owner, view="all")}

    def cols(no):
        return by_no[no]["买家号"], by_no[no]["买家号ID"], by_no[no]["分配账号"]

    assert cols("PO-DEMO-X0001") == ("环境-DEMO-B", None, "环境-DEMO-B")
    assert cols("PO-DEMO-X0002") == (None, None, "环境-DEMO-A")
    assert cols("PO-DEMO-X0003") == (None, ID_B, "环境-DEMO-B")
    assert cols("PO-DEMO-X0004") == ("环境-DEMO-无", None, None)
    assert by_no["PO-DEMO-X0004"]["状态"] == "待分配" and "没有同名账号" in by_no["PO-DEMO-X0004"]["异常原因"]
    batch = {r["平台订单号"]: r for r in _export(owner, batch_id=imp["upload"]["batch_id"])}
    assert batch == by_no

    # 模拟买完 X0002（自动分配在 A 上）：「买家号」变成采购账号
    plugin = plugin_client(api)
    dev = register_device(plugin, a)
    h = bearer(dev["token"])
    run = ok(plugin.post("/plugin/v1/runs", json=run_body(a), headers=h))
    att = ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/next", headers=h))["attempt"]
    assert att["upstream_order_no"] == "PO-DEMO-X0002"
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/guard", json=good_facts(att), headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/submitting", headers=h))
    ok(plugin.post(f"/plugin/v1/attempts/{att['attempt_id']}/result",
                   json={"outcome": "succeeded", "amazon_order_no": f"SIM-20261031-{att['attempt_id']}",
                         "amounts": {"total": "25.00"}}, headers=h))
    ok(plugin.post(f"/plugin/v1/runs/{run['run_id']}/finish", json={}, headers=h))
    bought = {r["平台订单号"]: r for r in _export(owner, view="all")}["PO-DEMO-X0002"]
    assert (bought["状态"], bought["买家号"], bought["买家号ID"], bought["分配账号"], bought["结果来源"]) == \
        ("已采购", "环境-DEMO-A", ID_A, "环境-DEMO-A", "模拟")

    # 导出文件原样再上传：「分配账号」不被认成买家号列；自动分配的 / 已买的任务都不会多出指定
    data = owner.get("/api/v1/exports/orders.xlsx", params={"view": "all"}).content
    up = upload(owner, data, filename="采购结果-DEMO.xlsx")
    assert up["suggested_mapping"]["买家号"] == "external_buyer_account" and up["suggested_mapping"]["分配账号"] is None
    assert up["suggested_mapping"]["买家号ID"] == "external_buyer_customer_id"
    ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview", json={"sheet": up["sheet"],
                                                                     "mapping": up["suggested_mapping"]}))
    ok(owner.post(f"/api/v1/imports/{up['batch_id']}/commit"))
    rows = _rows(owner)
    assert rows["PO-DEMO-X0002"]["designated_buyer"] is None and rows["PO-DEMO-X0002"]["status"] == "purchased"
    assert (rows["PO-DEMO-X0001"]["assignment_kind"], rows["PO-DEMO-X0001"]["assigned_account"]["id"]) == \
        ("source", b["id"])
    assert rows["PO-DEMO-X0003"]["designated_buyer"] == {"customer_id": ID_B, "name": None,
                                                         "account": {"id": b["id"], "name": b["name"]}}
    assert rows["PO-DEMO-X0004"]["designated_buyer"]["name"] == "环境-DEMO-无"


def test_export_reimport_keeps_id_and_name_designation(owner):
    """ALLOC-1（HTTP）：指定同时写了买家号 ID 与买家号的任务，导出文件原样再上传后一列不丢——指定、状态、账号、
    分配方式、说明都不变，预览里没有「来源指定的买家号由…改为…」，时间线上没有 order.designation_changed。
    之前导出没有「买家号ID」列：ID 被悄悄丢掉，任务被拉回待分配，或者落到名称指向的另一个账号上。"""
    a, b = _two_accounts(owner)
    day = {"建议采购日期": "2026-10-31"}                               # 来源给了日期：导出再上传时别的字段也都不变
    _import(owner, [
        _row("PO-DEMO-Y0001", cid=ID_B, name="店铺小号-DEMO", **day),  # ID 命中 B，名称谁也对不上
        _row("PO-DEMO-Y0002", cid=ID_B, name="环境-DEMO-A", **day),    # ID 与名称指向不同账号：不分配
        _row("PO-DEMO-Y0003", cid=ID_B, name="环境-DEMO-B", **day),    # ID 与名称都是 B
        _row("PO-DEMO-Y0004", cid=ID_B, **day),                        # 只有 ID
    ])
    keys = ("status", "status_reason", "assigned_account", "assignment_kind", "assignment_note", "designated_buyer")
    before = {no: {k: r[k] for k in keys} for no, r in _rows(owner).items()}
    acc_b = {"id": b["id"], "name": "环境-DEMO-B"}
    assert before["PO-DEMO-Y0001"]["assignment_note"] == \
        f"按来源的买家号 ID {ID_B}（来源写的买家号「店铺小号-DEMO」与账号名称不同）"
    assert (before["PO-DEMO-Y0002"]["status"], before["PO-DEMO-Y0002"]["assigned_account"]) == ("queued", None)
    assert "不是同一个账号" in before["PO-DEMO-Y0002"]["status_reason"]
    assert before["PO-DEMO-Y0003"]["designated_buyer"] == {"customer_id": ID_B, "name": "环境-DEMO-B",
                                                           "account": acc_b}

    by_no = {r["平台订单号"]: r for r in _export(owner, view="all")}
    assert [(by_no[f"PO-DEMO-Y000{i}"]["买家号ID"], by_no[f"PO-DEMO-Y000{i}"]["买家号"]) for i in (1, 2, 3, 4)] == [
        (ID_B, "店铺小号-DEMO"), (ID_B, "环境-DEMO-A"), (ID_B, "环境-DEMO-B"), (ID_B, None)]
    data = owner.get("/api/v1/exports/orders.xlsx", params={"view": "all"}).content
    up = upload(owner, data, filename="采购结果-DEMO.xlsx")
    assert up["suggested_mapping"]["买家号ID"] == "external_buyer_customer_id"
    pv = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview",
                       json={"sheet": up["sheet"], "mapping": up["suggested_mapping"]}))
    assert [(r["verdict"], r["reason"]) for r in pv["rows"]["rows"]] == [("unchanged", None)] * 4
    cm = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/commit"))
    assert cm["stats"]["unchanged"] == 4 and cm["stats"]["update"] == 0
    after = {no: {k: r[k] for k in keys} for no, r in _rows(owner).items()}
    assert after == before
    for no, r in _rows(owner).items():
        kinds = {e["kind"] for e in ok(owner.get(f"/api/v1/orders/{r['id']}"))["events"]}
        assert not {"order.designation_changed", "order.designation_not_adopted"} & kinds, no


def test_template_download_explains_the_two_buyer_columns(owner):
    """下载的标准模板「说明」表：买家号 / 买家号ID 两列写的是「指定谁来买」，不再只是历史采购记录；表头不变。"""
    resp = owner.get("/api/v1/imports/template.xlsx")
    assert resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True, data_only=True)
    try:
        header = [c for c in next(wb["采购订单"].iter_rows(values_only=True))]
        notes = {r[0]: r for r in wb["说明"].iter_rows(values_only=True)}
    finally:
        wb.close()
    assert "买家号" in header and "买家号ID" in header and "分配账号" not in header
    assert notes["买家号"][2] == ("指定由哪个买家号采购，须与控制台的账号名称一致；留空由系统自动分配。"
                               "已有 AMZ 订单号的行表示历史采购使用的买家号。"
                               "文件来源要改指定请重新上传并填上新的买家号；空着重新上传不会清除指定")
    assert notes["买家号ID"][2] == ("指定采购的买家号 ID（亚马逊 customerId，A 开头）；与「买家号」同时填写时以它为准。"
                                 "留空由系统自动分配")
    assert notes["买家号"][1] == notes["买家号ID"][1] != notes["平台订单号"][1]          # 两列都不是必填


# ── 飞书：找不到买家号列 ──────────────────────────────────────────────────────


@pytest.fixture()
def fake(monkeypatch):
    client = ff.FakeFeishuClient.with_order_table()
    monkeypatch.setattr(fc, "make_client", lambda: client)
    return client


def test_feishu_sync_reports_a_missing_buyer_column_and_keeps_the_designation(owner, fake):
    """验收第 4 条（HTTP，飞书）：改了买家号 → 立即读取后任务改到新账号；来源表里找不到买家号列 → 读取结果第一条
    问题项说明这一列的指定保持上次读到的内容、新记录暂不接收（scope = connection，不计入 skipped），任务的指定不变；
    这条连接级问题也带在连接的 last_stats.problems 里（定时读取时控制台的连接卡片靠它提示），新记录不会被建成
    没有指定的任务分给别的账号。"""
    a, b = _two_accounts(owner)
    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0001",
                    ff.order_fields("PO-DEMO-S0001", 买家号id=ff.text(ID_A)))
    cid = ok(owner.post("/api/v1/feishu/connections", json=ff.demo_connection_data(fake)))["id"]
    sync = ok(owner.post(f"/api/v1/feishu/connections/{cid}/sync"))
    assert sync["new"] == 1 and sync["problems"] == []
    row = find_order(owner, "PO-DEMO-S0001")
    assert (row["assignment_kind"], row["assigned_account"]["id"]) == ("source", a["id"])

    clock.freeze(clock.now().replace(minute=10))
    fake.edit_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0001", 买家号id=ff.text(ID_B))
    sync = ok(owner.post(f"/api/v1/feishu/connections/{cid}/sync"))
    assert sync["update"] == 1
    row = find_order(owner, "PO-DEMO-S0001")
    assert (row["assignment_kind"], row["assigned_account"]["id"]) == ("source", b["id"])

    clock.freeze(clock.now().replace(minute=20))
    table = fake.apps[ff.DEMO_APP][ff.DEMO_TABLE]                           # 有人把这一列改了名
    for fdef in table["fields"]:
        if fdef["field_name"] == "买家号id":
            fdef["field_name"] = "买家号ID（改名）"
    for rec in table["records"].values():
        rec.pop("买家号id", None)
    sync = ok(owner.post(f"/api/v1/feishu/connections/{cid}/sync"))
    notice = {"record_id": None, "upstream_order_no": "买家号列", "verdict": "skipped",
              "reason": "来源表里找不到买家号列「买家号id」，这一列的指定保持上次读到的内容，新记录暂不接收",
              "scope": "connection"}
    assert sync["problems"] == [notice]
    assert sync["skipped"] == 0 and sync["update"] == 0
    row = find_order(owner, "PO-DEMO-S0001")
    assert (row["assignment_kind"], row["assigned_account"]["id"]) == ("source", b["id"])
    assert row["designated_buyer"]["customer_id"] == ID_B
    runs = ok(owner.get(f"/api/v1/feishu/connections/{cid}/runs"))
    assert runs["rows"][0]["ok"] is True and runs["rows"][0]["stats"]["problems"][0]["scope"] == "connection"

    # 定时读取（后台调度走的路）：这期间表里多了一条新记录，买家号写在还读得到的「买家号」列里。
    # 读不全它的指定 → 暂不接收（不建任务，更不会自动分给别的账号）；连接卡片读 last_stats.problems 就能提示
    from workflows import feishu_intake as intake_workflow

    fake.add_record(ff.DEMO_APP, ff.DEMO_TABLE, "recDEMO0002",
                    ff.order_fields("PO-DEMO-S0002", 买家号=ff.text("环境-DEMO-B")))
    clock.freeze(clock.now() + timedelta(hours=30))
    scheduled = intake_workflow.run()                                         # 不带连接 id = 定时读取
    assert scheduled["due"] == 1 and scheduled["connections"][str(cid)]["skipped"] == 1
    listed = {c["id"]: c for c in ok(owner.get("/api/v1/feishu/connections"))["rows"]}[cid]
    assert listed["last_error"] is None and listed["last_stats"]["problems"] == [notice]
    assert listed["last_stats"]["skipped"] == 1 and listed["last_stats"]["new"] == 0
    latest = ok(owner.get(f"/api/v1/feishu/connections/{cid}/runs"))["rows"][0]
    assert latest["trigger"] == "schedule" and latest["stats"]["problems"] == [notice, {
        "record_id": "recDEMO0002", "upstream_order_no": "PO-DEMO-S0002", "verdict": "skipped",
        "reason": "来源表里找不到买家号列「买家号id」，读不出这一行有没有指定买家号，暂不接收；"
                  "恢复这一列或改字段对应后会自动接收"}]
    assert "PO-DEMO-S0002" not in _rows(owner)
    assert all(r["assignment_kind"] != "auto" for r in _rows(owner).values())

    # 另一种来源（文件）第一次提到这笔任务、带了不同的买家号：飞书是主来源，文件的不采用，预览里写明
    up = upload(owner, xlsx_bytes([_row("PO-DEMO-S0001", cid=ID_A, 建议采购日期="2026-10-31")], headers=HEADERS))
    pv = ok(owner.post(f"/api/v1/imports/{up['batch_id']}/preview",
                       json={"sheet": up["sheet"], "mapping": up["suggested_mapping"]}))
    assert pv["rows"]["rows"][0]["issues"] == [
        f"文件来源的买家号「{ID_A}」与任务现有的指定「{ID_B}」不同，{ADVICE}"]
    ok(owner.post(f"/api/v1/imports/{up['batch_id']}/commit"))
    row = find_order(owner, "PO-DEMO-S0001")
    assert (row["assignment_kind"], row["assigned_account"]["id"]) == ("source", b["id"])
    assert row["designated_buyer"]["customer_id"] == ID_B and row["source_kinds"] == ["feishu", "file"]
    # 提交之后，批次的问题行里也留着这一句（上传预览、批次行、飞书样本预览看得到；飞书读取结果的 problems 不带它）
    committed = ok(owner.get(f"/api/v1/imports/{up['batch_id']}/rows", params={"verdict": "problems"}))["rows"]
    assert [r["upstream_order_no"] for r in committed] == ["PO-DEMO-S0001"]
    assert f"文件来源的买家号「{ID_A}」与任务现有的指定「{ID_B}」不同，{ADVICE}" in committed[0]["issues"]
    # 没采用的那一句不只在预览里：任务时间线上有一条 order.designation_not_adopted
    events = [e for e in ok(owner.get(f"/api/v1/orders/{row['id']}"))["events"]
              if e["kind"] == "order.designation_not_adopted"]
    assert len(events) == 1 and events[0]["detail"]["source"]["kind"] == "file"
    assert events[0]["detail"]["stated"] == {"customer_id": ID_A, "name": None}
    assert events[0]["detail"]["current"] == {"customer_id": ID_B, "name": None}
    assert events[0]["detail"]["previous"] is None

    # 列找回来之后：连接上的提醒消失（last_stats 不再带 problems），之前暂不接收的新记录自动接收并按指定分配
    for fdef in table["fields"]:
        if fdef["field_name"] == "买家号ID（改名）":
            fdef["field_name"] = "买家号id"
    clock.freeze(clock.now() + timedelta(hours=30))
    sync = ok(owner.post(f"/api/v1/feishu/connections/{cid}/sync"))
    assert sync["new"] == 1 and sync["problems"] == []
    listed = {c["id"]: c for c in ok(owner.get("/api/v1/feishu/connections"))["rows"]}[cid]
    assert "problems" not in listed["last_stats"]
    new_row = find_order(owner, "PO-DEMO-S0002")
    assert (new_row["assignment_kind"], new_row["assigned_account"]["id"]) == ("source", b["id"])


# ── 跨工作区 ─────────────────────────────────────────────────────────────────


def test_designation_resolves_only_inside_its_own_workspace(owner, other, api):
    """铁律 5：指定只在任务自己的工作区里找账号。B 的来源写了 A 的账号名称与买家号 ID → 在 B 里是「没有登记」，
    看不到 A 的账号；待登记的买家号、忽略名单、分配摘要、导出各看各的；B 登记后只动 B 的任务。"""
    a = create_account(owner, name="环境-DEMO-A", amazon_customer_id=ID_A)
    _import(owner, [_row("PO-DEMO-WA001", cid=ID_A), _row("PO-DEMO-WA002", cid=ID_NEW)])
    imp_b = _import(other, [_row("PO-DEMO-WB001", cid=ID_A), _row("PO-DEMO-WB002", name="环境-DEMO-A"),
                            _row("PO-DEMO-WB003", cid="A1DEMO444444")])

    b_rows = _rows(other)
    assert set(b_rows) == {"PO-DEMO-WB001", "PO-DEMO-WB002", "PO-DEMO-WB003"}
    for no in b_rows:
        r = b_rows[no]
        assert (r["status"], r["assigned_account"], r["designated_buyer"]["account"]) == ("queued", None, None), no
    assert b_rows["PO-DEMO-WB001"]["status_reason"] == \
        f"来源指定的买家号 ID {ID_A} 还没有登记为账号；在「账号与浏览器」登记后会自动分配"
    assert b_rows["PO-DEMO-WB002"]["status_reason"] == \
        "来源指定的买家号「环境-DEMO-A」没有同名账号；新建同名账号、在来源里改正，或改派到其他账号"
    alloc_b = imp_b["commit"]["allocation"]
    assert alloc_b["stats"]["designated"] == 0 and alloc_b["stats"]["unassigned"] == 3
    assert {u["upstream_order_no"] for u in alloc_b["unassigned"]} == set(b_rows)
    assert ok(other.get("/api/v1/allocation/last"))["id"] == alloc_b["id"]
    assert ok(owner.get("/api/v1/allocation/last"))["id"] != alloc_b["id"]

    # 待登记的买家号：各看各的（A 里 ID_A 已登记，不在待登记；B 里它没登记）
    assert {(i["customer_id"], i["source_orders"]) for i in _discovered(owner)} == {(ID_NEW, 1)}
    assert {(i["customer_id"], i["source_orders"]) for i in _discovered(other)} == {(ID_A, 1), ("A1DEMO444444", 1)}
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 1
    assert ok(other.get("/api/v1/overview"))["discovered_buyers"] == 2
    # 忽略名单按工作区分开：B 忽略不影响 A，A 的 include_ignored 里也看不到 B 忽略的
    ok(other.post("/api/v1/accounts/discovered/ignore", json={"customer_id": ID_NEW}))
    ok(other.post("/api/v1/accounts/discovered/ignore", json={"customer_id": "A1DEMO444444"}))
    assert {i["customer_id"] for i in _discovered(owner, include_ignored=1)} == {ID_NEW}
    assert _discovered(owner, include_ignored=1)[0]["ignored"] is False
    assert {i["customer_id"]: i["ignored"] for i in _discovered(other, include_ignored=1)} == {
        ID_A: False, ID_NEW: True, "A1DEMO444444": True}
    assert ok(owner.get("/api/v1/overview"))["discovered_buyers"] == 1
    assert ok(other.get("/api/v1/overview"))["discovered_buyers"] == 1

    # B 读不到、也动不了 A 的指定任务与它的导出
    wa = find_order(owner, "PO-DEMO-WA001")
    err(other.get(f"/api/v1/orders/{wa['id']}"), 404, "NOT_FOUND")
    err(other.post(f"/api/v1/orders/{wa['id']}/unassign"), 404, "NOT_FOUND")
    assert {r["平台订单号"] for r in _export(other, view="all")} == set(b_rows)
    assert {r["平台订单号"] for r in _export(owner, view="all")} == {"PO-DEMO-WA001", "PO-DEMO-WA002"}
    assert ok(other.get("/api/v1/events", params={"order_id": wa["id"]}))["total"] == 0

    # B 登记同一个买家号 ID / 同名账号：只有 B 的任务分过去，A 的任务原封不动
    b_acc = create_account(other, name="环境-DEMO-A", amazon_customer_id=ID_A)
    b_rows = _rows(other)
    for no in ("PO-DEMO-WB001", "PO-DEMO-WB002"):
        assert (b_rows[no]["status"], b_rows[no]["assignment_kind"], b_rows[no]["assigned_account"]["id"]) == \
            ("assigned", "source", b_acc["id"]), no
        assert b_rows[no]["designated_buyer"]["account"] == {"id": b_acc["id"], "name": "环境-DEMO-A"}
    a_rows = _rows(owner)
    assert a_rows["PO-DEMO-WA001"]["assigned_account"] == {"id": a["id"], "name": "环境-DEMO-A"}
    assert a_rows["PO-DEMO-WA001"]["designated_buyer"]["account"]["id"] == a["id"] != b_acc["id"]
    assert a_rows["PO-DEMO-WA002"]["status"] == "queued"
    assert a_rows["PO-DEMO-WA002"]["status_reason"] == \
        f"来源指定的买家号 ID {ID_NEW} 还没有登记为账号；在「账号与浏览器」登记后会自动分配"   # B 的忽略没有渗过来
