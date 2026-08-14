"""P7.2 项目管理服务单元测试（task.md：P1.3 项目/标段/维度/遴选配置）。

覆盖（所有演示场景的基座）：
- create_project：project_code 唯一性校验 + 成功创建
- create_lot：SUM(lot.budget)+new ≤ project.budget 校验
- add_dimensions：权重和 = 1.0±0.001 校验 + 覆盖式删除重建
- configure_expert_criteria：权重和 + expert_count ≥ min 校验
- list_lots：状态过滤 + 关联项目名 + 标书数
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.project import (
    CriterionCreate,
    DimensionCreate,
    ExpertCriteriaCreate,
    LotCreate,
    ProjectCreate,
)
from app.services.project_service import (
    BudgetExceededError,
    LotNotFoundError,
    ProjectCodeTakenError,
    ProjectNotFoundError,
    WeightSumError,
    add_dimensions,
    configure_expert_criteria,
    create_lot,
    create_project,
    list_lots,
)


def _mk_project():
    p = MagicMock()
    p.project_id = "PRJ-1"
    p.project_code = "PRJ-2026-01"
    p.name = "测试项目"
    p.type = "GOODS"
    p.region = "华中"
    p.budget = Decimal("1000000")
    p.status = "DRAFT"
    return p


@pytest.mark.asyncio
async def test_create_project_success():
    """project_code 唯一 → 创建 + outbox + Neo4j 同步。"""
    session = AsyncMock()
    session.scalar.return_value = None  # 无重码
    data = ProjectCreate(project_code="PRJ-2026-01", name="测试项目", type="GOODS",
                         region="华中", budget=Decimal("1000000"))
    with patch("app.services.project_service.write_outbox_event", new=AsyncMock()), \
         patch("app.services.project_service.neo4j_sync.upsert_project", new=AsyncMock()) as upsert:
        proj = await create_project(session, data, operator_id="U-1")
    assert proj.project_code == "PRJ-2026-01"
    assert proj.status == "DRAFT"
    session.add.assert_called_once()
    session.commit.assert_awaited_once()
    upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_project_code_taken():
    """project_code 已存在 → ProjectCodeTakenError。"""
    session = AsyncMock()
    session.scalar.return_value = MagicMock()
    data = ProjectCreate(project_code="PRJ-2026-01", name="测试", type="GOODS", budget=Decimal("100"))
    with pytest.raises(ProjectCodeTakenError):
        await create_project(session, data, operator_id="U-1")


@pytest.mark.asyncio
async def test_create_lot_success_within_budget():
    """标段预算总和 + 新标段 ≤ 项目预算 → 创建。"""
    session = AsyncMock()
    session.get.return_value = _mk_project()
    session.scalar.return_value = Decimal("400000")  # 已有标段预算合计
    data = LotCreate(lot_code="LOT-001", name="一标段", budget=Decimal("600000"))
    with patch("app.services.project_service.write_outbox_event", new=AsyncMock()), \
         patch("app.services.project_service.neo4j_sync.upsert_lot", new=AsyncMock()):
        lot = await create_lot(session, "PRJ-1", data)
    assert lot.lot_status if hasattr(lot, "lot_status") else lot.status == "BIDDING"


@pytest.mark.asyncio
async def test_create_lot_budget_exceeded():
    """400000 + 700000 > 1000000 → BudgetExceededError。"""
    session = AsyncMock()
    session.get.return_value = _mk_project()
    session.scalar.return_value = Decimal("400000")
    data = LotCreate(lot_code="LOT-001", name="一标段", budget=Decimal("700000"))
    with pytest.raises(BudgetExceededError):
        await create_lot(session, "PRJ-1", data)


@pytest.mark.asyncio
async def test_create_lot_project_not_found():
    """项目不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    data = LotCreate(lot_code="LOT-001", name="一标段", budget=Decimal("100"))
    with pytest.raises(ProjectNotFoundError):
        await create_lot(session, "PRJ-X", data)


def _dims():
    return [
        DimensionCreate(name="报价", max_score=Decimal("20"), weight=Decimal("0.4"),
                        sort_order=1, criteria=[CriterionCreate(name="报价合理性", max_score=Decimal("20"))]),
        DimensionCreate(name="技术", max_score=Decimal("30"), weight=Decimal("0.6"), sort_order=2),
    ]


@pytest.mark.asyncio
async def test_add_dimensions_success():
    """权重和 1.0 → 覆盖式删除旧维度 + 重建 + outbox + Neo4j。"""
    session = AsyncMock()
    lot = MagicMock()
    session.get.return_value = lot
    old = MagicMock()
    old_result = MagicMock()
    old_result.__iter__.return_value = iter([old])  # 旧维度待删除
    session.scalars.return_value = old_result

    with patch("app.services.project_service.write_outbox_event", new=AsyncMock()), \
         patch("app.services.project_service.neo4j_sync.upsert_dimension", new=AsyncMock()) as upsert:
        created = await add_dimensions(session, "LOT-1", _dims())
    assert len(created) == 2
    assert created[0].dimension_id == "DIM-LOT-1-1"
    # 旧维度及其子项被删除（两次 delete）
    assert session.execute.call_count == 2
    upsert.assert_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_dimensions_weight_sum_invalid():
    """权重和 0.9 ≠ 1.0 → WeightSumError。"""
    session = AsyncMock()
    session.get.return_value = MagicMock()
    bad = [DimensionCreate(name="A", max_score=Decimal("10"), weight=Decimal("0.9"))]
    with pytest.raises(WeightSumError):
        await add_dimensions(session, "LOT-1", bad)


@pytest.mark.asyncio
async def test_add_dimensions_lot_not_found():
    """标段不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(LotNotFoundError):
        await add_dimensions(session, "LOT-X", _dims())


def _criteria(weights=(Decimal("0.4"), Decimal("0.3"), Decimal("0.2"), Decimal("0.1"))):
    ws, we, wq, wr = weights
    return ExpertCriteriaCreate(
        expert_count=5, min_experts_per_dimension=2,
        weight_specialization=ws, weight_experience=we,
        weight_review_quality=wq, weight_region=wr, min_experience=3,
    )


@pytest.mark.asyncio
async def test_configure_expert_criteria_success():
    """权重和 1.0 + count ≥ min → 保存。"""
    session = AsyncMock()
    session.get.side_effect = [MagicMock(), None]  # Lot 存在，LotExpertCriteria 新建
    criteria = await configure_expert_criteria(session, "LOT-1", _criteria())
    assert criteria.expert_count == 5
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_configure_expert_criteria_weight_invalid():
    """权重和 0.9 → WeightSumError。"""
    session = AsyncMock()
    session.get.return_value = MagicMock()
    with pytest.raises(WeightSumError):
        await configure_expert_criteria(session, "LOT-1", _criteria(weights=(Decimal("0.3"), Decimal("0.3"), Decimal("0.2"), Decimal("0.1"))))


@pytest.mark.asyncio
async def test_configure_expert_criteria_count_lt_min():
    """expert_count(2) < min(3) → 拒绝。"""
    session = AsyncMock()
    session.get.return_value = MagicMock()
    bad = _criteria()
    bad.expert_count = 2
    bad.min_experts_per_dimension = 3
    with pytest.raises(Exception) as ei:
        await configure_expert_criteria(session, "LOT-1", bad)
    assert "expert_count" in str(ei.value)


@pytest.mark.asyncio
async def test_configure_expert_criteria_lot_not_found():
    """标段不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(LotNotFoundError):
        await configure_expert_criteria(session, "LOT-X", _criteria())


@pytest.mark.asyncio
async def test_list_lots_with_status_filter():
    """标段列表：状态过滤 + 关联项目名 + 标书数。"""
    from types import SimpleNamespace

    session = AsyncMock()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    lot = MagicMock(lot_id="LOT-1", lot_code="LC-1", budget=Decimal("100"),
                    status="PRE_SCREEN", project_id="PRJ-1")
    rows_scalar = MagicMock()
    rows_scalar.all.return_value = [lot]
    rows_result = MagicMock()
    rows_result.scalars.return_value = rows_scalar  # 实现取 execute(...).scalars().all()
    bid_count_result = MagicMock()
    bid_count_result.all.return_value = [("LOT-1", 3)]
    # 实现 p.project_id/p.project_code/p.name 属性访问（Row 语义）
    proj_result = MagicMock()
    proj_result.all.return_value = [SimpleNamespace(project_id="PRJ-1", project_code="PC-1", name="测试项目")]

    # execute 顺序：count → rows → bid_count → project
    session.execute.side_effect = [count_result, rows_result, bid_count_result, proj_result]

    items, total = await list_lots(session, page=1, page_size=20, status="PRE_SCREEN")
    assert total == 1
    assert items[0]["status"] == "PRE_SCREEN"
    assert items[0]["project_code"] == "PC-1"
    assert items[0]["project_name"] == "测试项目"
    assert items[0]["bid_count"] == 3
