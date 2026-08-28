"""P7.3 跨存储一致性集成测试（task.md 6 场景，可自动化的 5 项）。

1. MySQL Expert → outbox EXPERT_CREATED → consume → Neo4j 节点可见
2. conflict 导入 → Neo4j 关系（test_import_api 已覆盖双匹配+关系；此处补 outbox no-op）
4. outbox FAILED → reconcile 重放修复
5. 供应商拉黑 → 非 AWARDED 项目评审 SUSPENDED（级联 SQL）
6. 定标归档 → expert_profile 重算（review_count +1、review_quality 归一）
   （场景 3 标书解析→Milvus chunks 需真实解析流水线，归 P7.5 RAG 基准）
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from io import BytesIO

import openpyxl
import pytest
from sqlalchemy import text

from app.core.database import session_factory
from app.models.bid_document import BidDocument, BidStatus
from app.models.expert_review import ExpertReview, ReviewStatus
from app.models.project import Lot, Project, ScoringDimension
from app.services import outbox_consumer

EXPERT_HEADERS = ["编号", "姓名", "单位", "地区", "从业年限", "专业标签", "身份证号", "邮箱", "电话"]


def _expert_excel() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(EXPERT_HEADERS)
    ws.append(["ITEST-EXP-01", "集成测试一致性专家", "集成测试大学", "华东", 12,
               "软件开发", "310101199002020011", "c@x.com", "13900000001"])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _neo4j(cypher: str) -> list:
    from app.core import neo4j

    driver = neo4j.get_driver()
    async with driver.session() as s:
        return await (await s.run(cypher)).data()


async def _assign_lot_expert(lot_id: str, expert_id: str, dim_ids: list[str]) -> None:
    """前置专家-标段分配（create_review 归属校验必需，P4.2 分配数据直接 ORM 落库）。

    与 test_review_api.py 同款 helper：2537281 起 create_review 要求专家已分配
    标段且维度在负责维度内，否则 403。存量一致性测试缺此步，补上。
    """
    from app.models.lot_expert_assignment import LotExpertAssignment

    async with session_factory() as s:
        s.add(LotExpertAssignment(lot_id=lot_id, expert_id=expert_id, dimension_ids=dim_ids))
        await s.commit()


# ==================== 场景 1：Expert → outbox → Neo4j ====================


@pytest.mark.asyncio
async def test_expert_import_outbox_neo4j(client, admin_headers):
    """MySQL 写入 Expert → outbox EXPERT_CREATED → consume → Neo4j 节点可见。"""
    resp = await client.post("/api/v1/experts/import", headers=admin_headers, files={
        "file": ("e.xlsx", _expert_excel(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 201
    async with session_factory() as s:
        pending = (await s.execute(text(
            "SELECT COUNT(*) FROM outbox_event WHERE event_type='EXPERT_CREATED' AND status='PENDING'"
        ))).scalar()
    assert pending >= 1
    processed = await outbox_consumer.consume_pending_once()
    assert processed >= 1
    rows = await _neo4j("MATCH (e:Expert {expertId:'ITEST-EXP-01'}) RETURN e.name")
    assert rows, "Neo4j 应可见 Expert 节点（outbox 消费后）"
    assert rows[0]["e.name"] == "集成测试一致性专家"


# ==================== 场景 2：conflict 导入 outbox no-op ====================


@pytest.mark.asyncio
async def test_conflict_import_outbox_noop(client, admin_headers):
    """CONFLICT_IMPORTED 事件消费为 no-op（关系已直同步，worker 不重放）。"""
    from app.services import neo4j_sync

    await neo4j_sync.upsert_expert("ITEST-E1", name="集成测试专家甲", region="华东",
                                   experience=15, status="ACTIVE")
    await neo4j_sync.upsert_supplier("ITEST-S2", name="集成测试供应商乙",
                                     uniform_credit_code="913100001222222222", blacklisted=False)
    csv_bytes = io.StringIO()
    import csv
    w = csv.writer(csv_bytes)
    w.writerow(["姓名", "企业名称", "统一社会信用代码", "关系类型", "职位", "持股比例"])
    w.writerow(["集成测试专家甲", "集成测试供应商乙", "913100001222222222", "任职", "技术总监", ""])
    resp = await client.post("/api/v1/conflicts/import", headers=admin_headers,
                             files={"file": ("c.csv", csv_bytes.getvalue().encode(), "text/csv")})
    assert resp.status_code == 201
    assert resp.json()["matched"] == 1
    async with session_factory() as s:
        has_event = (await s.execute(text(
            "SELECT COUNT(*) FROM outbox_event WHERE event_type='CONFLICT_IMPORTED'"))).scalar()
    assert has_event >= 1
    # 消费不抛错（no-op），关系已直同步在 Neo4j
    await outbox_consumer.consume_pending_once()
    rows = await _neo4j("MATCH (e:Expert {expertId:'ITEST-E1'})-[r:EMPLOYED_BY]->"
                        "(sup:Supplier {supplierId:'ITEST-S2'}) RETURN r.role")
    assert rows and rows[0]["r.role"] == "技术总监"


# ==================== 场景 4：outbox FAILED → reconcile ====================


@pytest.mark.asyncio
async def test_outbox_failed_reconcile_repairs(client, admin_headers):
    """FAILED 事件 → reconcile 重放 → Neo4j 修复。"""
    async with session_factory() as s:
        await s.execute(text(
            "INSERT INTO outbox_event (aggregate_id, event_type, payload, status, retry_count) "
            "VALUES ('ITEST-E1', 'EXPERT_CREATED', '{}', 'FAILED', 1)"))
        await s.commit()
    repaired = await outbox_consumer.reconcile_failed()
    assert repaired >= 1
    async with session_factory() as s:
        st = (await s.execute(text(
            "SELECT status FROM outbox_event WHERE aggregate_id='ITEST-E1' AND event_type='EXPERT_CREATED'"
        ))).scalar()
    assert st == "PROCESSED"
    rows = await _neo4j("MATCH (e:Expert {expertId:'ITEST-E1'}) RETURN e.name")
    assert rows


# ==================== 场景 5：黑名单级联评审 SUSPENDED ====================


@pytest.mark.asyncio
async def test_blacklist_suspends_non_awarded_review(client, admin_headers, pm_headers, exp_headers,
                                                     lot_factory, bid_factory, set_bid_parsed):
    """拉黑 → 非 AWARDED 项目评审 SUSPENDED（AWARDED 项目不受影响由单元测试覆盖）。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids)
    r = await client.post(f"/api/v1/lots/{lot['lot_id']}/close-bidding", headers=pm_headers)
    assert r.json()["risk"] == "LOW"
    dims = (await client.get(f"/api/v1/lots/{lot['lot_id']}/dimensions", headers=pm_headers)).json()["items"]
    # 2537281 起 create_review 校验专家分配归属：先给 exp_headers（ITEST-E1）分配本标段
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [dims[0]["dimension_id"]])
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": dims[0]["dimension_id"]})
    review_id = r.json()["review_id"]
    assert r.status_code == 201
    r = await client.put("/api/v1/suppliers/ITEST-S1/status", headers=admin_headers,
                         json={"blacklisted": True})
    assert r.status_code == 200
    async with session_factory() as s:
        st = (await s.execute(text("SELECT status FROM expert_review WHERE review_id=:r"),
                              {"r": review_id})).scalar()
    assert st == "SUSPENDED"


# ==================== 场景 6：定标归档 → expert_profile 重算 ====================


@pytest.mark.asyncio
async def test_archive_recalc_expert_profile(client, admin_headers):
    """archive_project → 参与专家 total_reviews+1、review_quality 归一化。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_factory() as s:
        s.add_all([
            Project(project_id="ITEST-PRJA", project_code="ITEST-PRJA", name="归档项目",
                    type="SERVICE", region="华东", budget=1000000, status="AWARDED",
                    created_at=now, updated_at=now),
            Lot(lot_id="ITEST-LOTA", project_id="ITEST-PRJA", lot_code="ITEST-LOTA",
                name="归档标段", budget=500000, status="EVALUATED", created_at=now, updated_at=now),
            ScoringDimension(dimension_id="ITEST-DIMA", lot_id="ITEST-LOTA", name="技术",
                             max_score=30, weight=1.0, sort_order=1, created_at=now),
            BidDocument(bid_id="ITEST-BIDA", lot_id="ITEST-LOTA", supplier_id="ITEST-S1",
                        status=BidStatus.FROZEN, bid_amount=100, created_at=now, updated_at=now),
            ExpertReview(review_id="ITEST-REVA", expert_id="ITEST-E1", bid_id="ITEST-BIDA",
                         dimension_id="ITEST-DIMA", score=24, comment="归档评审",
                         status=ReviewStatus.CONFIRMED, created_at=now, updated_at=now),
        ])
        await s.commit()
    from app.tasks.archive import archive_project

    await archive_project({}, "ITEST-PRJA")
    async with session_factory() as s:
        row = (await s.execute(text(
            "SELECT total_reviews, review_quality FROM expert_profile WHERE expert_id='ITEST-E1'"
        ))).one()
    assert row.total_reviews == 1
    assert float(row.review_quality) == pytest.approx(24 / 30, abs=0.001)
