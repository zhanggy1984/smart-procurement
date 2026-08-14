"""P7.2 ExpertDeclarationService 单元测试（task.md：3 用例）。

覆盖：全部确认无冲突 → IN_PROGRESS、申报冲突 → CONFLICT_DECLARED、
非本人 assignment / 已处理重复申报 → 拒绝。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.expert_declaration_service import (
    AlreadyDeclaredError,
    AssignmentAccessDeniedError,
    AssignmentNotFoundError,
    declare,
)


@pytest.fixture
def session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_declare_all_clean(session):
    """全部确认无冲突 → IN_PROGRESS。"""
    assignment = MagicMock()
    assignment.expert_id = "EXP-1"
    assignment.status = "PENDING_DECLARATION"
    assignment.lot_id = "LOT-1"
    assignment.id = 10
    session.get.return_value = assignment
    with patch("app.services.expert_declaration_service.notification.send_to_expert", new=AsyncMock()):
        res = await declare(session, assignment_id=10, expert_id="EXP-1",
                            confirmations=[{"supplier_id": "S1", "has_conflict": False}])
    assert res["status"] == "IN_PROGRESS"
    assert assignment.status == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_declare_conflict(session):
    """申报冲突 → CONFLICT_DECLARED + 补充匹配。"""
    assignment = MagicMock()
    assignment.expert_id = "EXP-1"
    assignment.status = "PENDING_DECLARATION"
    assignment.lot_id = "LOT-1"
    assignment.id = 10
    session.get.return_value = assignment
    # declare 内是函数级局部 import（from app.services import neo4j_sync），
    # 不产生模块属性，patch 必须打在真模块 app.services.neo4j_sync 上
    with patch("app.services.expert_declaration_service.notification.send_to_expert", new=AsyncMock()), \
         patch("app.services.neo4j_sync.upsert_conflict_relation", new=AsyncMock()), \
         patch("app.services.expert_declaration_service._supplement", new=AsyncMock(return_value="EXP-9")):
        res = await declare(session, assignment_id=10, expert_id="EXP-1",
                            confirmations=[{"supplier_id": "S1", "has_conflict": True, "relation_type": "HOLDS_SHARE"}])
    assert res["status"] == "CONFLICT_DECLARED"
    assert res["declared_conflicts"] == ["S1"]
    assert res["supplemented_expert"] == "EXP-9"


@pytest.mark.asyncio
async def test_declare_reject_not_own(session):
    """非本人 assignment → AssignmentAccessDeniedError。"""
    assignment = MagicMock()
    assignment.expert_id = "EXP-2"  # 不是申报人 EXP-1
    session.get.return_value = assignment
    with pytest.raises(AssignmentAccessDeniedError):
        await declare(session, assignment_id=10, expert_id="EXP-1", confirmations=[])


@pytest.mark.asyncio
async def test_declare_reject_already_declared(session):
    """已处理（非 PENDING_DECLARATION）→ AlreadyDeclaredError。"""
    assignment = MagicMock()
    assignment.expert_id = "EXP-1"
    assignment.status = "IN_PROGRESS"
    session.get.return_value = assignment
    with pytest.raises(AlreadyDeclaredError):
        await declare(session, assignment_id=10, expert_id="EXP-1", confirmations=[])


@pytest.mark.asyncio
async def test_declare_not_found(session):
    """assignment 不存在 → AssignmentNotFoundError。"""
    session.get.return_value = None
    with pytest.raises(AssignmentNotFoundError):
        await declare(session, assignment_id=999, expert_id="EXP-1", confirmations=[])
