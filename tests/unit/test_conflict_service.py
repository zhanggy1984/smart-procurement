"""P7.2 企查查冲突导入服务单元测试（task.md：P1.4 冲突关系）。

覆盖（演示链路场景2 持股冲突来源）：
- _parse_ratio：小数 / 百分比 / 空 / 非法
- import_conflicts：双匹配（股东→HOLDS_SHARE 带 ratio、任职→EMPLOYED_BY 带 role）
  → Neo4j 直同步；人匹配企业未匹配 → pending 冷数据；人未匹配 / 关系类型未知 → 计数
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conflict_service import _parse_ratio, import_conflicts


def test_parse_ratio():
    """持股比例解析：小数、百分比、空、非法 → 小数比例 / None。"""
    assert _parse_ratio("5%") == 0.05
    assert _parse_ratio("0.05") == 0.05
    assert _parse_ratio("") is None
    assert _parse_ratio(None) is None
    assert _parse_ratio("abc") is None
    assert _parse_ratio(" 12.5% ") == 0.125


def _iter_result(items):
    r = MagicMock()
    r.__iter__.return_value = iter(items)
    return r


@pytest.mark.asyncio
async def test_import_conflicts_full_flow():
    """双匹配（股东/任职）+ 冷数据 + 人未匹配 + 未知关系 → 计数与 Neo4j 同步。"""
    session = AsyncMock()

    def _exp(eid, name):
        e = MagicMock()
        e.expert_id = eid
        e.name = name
        return e

    expert_result = _iter_result([_exp("EXP-1", "张三"), _exp("EXP-2", "李四"), _exp("EXP-3", "王五")])

    def _sup(sid, name, code):
        s = MagicMock()
        s.supplier_id = sid
        s.name = name
        s.uniform_credit_code = code
        return s

    sup_result = _iter_result([_sup("SUP-1", "甲科技", "91310000123456789A"), _sup("SUP-2", "乙科技", None)])
    session.scalars.side_effect = [expert_result, sup_result]

    rows = [
        # 1. 股东 → HOLDS_SHARE（带比例 5% → ratio=0.05）
        {"姓名": "张三", "企业名称": "甲科技", "统一社会信用代码": "91310000123456789A",
         "关系类型": "股东", "持股比例": "5%"},
        # 2. 任职 → EMPLOYED_BY（带职位）
        {"姓名": "李四", "企业名称": "乙科技", "统一社会信用代码": "",
         "关系类型": "任职", "职位": "CEO"},
        # 3. 人匹配、企业未匹配 → pending 冷数据
        {"姓名": "王五", "企业名称": "丙科技", "统一社会信用代码": "", "关系类型": "股东"},
        # 4. 人未匹配 → person_unmatched
        {"姓名": "赵六", "企业名称": "甲科技", "统一社会信用代码": "", "关系类型": "股东"},
        # 5. 关系类型未知 → unknown_relation
        {"姓名": "张三", "企业名称": "甲科技", "统一社会信用代码": "91310000123456789A",
         "关系类型": "表亲"},
    ]

    with patch("app.services.conflict_service.write_outbox_event", new=AsyncMock()), \
         patch("app.services.conflict_service.neo4j_sync.upsert_conflict_relation", new=AsyncMock()) as upsert:
        res = await import_conflicts(session, rows, operator_id="U-1")

    assert res == {"total": 5, "matched": 2, "pending": 1,
                   "person_unmatched": 1, "unknown_relation": 1}
    session.commit.assert_awaited_once()
    # 2 条双匹配关系直同步 Neo4j（HOLDS_SHARE 带 ratio；EMPLOYED_BY 带 role）
    assert upsert.await_count == 2
    share_call = [c for c in upsert.await_args_list
                  if c.args[0] == "HOLDS_SHARE" and c.kwargs.get("ratio") == 0.05]
    assert share_call, "HOLDS_SHARE 关系应带 ratio=0.05"
    emp_call = [c for c in upsert.await_args_list
                if c.args[0] == "EMPLOYED_BY" and c.kwargs.get("role") == "CEO"]
    assert emp_call, "EMPLOYED_BY 关系应带 role"


@pytest.mark.asyncio
async def test_import_conflicts_neo4j_failure_warns_only():
    """Neo4j 直同步失败仅告警，不影响导入主链路（outbox 事件兜底重放）。"""
    session = AsyncMock()
    expert = MagicMock()
    expert.expert_id = "EXP-1"
    expert.name = "张三"  # 属性必须显式设置（MagicMock(name=...) 是调试名不是属性）
    supplier = MagicMock()
    supplier.supplier_id = "SUP-1"
    supplier.name = "甲科技"
    supplier.uniform_credit_code = "913100001234567891"
    session.scalars.side_effect = [
        _iter_result([expert]),
        _iter_result([supplier]),
    ]
    rows = [{"姓名": "张三", "企业名称": "甲科技", "统一社会信用代码": "91310000123456789A",
             "关系类型": "股东", "持股比例": "5%"}]

    with patch("app.services.conflict_service.write_outbox_event", new=AsyncMock()), \
         patch("app.services.conflict_service.neo4j_sync.upsert_conflict_relation",
               new=AsyncMock(side_effect=RuntimeError("neo4j down"))):
        res = await import_conflicts(session, rows, operator_id="U-1")
    assert res["matched"] == 1  # 同步失败不阻断导入
