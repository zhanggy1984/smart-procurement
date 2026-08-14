"""P7.2 ExpertMatchService 匹配流程单元测试（补深：match_experts 全流程）。

覆盖（演示链路场景2 关键）：
- match_experts：Step1-5 全流程 → 冲突专家排除（excluded_conflict）+ 落库
  PENDING_DECLARATION + 维度覆盖；候选不足 → insufficient 告警
- 前置校验：lot 不存在 / 非 UNDER_REVIEW / 无标签 → 400/404
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.project import Lot, Project
from app.services.expert_match_service import (
    LotNotFoundError,
    LotNotUnderReviewError,
    NoTagsError,
    match_experts,
)


def _cand(eid: str, tags: list[str], exp: int = 10, name: str = "专家", region: str = "华中") -> dict:
    return {"expert_id": eid, "name": name, "region": region, "experience": exp, "tags": set(tags)}


def _mk_lot(status: str = "UNDER_REVIEW") -> MagicMock:
    lot = MagicMock()
    lot.lot_id = "LOT-1"
    lot.status = status
    return lot


@pytest.mark.asyncio
async def test_match_experts_excludes_conflict_and_persists():
    """冲突专家被排除；干净专家落库 PENDING_DECLARATION + 维度分配。"""
    session = AsyncMock()
    lot = _mk_lot()
    project = MagicMock()
    project.region = "华中"
    session.get.side_effect = [lot, project, None]  # Lot, Project, LotExpertCriteria=None

    sup_result = MagicMock()
    sup_result.all.return_value = ["S1", "S2", "S3"]
    dims = [MagicMock(dimension_id="D1"), MagicMock(dimension_id="D2")]
    dim_result = MagicMock()
    dim_result.all.return_value = dims
    # scalars 第一次=_load_bidding_suppliers，第二次=维度列表
    session.scalars.side_effect = [sup_result, dim_result]
    session.scalar.return_value = None  # existing 检查：均新建

    cands = [
        _cand("EXP-1", ["软件开发"]),
        _cand("EXP-2", ["云计算"], exp=15, name="专家二"),
        _cand("EXP-3", ["软件开发"], exp=20, name="持股专家"),  # 与 S1 有持股关系
    ]
    with patch("app.services.expert_match_service._load_candidates", new=AsyncMock(return_value=cands)), \
         patch("app.services.expert_match_service._find_conflicts",
               new=AsyncMock(return_value={"EXP-3": ["HOLDS_SHARE"]})), \
         patch("app.services.expert_match_service._load_review_quality", new=AsyncMock(return_value={})):
        res = await match_experts(session, lot_id="LOT-1", tags=["软件开发", "云计算"], operator_id="U-1")

    assert res["excluded_conflict"] == ["EXP-3"]
    assigned_ids = [a["expert_id"] for a in res["assigned"]]
    assert "EXP-3" not in assigned_ids
    assert set(assigned_ids) == {"EXP-1", "EXP-2"}
    # 默认 expert_count=5，候选仅 2 干净 → 不足告警
    assert res["insufficient"] is True
    assert res["match_mode"] == "AUTO"
    # 落库：2 条 PENDING_DECLARATION
    assert session.add.call_count == 2
    persisted = [call.args[0] for call in session.add.call_args_list]
    assert all(a.status == "PENDING_DECLARATION" for a in persisted)
    assert all(a.lot_id == "LOT-1" for a in persisted)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_match_experts_reuses_existing_assignment():
    """已存在的分配记录 → 更新 dimension_ids/status，不重复创建。"""
    session = AsyncMock()
    lot = _mk_lot()
    project = MagicMock()
    project.region = "华中"
    session.get.side_effect = [lot, project, None]

    sup_result = MagicMock()
    sup_result.all.return_value = ["S1"]
    dim_result = MagicMock()
    dim_result.all.return_value = [MagicMock(dimension_id="D1")]
    session.scalars.side_effect = [sup_result, dim_result]
    existing = MagicMock()  # EXP-1 已存在分配
    session.scalar.return_value = existing

    cands = [_cand("EXP-1", ["软件开发"])]
    with patch("app.services.expert_match_service._load_candidates", new=AsyncMock(return_value=cands)), \
         patch("app.services.expert_match_service._find_conflicts", new=AsyncMock(return_value={})), \
         patch("app.services.expert_match_service._load_review_quality", new=AsyncMock(return_value={})):
        await match_experts(session, lot_id="LOT-1", tags=["软件开发"], operator_id="U-1")

    assert existing.status == "PENDING_DECLARATION"
    assert session.add.call_count == 0  # 全部复用


@pytest.mark.asyncio
async def test_match_experts_lot_not_found():
    """lot 不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(LotNotFoundError):
        await match_experts(session, lot_id="LOT-X", tags=["软件开发"], operator_id="U-1")


@pytest.mark.asyncio
async def test_match_experts_not_under_review():
    """lot 非 UNDER_REVIEW → 400。"""
    session = AsyncMock()
    session.get.return_value = _mk_lot(status="BIDDING")
    with pytest.raises(LotNotUnderReviewError):
        await match_experts(session, lot_id="LOT-1", tags=["软件开发"], operator_id="U-1")


@pytest.mark.asyncio
async def test_match_experts_no_tags():
    """无项目标签 → NoTagsError（需先标签翻译或手动选择）。"""
    session = AsyncMock()
    session.get.return_value = _mk_lot()
    with pytest.raises(NoTagsError):
        await match_experts(session, lot_id="LOT-1", tags=[], operator_id="U-1")
