"""来源指定买家号的纯函数（services/designation，方案 v2 修订二 §D）：清洗、来源行是否表态、解析规则的每一条原因
（逐字）、账号能不能接的每一条原因（逐字）、「分配已不符合指定」的判断。不连库；全部数据虚构。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services import designation as dz
from services.models import SourceRef, UpstreamRecord

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 2, tzinfo=timezone.utc)


def acc(id_, name, cid=None, *, archived=None, **kw):
    """虚构的账号行（只带解析与检查用到的列）。"""
    row = {"id": id_, "name": name, "amazon_customer_id": cid, "archived_at": archived, "site": "US",
           "min_amount": Decimal("0"), "max_amount": None, "daily_limit": 20, "purchase_enabled": True,
           "execution_paused": False}
    row.update(kw)
    return row


A = acc(1, "环境-DEMO-A", "A1DEMO0000")
B = acc(2, "环境-DEMO-B", "A1DEMO0001")
C_BLANK = acc(3, "环境-DEMO-C")                               # 没填买家号 ID
GONE = acc(4, "环境-DEMO-旧", "A1DEMO0009", archived=T0)      # 已归档
ALL = [A, B, C_BLANK, GONE]


def rec(**kw):
    return UpstreamRecord(upstream_order_no="PO-DEMO-0001", **kw)


# ── 清洗 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, want", [
    (" a1demo0000 ", "A1DEMO0000"),
    ("a1 demo\t00\n00", "A1DEMO0000"),                       # 去掉全部空白
    ("Ａ１ＤＥＭＯ００００", "A1DEMO0000"),                      # 全角 → 半角（NFKC）
    ("　a1demo0000　", "A1DEMO0000"),                # 全角空格
    ("", None), ("   ", None), (None, None),
    ("not-an-id", "NOT-AN-ID"),                              # 不校验形状，只清洗
])
def test_clean_customer_id(raw, want):
    assert dz.clean_customer_id(raw) == want


def test_clean_name_only_strips_and_name_key_is_loose():
    assert dz.clean_name("  环境-DEMO-A ") == "环境-DEMO-A"
    assert dz.clean_name(" 环境 A ") == "环境 A"                        # 中间的空白、大小写、全半角都原样保留
    assert dz.clean_name("") is None and dz.clean_name("  ") is None and dz.clean_name(None) is None
    assert dz.name_key("环境 A") == dz.name_key("环境A") == dz.name_key("环境Ａ") == dz.name_key(" 环境 a ")
    assert dz.name_key("环境-A") != dz.name_key("环境A")                 # 标点不忽略
    assert dz.name_key(None) == ""


def test_id_shape_and_labels():
    assert dz.id_shape_ok("A1DEMO0000") and dz.id_shape_ok("A12345678")
    for bad in (None, "", "A1234567", "B1DEMO0000", "a1demo0000", "A1DEMO-000", "#N/A"):
        assert not dz.id_shape_ok(bad), bad
    assert dz.label("A1DEMO0000", "环境-DEMO-A") == "A1DEMO0000"         # 有 ID 写 ID
    assert dz.label(None, "环境-DEMO-A") == "环境-DEMO-A" and dz.label(None, None) is None
    assert dz.label_or_empty(None, None) == "（未指定）"
    # 两部分都写的格式：名称（ID），与任务详情一致；只有一部分就写那一部分
    assert dz.label_both("A1DEMO0000", "环境-DEMO-A") == "环境-DEMO-A（A1DEMO0000）"
    assert dz.label_both("A1DEMO0000", None) == "A1DEMO0000" and dz.label_both(None, "环境-DEMO-A") == "环境-DEMO-A"
    assert dz.label_both(None, None) == "（未指定）"


def test_labels_never_show_two_different_designations_as_the_same_text():
    """ING-7：新旧两边对比着写时，按「有 ID 写 ID」显示成同一个标签（只有名称变了）就改用 名称（ID）；
    不能出现「由「X」改为「X」」。"""
    assert dz.labels(("A1DEMO0000", None), ("A1DEMO0001", None)) == ("A1DEMO0000", "A1DEMO0001")     # 照常：有 ID 写 ID
    assert dz.labels((None, None), (None, "环境-DEMO-A")) == ("（未指定）", "环境-DEMO-A")
    assert dz.labels(("A1DEMO0000", "环境-DEMO-A"), ("A1DEMO0001", "环境-DEMO-B")) == ("A1DEMO0000", "A1DEMO0001")
    assert dz.labels(("A1DEMO0000", None), ("A1DEMO0000", "环境-DEMO-B")) == (
        "A1DEMO0000", "环境-DEMO-B（A1DEMO0000）")
    assert dz.labels(("A1DEMO0000", "环境-DEMO-A"), ("A1DEMO0000", "环境-DEMO-B")) == (
        "环境-DEMO-A（A1DEMO0000）", "环境-DEMO-B（A1DEMO0000）")
    # 同一段文字从一列挪到了另一列：把列名写出来
    assert dz.labels((None, "A1DEMO0000"), ("A1DEMO0000", None)) == ("买家号 A1DEMO0000", "买家号 ID A1DEMO0000")
    same = dz.labels(("A1DEMO0000", "环境-DEMO-A"), ("A1DEMO0000", "环境-DEMO-A"))                   # 本来就相同：照常
    assert same == ("A1DEMO0000", "A1DEMO0000")


# ── 来源行是否表态 ────────────────────────────────────────────────────────────


def test_statement_without_order_number_is_a_cleaned_pair():
    assert dz.statement(rec(external_buyer_customer_id=" a1demo0000 ", external_buyer_account=" 环境-DEMO-A ")) == (
        "A1DEMO0000", "环境-DEMO-A")
    assert dz.statement(rec(external_buyer_account="环境-DEMO-A")) == (None, "环境-DEMO-A")
    assert dz.statement(rec(external_buyer_customer_id="A1DEMO0000")) == ("A1DEMO0000", None)
    empty = dz.statement(rec())
    assert empty == (None, None) and empty is not dz.SILENT and dz.is_empty(empty)
    assert not dz.is_empty(dz.SILENT) and not dz.is_empty(("A1DEMO0000", None))


def test_statement_is_silent_with_a_credible_order_number_or_when_unreadable():
    """修订二 §A 第 2、4 条：带可信单号的行、读不到买家号列的读取都不表态；格式不对的单号不算可信。"""
    with_no = rec(external_amz_order_no="111-0000000-0000001", external_buyer_account="环境-DEMO-A",
                  external_buyer_customer_id="A1DEMO0000")
    assert dz.statement(with_no) is dz.SILENT
    assert dz.statement(rec(external_amz_order_no=" 111-0000000-0000001 ")) is dz.SILENT
    malformed = rec(external_amz_order_no="重拍", external_buyer_account="环境-DEMO-A")
    assert dz.statement(malformed) == (None, "环境-DEMO-A")
    assert dz.statement(rec(external_buyer_account="环境-DEMO-A"), readable=False) is dz.SILENT
    assert dz.statement(None) is dz.SILENT
    assert not dz.SILENT and repr(dz.SILENT) == "SILENT"


def test_statement_leaves_an_unread_field_out_and_is_silent_when_both_are_unread():
    """读没读到按列算：没读到的字段记 None（不是「表态为空」，由调用方保持原值）；两个都没读到才不表态。"""
    both = rec(external_buyer_customer_id="A1DEMO0000", external_buyer_account="环境-DEMO-A")
    k_id, k_name = dz.RECORD_KEYS
    assert (k_id, k_name) == ("external_buyer_customer_id", "external_buyer_account")
    assert dz.statement(both, unread=(k_id,)) == (None, "环境-DEMO-A")
    assert dz.statement(both, unread=(k_name,)) == ("A1DEMO0000", None)
    assert dz.statement(both, unread=dz.RECORD_KEYS) is dz.SILENT
    assert dz.statement(both, unread=()) == ("A1DEMO0000", "环境-DEMO-A")
    with_no = rec(external_amz_order_no="111-0000000-0000001", external_buyer_account="环境-DEMO-A")
    assert dz.statement(with_no, unread=(k_id,)) is dz.SILENT                 # 可信单号仍然优先


def test_unread_keys_is_the_one_place_that_reads_the_source_flags():
    k_id, k_name = dz.RECORD_KEYS

    def src(**kw):
        return SourceRef(kind="file", snapshot={}, batch_id=1, row_no=2, **kw)

    assert dz.unread_keys(src()) == () and dz.unread_keys(None) == ()
    assert dz.unread_keys(src(buyer_unread=(k_name,))) == (k_name,)
    assert dz.unread_keys(src(buyer_unread=[k_name, k_id])) == (k_id, k_name)          # 列表也收，按固定顺序
    assert dz.unread_keys(src(buyer_readable=False)) == dz.RECORD_KEYS                 # 原义：两个都没读到
    assert dz.unread_keys(src(buyer_readable=False, buyer_unread=(k_id,))) == dz.RECORD_KEYS
    with pytest.raises(ValueError):
        src(buyer_unread=("buyer_account",))                                           # 写错的字段名不能悄悄当成读到了


def test_never_read_reads_the_marker_stored_on_a_link():
    k_id, k_name = dz.RECORD_KEYS
    assert dz.UNREAD_MARK == "buyer_unread"
    assert dz.never_read(None) == () and dz.never_read({}) == ()                       # 旧关联没有标记 = 都读到过
    assert dz.never_read({"buyer_unread": [k_name]}) == (k_name,)
    assert dz.never_read({"buyer_unread": [k_name, k_id, "别的"]}) == (k_id, k_name)
    assert dz.never_read({"buyer_unread": "external_buyer_account"}) == ()             # 形状不对按没有标记
    # 带标记的规范化记录照样能还原成 UpstreamRecord（多出来的键被忽略）
    stored = {**rec(external_buyer_account="环境-DEMO-A").to_json(), "buyer_unread": [k_id]}
    assert UpstreamRecord.from_json(stored).external_buyer_account == "环境-DEMO-A"


@pytest.mark.parametrize("placeholder", ["无", "—", "N/A", "n/a", "0", "/", " - ", "没有", "#"])
def test_statement_treats_placeholders_as_not_written(placeholder):
    """旧版存下的规范化记录里占位写法还是原文：statement 也按没写处理（回填与「上次说的」都靠它）。"""
    assert dz.statement(rec(external_buyer_account=placeholder, external_buyer_customer_id=placeholder)) == (None, None)
    assert dz.statement(rec(external_buyer_account=placeholder, external_buyer_customer_id="A1DEMO0000")) == (
        "A1DEMO0000", None)


def test_statement_keeps_spreadsheet_error_values_as_an_unresolvable_designation():
    """表格错误值不当成留空：它是一个解析不了的指定。"""
    said = dz.statement(rec(external_buyer_account="#N/A"))
    assert said == (None, "#N/A") and not dz.is_empty(said)
    assert dz.resolve(None, "#N/A", ALL).account is None
    assert dz.resolve("#REF!", None, ALL).account is None


def test_error_literal_is_reported_as_unreadable_not_as_a_missing_account():
    """WEB-8：表格错误值（#N/A、#REF! …）的原因说的是「读不出指定的买家号」，不再建议「新建同名账号」；
    也不去匹配账号——名叫 #N/A 的账号接不走这种任务。"""
    named = acc(9, "#N/A", "A1DEMO0008")
    pool = [*ALL, named]
    r = dz.resolve(None, "#N/A", pool)
    assert r.account is None and r.note is None
    assert r.problem == "来源的买家号是表格错误值 #N/A，读不出指定的买家号；在来源里改正后会重新分配"
    assert dz.resolve(None, " #n/a ".strip(), pool).account is None                    # 大小写不同也认得
    r = dz.resolve("#REF!", None, pool)
    assert r.account is None
    assert r.problem == "来源的买家号 ID 是表格错误值 #REF!，读不出指定的买家号；在来源里改正后会重新分配"
    # 两个都是错误值：说买家号 ID；ID 正常而名称是错误值：也不按 ID 分走（读不出的那一列可能指向别的账号）
    assert dz.resolve("#VALUE!", "#N/A", pool).problem.startswith("来源的买家号 ID 是表格错误值 #VALUE!")
    r = dz.resolve("A1DEMO0000", "#N/A", pool)
    assert r.account is None and r.problem.startswith("来源的买家号是表格错误值 #N/A")
    r = dz.resolve("#DIV/0!", "环境-DEMO-A", pool)
    assert r.account is None and r.problem.startswith("来源的买家号 ID 是表格错误值 #DIV/0!")
    # 普通的名称、以 # 开头但不是错误值的名称照旧按名称解析
    assert dz.resolve(None, "环境-DEMO-A", pool).account == A
    assert dz.resolve(None, "#1 号", [*pool, acc(10, "#1 号")]).account["id"] == 10


def test_order_helpers():
    row = {"designated_customer_id": None, "designated_buyer_name": None}
    assert not dz.has(row) and dz.of_order(row) == (None, None) and dz.out_of(row, None) is None
    row = {"designated_customer_id": "A1DEMO0000", "designated_buyer_name": None}
    assert dz.has(row) and dz.of_order(row) == ("A1DEMO0000", None)
    assert dz.has({"designated_customer_id": None, "designated_buyer_name": "环境-DEMO-A"})
    assert not dz.has({})                                                    # 旧形状的行（没有这两列）


# ── 解析规则 §D 第 1 条：有买家号 ID ──────────────────────────────────────────


def test_no_designation_resolves_to_nothing():
    assert dz.resolve(None, None, ALL) == dz.Resolution(None, None, None)


def test_id_with_a_bad_shape():
    r = dz.resolve("X1DEMO", "环境-DEMO-A", ALL)
    assert r.account is None and r.note is None
    assert r.problem == ("来源指定的买家号 ID「X1DEMO」格式不对（应以 A 开头，至少 9 位大写字母或数字）；"
                         "在来源里改正或清空后会重新分配")
    long = "A1DEMO" + "-" * 60
    r = dz.resolve(long, None, ALL)
    assert f"「{long[:40]}…」" in r.problem                                  # 原文最多 40 字


def test_id_registered_on_an_unarchived_account():
    r = dz.resolve("A1DEMO0000", None, ALL)
    assert r.account is A and r.problem is None and r.note == "按来源的买家号 ID A1DEMO0000"
    # 同时写了名称、名称就是这个账号（逐字或宽松）→ 同样命中，说明不加括号
    assert dz.resolve("A1DEMO0000", "环境-DEMO-A", ALL) == dz.Resolution(A, None, "按来源的买家号 ID A1DEMO0000")
    assert dz.resolve("A1DEMO0000", "环境 - demo - a", ALL) == dz.Resolution(A, None, "按来源的买家号 ID A1DEMO0000")


def test_id_and_name_point_at_different_accounts():
    r = dz.resolve("A1DEMO0000", "环境-DEMO-B", ALL)
    assert r.account is None and r.note is None
    assert r.problem == ("来源指定的买家号「环境-DEMO-B」（账号 环境-DEMO-B）与买家号 ID A1DEMO0000"
                         "（账号 环境-DEMO-A）不是同一个账号；在来源里改正其中一个后会重新分配")
    # 名称宽松匹配到另一个账号也算
    r = dz.resolve("A1DEMO0000", "环境-demo-b", ALL)
    assert r.problem.startswith("来源指定的买家号「环境-demo-b」（账号 环境-DEMO-B）与买家号 ID A1DEMO0000")


def test_id_wins_when_the_name_matches_no_account():
    r = dz.resolve("A1DEMO0000", "环境-DEMO-不存在", ALL)
    assert r.account is A and r.problem is None
    assert r.note == "按来源的买家号 ID A1DEMO0000（来源写的买家号「环境-DEMO-不存在」与账号名称不同）"
    # 名称只对得上已归档账号：当作谁也对不上，仍按 ID
    assert dz.resolve("A1DEMO0000", "环境-DEMO-旧", ALL).account is A


def test_id_only_on_an_archived_account():
    r = dz.resolve("A1DEMO0009", None, ALL)
    assert r.account is None
    assert r.problem == "来源指定的买家号 ID A1DEMO0009 对应的账号 环境-DEMO-旧 已归档；重新登记这个买家号，或改派到其他账号"
    # 多个已归档账号登记过同一个 ID：写最近归档的那个；名称对得上别的账号也先报已归档
    newer = acc(5, "环境-DEMO-更旧", "A1DEMO0009", archived=T1)
    r = dz.resolve("A1DEMO0009", "环境-DEMO-C", [*ALL, newer])
    assert r.problem.startswith("来源指定的买家号 ID A1DEMO0009 对应的账号 环境-DEMO-更旧 已归档")
    # 重新登记到未归档账号后就按它解析
    again = acc(6, "环境-DEMO-新", "A1DEMO0009")
    assert dz.resolve("A1DEMO0009", None, [*ALL, again]).account is again


def test_id_not_registered_but_the_name_matches_an_account():
    r = dz.resolve("A1DEMO0777", "环境-DEMO-C", ALL)
    assert r.account is None
    assert r.problem == "来源指定的买家号 ID A1DEMO0777 还没有登记；同名账号 环境-DEMO-C 还没有填买家号 ID，填上后会自动分配"
    r = dz.resolve("A1DEMO0777", "环境-DEMO-B", ALL)
    assert r.account is None
    assert r.problem == ("账号 环境-DEMO-B 登记的买家号 ID 是 A1DEMO0001，与来源指定的 A1DEMO0777 不同；"
                         "在来源里改正，或修改账号的买家号 ID")
    # 宽松匹配同样算「同名账号」
    assert "同名账号 环境-DEMO-C 还没有填买家号 ID" in dz.resolve("A1DEMO0777", "环境-demo-c", ALL).problem


def test_id_not_registered_anywhere():
    r = dz.resolve("A1DEMO0777", None, ALL)
    assert r.account is None
    assert r.problem == "来源指定的买家号 ID A1DEMO0777 还没有登记为账号；在「账号与浏览器」登记后会自动分配"
    r = dz.resolve("A1DEMO0777", "环境-DEMO-新名字", ALL)
    assert r.problem == ("来源指定的买家号 ID A1DEMO0777（买家号「环境-DEMO-新名字」）还没有登记为账号；"
                         "在「账号与浏览器」登记后会自动分配")
    r = dz.resolve("A1DEMO0777", None, ALL, ignored_ids={"A1DEMO0777"})
    assert r.problem == ("来源指定的买家号 ID A1DEMO0777 还没有登记为账号；在「账号与浏览器」登记后会自动分配"
                         "（这个买家号 ID 已被忽略，恢复后才能登记）")
    r = dz.resolve("A1DEMO0777", "环境-DEMO-新名字", ALL, ignored_ids=["A1DEMO0777", "A1DEMO0888"])
    assert r.problem.endswith("登记后会自动分配（这个买家号 ID 已被忽略，恢复后才能登记）")
    # 名称宽松匹配到多个账号：不猜是哪一个，按「还没有登记」
    twins = [acc(7, "环境 X"), acc(8, "环境X")]
    assert "还没有登记为账号" in dz.resolve("A1DEMO0777", "环境Ｘ", twins).problem


# ── 解析规则 §D 第 2 条：只有名称 ────────────────────────────────────────────


def test_name_exact_match():
    r = dz.resolve(None, "环境-DEMO-B", ALL)
    assert r.account is B and r.problem is None and r.note == "按来源的买家号「环境-DEMO-B」"
    # 没填买家号 ID 的账号也能按名称指定（与自动分配一样不要求填了 ID）
    assert dz.resolve(None, "环境-DEMO-C", ALL).account is C_BLANK


@pytest.mark.parametrize("written", ["环境-demo-a", "环境 - DEMO - A", "环境－ＤＥＭＯ－Ａ", "环境-Demo-a"])
def test_name_loose_match_ignores_spaces_width_and_case(written):
    """验收 修订二 第 1 条：只填买家号 → 同名账号（忽略空格、全半角、大小写差异）。"""
    r = dz.resolve(None, written, ALL)
    assert r.account is A and r.problem is None
    assert r.note == f"按来源的买家号「{written}」（对应账号 环境-DEMO-A）"


def test_exact_name_beats_loose_matches():
    spaced, tight = acc(7, "环境 X", "A1DEMO0007"), acc(8, "环境X", "A1DEMO0008")
    assert dz.resolve(None, "环境X", [spaced, tight]) == dz.Resolution(tight, None, "按来源的买家号「环境X」")
    assert dz.resolve(None, "环境 X", [spaced, tight]).account is spaced


def test_name_matches_several_accounts():
    spaced, tight = acc(7, "环境 X"), acc(8, "环境X")
    r = dz.resolve(None, "环境Ｘ", [tight, spaced])
    assert r.account is None
    assert r.problem == "来源指定的买家号「环境Ｘ」对应多个账号（环境 X、环境X）；在来源里填买家号 ID"


def test_name_only_matches_an_archived_account():
    r = dz.resolve(None, "环境-DEMO-旧", ALL)
    assert r.account is None
    assert r.problem == "来源指定的买家号「环境-DEMO-旧」对应的账号已归档；在来源里改成其他买家号，或改派到其他账号"
    assert dz.resolve(None, "环境-demo-旧", ALL).problem.endswith("对应的账号已归档；在来源里改成其他买家号，或改派到其他账号")


def test_name_matches_nothing():
    r = dz.resolve(None, "环境-DEMO-不存在", ALL)
    assert r.account is None and r.note is None
    assert r.problem == "来源指定的买家号「环境-DEMO-不存在」没有同名账号；新建同名账号、在来源里改正，或改派到其他账号"
    assert dz.resolve(None, "环境-DEMO-不存在", []).problem == r.problem


def test_resolution_does_not_depend_on_account_order():
    for rows in (ALL, list(reversed(ALL))):
        assert dz.resolve("A1DEMO0000", None, rows).account is A
        assert dz.resolve(None, "环境-demo-b", rows).account is B


def test_resolve_order_and_out_shape():
    ctx = dz.Context(accounts=tuple(ALL), ignored=frozenset({"A1DEMO0777"}))
    row = {"designated_customer_id": "A1DEMO0001", "designated_buyer_name": "环境-DEMO-B"}
    res = dz.resolve_order(row, ctx)
    assert res.account is B
    assert dz.out_of(row, res) == {"customer_id": "A1DEMO0001", "name": "环境-DEMO-B",
                                   "account": {"id": 2, "name": "环境-DEMO-B"}}
    row = {"designated_customer_id": "A1DEMO0777", "designated_buyer_name": None}
    res = dz.resolve_order(row, ctx)
    assert "已被忽略" in res.problem
    assert dz.out_of(row, res) == {"customer_id": "A1DEMO0777", "name": None, "account": None}
    assert dz.out_of(row, None)["account"] is None


# ── 「分配已不符合指定」（orders._assignment_problem 与领取复核共用）──────────────


def test_off_target():
    row = {"designated_customer_id": "A1DEMO0000", "designated_buyer_name": None, "assigned_account_id": 1,
           "assignment_kind": "source"}
    assert not dz.off_target(row, dz.Resolution(A, None, "x"))                 # 就在指定的账号上
    assert dz.off_target({**row, "assigned_account_id": 2}, dz.Resolution(A, None, "x"))
    assert dz.off_target({**row, "assignment_kind": "auto", "assigned_account_id": 2}, dz.Resolution(A, None, "x"))
    assert dz.off_target(row, dz.Resolution(None, "解析不了", None))            # 解析不出账号也算
    # 人工改派不受来源约束；没有指定、没有分配都不算
    manual = {**row, "assignment_kind": "manual", "assigned_account_id": 2}
    assert not dz.off_target(manual, dz.Resolution(A, None, "x"))
    assert not dz.off_target({**row, "designated_customer_id": None}, dz.Resolution())
    assert not dz.off_target({**row, "assigned_account_id": None, "assignment_kind": None}, dz.Resolution(A, None, "x"))


# ── 指定的账号此刻能不能接（§D 的表，按顺序取第一条不满足的）──────────────────────

DAY = date(2026, 11, 18)
OK = dict(site_ok=True, subtotal=Decimal("25.00"), in_range=True, on_account=False, cap=1, day=DAY)


def test_placement_passes():
    assert dz.placement_problem(A, **OK) is None


def test_placement_reasons_exact_wording():
    ranged = acc(1, "环境-DEMO-A", "A1DEMO0000", min_amount=Decimal("20"), max_amount=Decimal("30"))
    open_ended = acc(1, "环境-DEMO-A", "A1DEMO0000", min_amount=Decimal("50"))
    off = acc(1, "环境-DEMO-A", "A1DEMO0000", purchase_enabled=False)
    paused = acc(1, "环境-DEMO-A", "A1DEMO0000", execution_paused=True)
    zero = acc(1, "环境-DEMO-A", "A1DEMO0000", daily_limit=0)
    assert dz.placement_problem(A, **{**OK, "site_ok": False}) == "任务收货国家与来源指定的账号 环境-DEMO-A 的站点不符"
    assert dz.placement_problem(A, **{**OK, "subtotal": None, "in_range": False}) == (
        "商品小计未知（缺少采购单价或数量），不能分给来源指定的账号 环境-DEMO-A")
    assert dz.placement_problem(ranged, **{**OK, "subtotal": Decimal("45.00"), "in_range": False}) == (
        "金额 $45.00 不在来源指定的账号 环境-DEMO-A 的区间 $20.00–$30.00 内；调整账号的金额区间，或改派到其他账号")
    assert dz.placement_problem(open_ended, **{**OK, "in_range": False}) == (
        "金额 $25.00 不在来源指定的账号 环境-DEMO-A 的区间 $50.00–不限 内；调整账号的金额区间，或改派到其他账号")
    assert dz.placement_problem(off, **OK) == "来源指定的账号 环境-DEMO-A 已关闭采购开关，不接收新分配；打开开关后会自动分配"
    assert dz.placement_problem(paused, **OK) == "来源指定的账号 环境-DEMO-A 已暂停执行；恢复执行后会自动分配"
    assert dz.placement_problem(zero, **{**OK, "cap": 0}) == "来源指定的账号 环境-DEMO-A 的每日上限为 0"
    assert dz.placement_problem(A, **{**OK, "cap": 0}) == (
        "来源指定的账号 环境-DEMO-A 在 2026-11-18 的额度已满（上限 20）；有空位后的分配会再安排")
    assert dz.placement_problem(A, **{**OK, "cap": -2}).startswith("来源指定的账号 环境-DEMO-A 在 2026-11-18 的额度已满")


def test_placement_takes_the_first_failing_condition_in_table_order():
    worst = acc(1, "环境-DEMO-A", "A1DEMO0000", purchase_enabled=False, execution_paused=True, daily_limit=0,
                min_amount=Decimal("50"))
    order = [
        ({"site_ok": False, "subtotal": None, "in_range": False, "cap": 0}, "站点不符"),
        ({"subtotal": None, "in_range": False, "cap": 0}, "商品小计未知"),
        ({"in_range": False, "cap": 0}, "不在来源指定的账号"),
        ({"cap": 0}, "已关闭采购开关"),
    ]
    for over, expect in order:
        assert expect in dz.placement_problem(worst, **{**OK, **over}), expect
    paused_zero = {**worst, "purchase_enabled": True}
    assert "已暂停执行" in dz.placement_problem(paused_zero, **{**OK, "cap": 0})
    # 任务当前就在这个账号上：采购开关、暂停两条不看，接着看上限与额度
    assert dz.placement_problem(worst, **{**OK, "on_account": True, "cap": 0}) == "来源指定的账号 环境-DEMO-A 的每日上限为 0"
    assert dz.placement_problem({**worst, "daily_limit": 3}, **{**OK, "on_account": True, "cap": 1}) is None
