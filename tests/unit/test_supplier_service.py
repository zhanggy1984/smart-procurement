"""P7.2 供应商管理服务单元测试（task.md：P1.4 导入 + 拉黑级联）。

覆盖（演示链路投标商管理）：
- _validate_credit_code：18 位数字
- import_suppliers：成功导入 + 冷数据唤醒；企业名/信用代码校验；去重 skipped
- _activate_pending_conflicts：PENDING → ACTIVATED + Neo4j 回避关系
- update_status：拉黑级联（DISQUALIFIED/SUSPENDED 路径）+ 解除还原 + 非法参数/404
- resolve_me：供应商主体唯一解析；0/多个/非供应商角色 → 拒绝
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.user import Role
from app.services.supplier_service import (
    InvalidSupplierStatusError,
    SupplierImportError,
    SupplierNotResolvableError,
    SupplierNotFoundError,
    _activate_pending_conflicts,
    _validate_credit_code,
    import_suppliers,
    resolve_me,
    update_status,
)

_CODE = "913100001234567891"  # 18 位纯数字（isdigit 校验）


def test_validate_credit_code():
    """统一社会信用代码：18 位数字合法。"""
    assert _validate_credit_code(_CODE) is True
    assert _validate_credit_code("123") is False
    assert _validate_credit_code(_CODE + "0") is False  # 19 位


def _scalar_all(items):
    """session.scalars(...) 返回 ScalarResult，实现取 .all()（同步方法）。"""
    r = MagicMock()
    r.all.return_value = items
    return r


def _ok_row(**kw):
    row = {"企业名称": "甲科技", "统一社会信用代码": _CODE, "法定代表人": "李某",
           "所属行业": "软件", "企业规模": "LARGE"}
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_import_suppliers_success():
    """合法行 → 建登录账号 + supplier + outbox + Neo4j + 冷数据唤醒。"""
    session = AsyncMock()
    session.scalars.side_effect = [_scalar_all([]), _scalar_all([]), _scalar_all([])]
    with patch("app.services.supplier_service.write_outbox_event", new=AsyncMock()) as outbox, \
         patch("app.services.supplier_service.neo4j_sync.upsert_supplier", new=AsyncMock()) as upsert:
        res = await import_suppliers(session, [_ok_row()], operator_id="U-1")
    assert res == {"imported": 1, "skipped": 0}
    assert outbox.await_count == 1
    assert upsert.await_count == 1
    session.commit.assert_awaited()  # 主事务 + 唤醒持久化


@pytest.mark.asyncio
async def test_import_suppliers_skips_duplicate_code():
    """库内信用代码已存在 → skipped（幂等重导）。"""
    session = AsyncMock()
    session.scalars.side_effect = [_scalar_all([_CODE]), _scalar_all([])]
    res = await import_suppliers(session, [_ok_row()], operator_id="U-1")
    assert res == {"imported": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_import_suppliers_validation_errors():
    """企业名缺失 / 信用代码非 18 位 → SupplierImportError。"""
    session = AsyncMock()
    session.scalars.side_effect = [_scalar_all([]), _scalar_all([])]
    rows = [_ok_row(企业名称=""), _ok_row(统一社会信用代码="123")]
    with pytest.raises(SupplierImportError) as ei:
        await import_suppliers(session, rows, operator_id="U-1")
    fields = {e["field"] for e in ei.value.errors}
    assert {"企业名称", "统一社会信用代码"} <= fields


@pytest.mark.asyncio
async def test_activate_pending_conflicts():
    """企查查冷数据唤醒：PENDING → ACTIVATED + 补写 Neo4j 回避关系。"""
    session = AsyncMock()
    p_share = MagicMock()
    p_share.expert_id = "EXP-1"
    p_share.relation_type = "股东"
    p_share.credit_code = _CODE
    p_share.company_name = "甲科技"
    p_emp = MagicMock()
    p_emp.expert_id = "EXP-2"
    p_emp.relation_type = "任职"
    session.scalars.return_value = _scalar_all([p_share, p_emp])
    supplier = MagicMock()
    supplier.supplier_id = "SUP-1"
    supplier.uniform_credit_code = _CODE
    supplier.name = "甲科技"

    with patch("app.services.supplier_service.neo4j_sync.upsert_conflict_relation", new=AsyncMock()) as upsert:
        activated = await _activate_pending_conflicts(session, supplier)
    assert len(activated) == 2
    assert p_share.status == "ACTIVATED"
    assert p_share.supplier_id == "SUP-1"
    assert upsert.await_count == 2


@pytest.mark.asyncio
async def test_update_status_blacklist_cascades():
    """拉黑 → INACTIVE + 黑名单级联 + outbox SUPPLIER_BLACKLISTED。"""
    session = AsyncMock()
    supplier = MagicMock()
    supplier.supplier_id = "SUP-1"
    supplier.name = "甲科技"
    supplier.blacklisted = False
    session.get.return_value = supplier
    session.scalar.return_value = None  # 供应商账号
    session.execute.return_value.all.return_value = []  # _notify_blacklist 受影响项目（无负责人）

    with patch("app.services.supplier_service._cascade_blacklist", new=AsyncMock()) as cascade, \
         patch("app.services.supplier_service.write_outbox_event", new=AsyncMock()) as outbox, \
         patch("app.services.supplier_service._notify_blacklist", new=AsyncMock()) as notify, \
         patch("app.services.supplier_service.neo4j_sync.upsert_supplier", new=AsyncMock()):
        done = await update_status(session, "SUP-1", blacklisted=True, status=None, operator_id="U-1")
    assert done.blacklisted is True
    assert done.status == "INACTIVE"
    cascade.assert_awaited_once_with(session, "SUP-1")
    outbox.assert_awaited_once()
    notify.assert_awaited_once_with(session, "SUP-1")


@pytest.mark.asyncio
async def test_update_status_unblacklist_restores():
    """解除拉黑 → ACTIVE + 评审还原。"""
    session = AsyncMock()
    supplier = MagicMock()
    supplier.supplier_id = "SUP-1"
    supplier.name = "甲科技"
    supplier.blacklisted = True
    session.get.return_value = supplier

    with patch("app.services.supplier_service._restore_suspended_reviews", new=AsyncMock()) as restore, \
         patch("app.services.supplier_service.neo4j_sync.upsert_supplier", new=AsyncMock()):
        done = await update_status(session, "SUP-1", blacklisted=False, status=None, operator_id="U-1")
    assert done.blacklisted is False
    assert done.status == "ACTIVE"
    restore.assert_awaited_once_with(session, "SUP-1")


@pytest.mark.asyncio
async def test_update_status_invalid_params():
    """status 与 blacklisted 都未传 / status 非法 → 422。"""
    session = AsyncMock()
    with pytest.raises(InvalidSupplierStatusError):
        await update_status(session, "SUP-1", blacklisted=None, status=None, operator_id="U-1")
    with pytest.raises(InvalidSupplierStatusError):
        await update_status(session, "SUP-1", blacklisted=None, status="FROZEN", operator_id="U-1")


@pytest.mark.asyncio
async def test_update_status_not_found():
    """供应商不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(SupplierNotFoundError):
        await update_status(session, "SUP-X", blacklisted=True, status=None, operator_id="U-1")


@pytest.mark.asyncio
async def test_resolve_me_success():
    """供应商账号唯一同名 → 解析主体。"""
    session = AsyncMock()
    supplier = MagicMock()
    supplier_result = _scalar_all([supplier])
    session.scalars.return_value = supplier_result
    user = MagicMock()
    user.role = Role.SUPPLIER
    user.display_name = "甲科技"
    assert await resolve_me(session, user) is supplier


@pytest.mark.asyncio
async def test_resolve_me_rejects_non_supplier():
    """非供应商角色 → 拒绝。"""
    session = AsyncMock()
    user = MagicMock()
    user.role = Role.PROJECT_MANAGER
    with pytest.raises(SupplierNotResolvableError):
        await resolve_me(session, user)


@pytest.mark.asyncio
async def test_resolve_me_ambiguous_or_none():
    """0 或多个同名 → 拒绝（避免静默选错主体）。"""
    session = AsyncMock()
    user = MagicMock()
    user.role = Role.SUPPLIER
    user.display_name = "甲科技"
    session.scalars.return_value = _scalar_all([])
    with pytest.raises(SupplierNotResolvableError):
        await resolve_me(session, user)
    session.scalars.return_value = _scalar_all([MagicMock(), MagicMock()])
    with pytest.raises(SupplierNotResolvableError):
        await resolve_me(session, user)


def _mk_result_row(bid_id, lot_status, bid_status="FROZEN", bid_amount=100):
    from types import SimpleNamespace

    from app.models.bid_document import BidStatus

    bid = SimpleNamespace(bid_id=bid_id, lot_id=f"LOT-{bid_id}", supplier_id="S1",
                          status=bid_status if bid_status else BidStatus.FROZEN,
                          bid_amount=bid_amount, created_at=None)
    lot = SimpleNamespace(lot_id=f"LOT-{bid_id}", lot_code=f"LC-{bid_id}",
                          name=f"标段{bid_id}", status=lot_status)
    proj = SimpleNamespace(project_id=f"PRJ-{bid_id}", name=f"项目{bid_id}")
    return (bid, lot, proj)


@pytest.mark.asyncio
async def test_list_my_results_three_states():
    """投标结果三态：废标 / 评审中 / 中标（复用评标汇总 rank）。"""
    session = AsyncMock()
    rows = [
        _mk_result_row("BID-B1", lot_status="UNDER_REVIEW", bid_status="DISQUALIFIED"),
        _mk_result_row("BID-B2", lot_status="UNDER_REVIEW"),
        _mk_result_row("BID-B3", lot_status="EVALUATED"),
    ]
    row_result = MagicMock()
    row_result.all.return_value = rows
    session.execute.return_value = row_result

    summary = {
        "bids": [
            {"bid_id": "BID-B3", "rank": 1, "supplier_name": "甲科技",
             "bid_amount": 100.0, "weighted_total": 90.0, "dimension_scores": []},
        ],
    }
    with patch("app.services.closeout_service.get_lot_summary", new=AsyncMock(return_value=summary)):
        from app.services.supplier_service import list_my_results
        items = await list_my_results(session, "S1")
    by_bid = {it["bid_id"]: it for it in items}
    assert by_bid["BID-B1"]["result_status"] == "DISQUALIFIED"
    assert by_bid["BID-B2"]["result_status"] == "UNDER_REVIEW"
    assert by_bid["BID-B3"]["result_status"] == "WINNER"
    assert by_bid["BID-B3"]["rank"] == 1
    assert by_bid["BID-B3"]["winner_supplier_name"] == "甲科技"
