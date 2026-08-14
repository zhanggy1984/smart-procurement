"""供应商管理服务（P1.4）。

导入：Excel 行 → 校验（企业名必填、信用代码 18 位）→ 去重（uniform_credit_code）
→ 单事务写 users（登录账号）+ supplier → outbox SUPPLIER_CREATED → Neo4j 直同步
→ 触发 pending_conflict 冷数据唤醒（该供应商入库后补写回避关系）。

拉黑级联（task.md P1.4 / solution.md 1.2）：
- 未封存标书（freeze_hash IS NULL）→ DISQUALIFIED
- 非 AWARDED 项目下的评审 → SUSPENDED + previous_status 快照；AWARDED 项目不动
- 解除拉黑 → 评审按 previous_status 还原（标书状态不还原，废标不可逆）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.constants import QCC_RELATION_TO_NEO4J
from app.core.crypto import generate_id
from app.models.bid_document import BidDocument, BidStatus
from app.models.expert_review import ExpertReview, ReviewStatus
from app.models.outbox import OutboxEventType
from app.models.pending_conflict import PendingConflict, PendingConflictStatus
from app.models.project import Lot, Project, ScoringDimension
from app.models.supplier import Supplier, SupplierStatus
from app.models.user import Role, User
from app.services import neo4j_sync
from app.services.outbox import write_outbox_event

logger = structlog.get_logger(__name__)

# 导入初始密码（与合成数据一致）
INITIAL_PASSWORD = "Smart@2026"

# 定标终态（黑名单级联豁免口径：AWARDED 项目关联评审不变）
_AWARDED = "AWARDED"


class SupplierImportError(ValueError):
    """导入校验失败（errors: 行级错误列表）→ 422。"""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__(f"导入校验失败: {len(errors)} 处错误")


class SupplierNotFoundError(ValueError):
    """供应商不存在 → 404。"""


class InvalidSupplierStatusError(ValueError):
    """状态变更参数非法 → 422。"""


class SupplierNotResolvableError(ValueError):
    """当前登录账号无法唯一定位供应商主体（0 或多个同名）→ 422。"""


async def _sync_neo4j(name: str, coro) -> None:
    """执行 Neo4j 同步，失败仅告警（outbox 事件可兜底重放）。"""
    try:
        await coro
    except Exception as e:  # noqa: BLE001
        logger.warning("neo4j_sync_failed", operation=name, error=str(e))


def _validate_credit_code(code: str) -> bool:
    """统一社会信用代码：18 位数字（仿真数据不校验校验位）。"""
    return len(code) == 18 and code.isdigit()


async def _activate_pending_conflicts(session: AsyncSession, supplier: Supplier) -> list[PendingConflict]:
    """企查查冷数据唤醒：该供应商入库后，按信用代码/企业名匹配 PENDING 记录。

    匹配到的记录回填 supplier_id → ACTIVATED，并补写 Neo4j 回避关系
    （relation_type 中文经 QCC_RELATION_TO_NEO4J 映射）。
    """
    pending = (
        await session.scalars(
            select(PendingConflict).where(
                PendingConflict.status == PendingConflictStatus.PENDING,
                or_(
                    PendingConflict.credit_code == supplier.uniform_credit_code,
                    PendingConflict.company_name == supplier.name,
                ),
            )
        )
    ).all()
    for p in pending:
        p.supplier_id = supplier.supplier_id
        p.status = PendingConflictStatus.ACTIVATED
        if p.expert_id and p.relation_type in QCC_RELATION_TO_NEO4J:
            rel_type = QCC_RELATION_TO_NEO4J[p.relation_type]
            # 当前任职/持股快照：endDate 缺失表达当前（Neo4j null 属性不允许）
            props = {"role": "企查查导入", "startDate": None, "endDate": None} if rel_type == "EMPLOYED_BY" else {}
            await _sync_neo4j(
                "conflict_activated",
                neo4j_sync.upsert_conflict_relation(
                    rel_type,
                    expert_id=p.expert_id,
                    supplier_id=supplier.supplier_id,
                    **props,
                ),
            )
    return pending


async def import_suppliers(
    session: AsyncSession,
    rows: list[dict],
    *,
    operator_id: str,
) -> dict:
    """批量导入供应商（单事务）：建登录账号 + supplier + 冷数据唤醒。

    去重口径：uniform_credit_code 已存在 → skipped（幂等重导不报错）。
    返回 {"imported": n, "skipped": m}。
    """
    existing_codes = set(
        (await session.scalars(select(Supplier.uniform_credit_code).where(Supplier.uniform_credit_code.is_not(None)))).all()
    )
    used_usernames = set((await session.scalars(select(User.username))).all())

    errors: list[dict] = []
    suppliers: list[Supplier] = []
    new_users: list[User] = []
    skipped = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    seq = 1

    def _next_username() -> str:
        # 两位序号（supplier_01），与合成数据/前端约定一致
        nonlocal seq
        while f"supplier_{seq:02d}" in used_usernames:
            seq += 1
        username = f"supplier_{seq:02d}"
        used_usernames.add(username)
        seq += 1
        return username

    for idx, row in enumerate(rows, start=2):  # 第 1 行为表头
        name = (row.get("企业名称") or "").strip()
        credit_code = (row.get("统一社会信用代码") or "").strip()

        if not name:
            errors.append({"line": idx, "field": "企业名称", "message": "企业名称为必填"})
        if credit_code and not _validate_credit_code(credit_code):
            errors.append({"line": idx, "field": "统一社会信用代码", "message": "统一社会信用代码必须为 18 位数字"})
            continue
        if credit_code and credit_code in existing_codes:
            skipped += 1
            continue

        if any(e["line"] == idx for e in errors):
            continue

        # 编号可选：填了复用（迁移/验收对齐合成 ID），留空生成随机
        supplier_id = (row.get("编号") or "").strip() or generate_id("SUP")
        user = User(
            user_id=generate_id("U"),
            username=_next_username(),
            password_hash=security.hash_password(INITIAL_PASSWORD),
            role=Role.SUPPLIER,
            display_name=name,
            email=None,
            phone=None,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        new_users.append(user)
        suppliers.append(
            Supplier(
                supplier_id=supplier_id,
                name=name,
                uniform_credit_code=credit_code or None,
                legal_person=(row.get("法定代表人") or "").strip() or None,
                industry=(row.get("所属行业") or "").strip() or None,
                scale=(row.get("企业规模") or "").strip() or None,
                blacklisted=False,
                status=SupplierStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )

    if errors:
        raise SupplierImportError(errors)

    session.add_all(new_users)
    session.add_all(suppliers)
    for s in suppliers:
        await write_outbox_event(
            session,
            aggregate_id=s.supplier_id,
            event_type=OutboxEventType.SUPPLIER_CREATED,
            payload={"supplier_id": s.supplier_id, "name": s.name},
        )
    await session.commit()

    # 提交后：Neo4j 同步 + 冷数据唤醒（唤醒也是写 Neo4j，可与其他同步并行）
    activated_total = 0
    for s in suppliers:
        await _sync_neo4j(
            "upsert_supplier",
            neo4j_sync.upsert_supplier(
                s.supplier_id,
                name=s.name,
                uniform_credit_code=s.uniform_credit_code,
                legal_person=s.legal_person,
                industry=s.industry,
                scale=s.scale,
                blacklisted=s.blacklisted,
            ),
        )
        activated_total += len(await _activate_pending_conflicts(session, s))
    await session.commit()  # 持久化唤醒状态（supplier_id 回填 + ACTIVATED）

    logger.info(
        "suppliers_imported",
        imported=len(suppliers),
        skipped=skipped,
        pending_activated=activated_total,
        operator=operator_id,
    )
    return {"imported": len(suppliers), "skipped": skipped}


async def _cascade_blacklist(session: AsyncSession, supplier_id: str) -> None:
    """拉黑级联：未封存标书→DISQUALIFIED；非 AWARDED 项目评审→SUSPENDED+快照。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 1. 未封存标书废标（freeze_hash IS NULL = 未封存）
    await session.execute(
        update(BidDocument)
        .where(
            BidDocument.supplier_id == supplier_id,
            BidDocument.freeze_hash.is_(None),
            BidDocument.status != BidStatus.DISQUALIFIED,
        )
        .values(status=BidStatus.DISQUALIFIED, updated_at=now)
    )
    # 2. 非 AWARDED 项目下的评审 → SUSPENDED（previous_status 快照，供解除拉黑还原）
    bid_ids = (
        await session.scalars(select(BidDocument.bid_id).where(BidDocument.supplier_id == supplier_id))
    ).all()
    if bid_ids:
        awarded_bid = (
            select(Project.project_id)
            .join(Lot, Lot.project_id == Project.project_id)
            .join(BidDocument, BidDocument.lot_id == Lot.lot_id)
            .where(BidDocument.bid_id == ExpertReview.bid_id, Project.status == _AWARDED)
        ).exists()
        # 快照 + 置位分两次 UPDATE：MySQL 对 SET 右侧列引用的顺序语义在不同版本/限定符下
        # 不可靠（实测同语句内可能取到更新后的 SUSPENDED），两次独立 UPDATE 无歧义。
        await session.execute(
            update(ExpertReview)
            .where(
                ExpertReview.bid_id.in_(bid_ids),
                ExpertReview.status != ReviewStatus.SUSPENDED,
                ~awarded_bid,
            )
            .values(previous_status=ExpertReview.status, updated_at=now)
        )
        await session.execute(
            update(ExpertReview)
            .where(
                ExpertReview.bid_id.in_(bid_ids),
                ExpertReview.status != ReviewStatus.SUSPENDED,
                ~awarded_bid,
            )
            .values(status=ReviewStatus.SUSPENDED, updated_at=now)
        )


async def _restore_suspended_reviews(session: AsyncSession, supplier_id: str) -> None:
    """解除拉黑：SUSPENDED 评审按 previous_status 还原（标书 DISQUALIFIED 不还原）。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bid_ids = (
        await session.scalars(select(BidDocument.bid_id).where(BidDocument.supplier_id == supplier_id))
    ).all()
    if not bid_ids:
        return
    await session.execute(
        update(ExpertReview)
        .where(
            ExpertReview.bid_id.in_(bid_ids),
            ExpertReview.status == ReviewStatus.SUSPENDED,
            ExpertReview.previous_status.is_not(None),
        )
        .values(status=ExpertReview.previous_status, previous_status=None, updated_at=now)
    )


async def list_suppliers(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """管理端供应商列表（P7.4 补齐：拉黑管理 UI 的数据源）。"""
    stmt = select(Supplier).order_by(Supplier.created_at.desc())
    count_stmt = select(func.count()).select_from(Supplier)
    if keyword:
        like = f"%{keyword}%"
        cond = or_(Supplier.name.like(like), Supplier.uniform_credit_code.like(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = (await session.scalar(count_stmt)) or 0
    items = (await session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    return {
        "total": total,
        "items": [{
            "supplier_id": s.supplier_id, "name": s.name,
            "uniform_credit_code": s.uniform_credit_code, "legal_person": s.legal_person,
            "industry": s.industry, "scale": s.scale,
            "blacklisted": s.blacklisted, "status": s.status,
        } for s in items],
    }


async def _notify_blacklist(session: AsyncSession, supplier_id: str) -> None:
    """黑名单站内信（task.md P4.4 9 类通知之一）：通知受影响项目负责人（managed_by）。"""
    from app.services import notification_service as notification

    rows = (await session.execute(
        select(Project.managed_by, Project.project_id, Project.name)
        .join(Lot, Lot.project_id == Project.project_id)
        .join(BidDocument, BidDocument.lot_id == Lot.lot_id)
        .where(BidDocument.supplier_id == supplier_id, Project.managed_by.is_not(None))
    )).all()
    for managed_by, pid, pname in rows:
        await notification.send(
            session, user_id=managed_by, type="SUPPLIER_BLACKLISTED",
            title="供应商已拉黑",
            content=f"供应商 {supplier_id} 已被拉黑，关联项目 {pname} 的标书/评审已级联处理。",
            related_id=pid,
        )
    logger.info("supplier.blacklist_notify", supplier_id=supplier_id, targets=[r[0] for r in rows])


async def update_status(
    session: AsyncSession,
    supplier_id: str,
    *,
    blacklisted: Optional[bool],
    status: Optional[str],
    operator_id: str,
) -> Supplier:
    """拉黑/解除/停用/启用。拉黑触发黑名单级联，解除触发评审还原。"""
    if blacklisted is None and status is None:
        raise InvalidSupplierStatusError("status 与 blacklisted 至少传一个")
    if status is not None and status not in SupplierStatus.ALL:
        raise InvalidSupplierStatusError(f"status 非法: {status}，必须为 {SupplierStatus.ALL}")

    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(f"供应商不存在: {supplier_id}")

    # 目标状态解析：拉黑/解除由 blacklisted 主导，否则按 status
    if blacklisted is True:
        new_blacklisted, new_status = True, SupplierStatus.INACTIVE
    elif blacklisted is False:
        new_blacklisted, new_status = False, SupplierStatus.ACTIVE
    else:
        new_blacklisted, new_status = supplier.blacklisted, status

    was_blacklisted = supplier.blacklisted
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    supplier.blacklisted = new_blacklisted
    supplier.status = new_status
    supplier.updated_at = now

    # 登录账号同步禁用/启用（供应商账号按 username 前缀约定关联，仅禁用不查实体）
    if new_status == SupplierStatus.INACTIVE:
        user = await session.scalar(select(User).where(User.role == Role.SUPPLIER, User.display_name == supplier.name))
        if user is not None:
            user.is_active = False
            user.updated_at = now

    # 级联：拉黑触发、解除还原
    if new_blacklisted and not was_blacklisted:
        await _cascade_blacklist(session, supplier.supplier_id)
        await write_outbox_event(
            session,
            aggregate_id=supplier.supplier_id,
            event_type=OutboxEventType.SUPPLIER_BLACKLISTED,
            payload={"supplier_id": supplier.supplier_id, "name": supplier.name},
        )
        await _notify_blacklist(session, supplier.supplier_id)
    elif not new_blacklisted and was_blacklisted:
        await _restore_suspended_reviews(session, supplier.supplier_id)

    await session.commit()
    await session.refresh(supplier)
    await _sync_neo4j(
        "upsert_supplier_status",
        neo4j_sync.upsert_supplier(
            supplier.supplier_id,
            name=supplier.name,
            uniform_credit_code=supplier.uniform_credit_code,
            legal_person=supplier.legal_person,
            industry=supplier.industry,
            scale=supplier.scale,
            blacklisted=supplier.blacklisted,
        ),
    )
    logger.info("supplier_status_updated", supplier_id=supplier_id, blacklisted=new_blacklisted, status=new_status, operator=operator_id)
    return supplier


# ============ 供应商端自助接口（P6.5） ============


async def resolve_me(session: AsyncSession, user: User) -> Supplier:
    """解析当前登录供应商主体（users.display_name = supplier.name 约定关联）。

    与投标绑定口径一致（P0.4 DDL 无 supplier.user_id 列）。合成数据存在同名
    供应商（如 SUP-009/SUP-013），0 或 2+ 个匹配时抛异常提示联系管理员，
    避免静默选错主体。
    """
    if user.role != Role.SUPPLIER:
        raise SupplierNotResolvableError("仅供应商账号可访问本接口")
    candidates = (
        await session.scalars(select(Supplier).where(Supplier.name == user.display_name))
    ).all()
    if len(candidates) != 1:
        raise SupplierNotResolvableError(
            "当前账号无法唯一定位供应商主体" if not candidates else "当前账号对应多个供应商（重名），请联系管理员"
        )
    return candidates[0]


async def list_market(
    session: AsyncSession,
    supplier_id: str,
    *,
    project_type: Optional[str] = None,
    region: Optional[str] = None,
    budget_min: Optional[float] = None,
    budget_max: Optional[float] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int, dict]:
    """招标市场（P6.5）：可投标标段（BIDDING）+ 项目信息 + 维度摘要 + 已投标标记。

    筛选：项目类型 / 地区 / 预算区间（lot.budget）。返回 (items, total, filters)，
    filters 为全部筛选项去重值（前端下拉用，不受本次筛选影响）。
    """
    from sqlalchemy import func

    base = (
        select(Lot)
        .join(Project, Lot.project_id == Project.project_id)
        .where(Lot.status == "BIDDING")
    )
    if project_type:
        base = base.where(Project.type == project_type)
    if region:
        base = base.where(Project.region == region)
    if budget_min is not None:
        base = base.where(Lot.budget >= budget_min)
    if budget_max is not None:
        base = base.where(Lot.budget <= budget_max)

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(Lot.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    my_lots = set(
        (await session.scalars(select(BidDocument.lot_id).where(BidDocument.supplier_id == supplier_id))).all()
    )
    lot_ids = [l.lot_id for l in rows]
    proj_map: dict[str, Project] = {}
    dim_map: dict[str, list[dict]] = {}
    bid_count_map: dict[str, int] = {}
    if lot_ids:
        proj_map = {
            p.project_id: p
            for p in (await session.execute(select(Project).where(Project.project_id.in_({l.project_id for l in rows})))).scalars()
        }
        dim_rows = (
            await session.execute(
                select(ScoringDimension)
                .where(ScoringDimension.lot_id.in_(lot_ids))
                .order_by(ScoringDimension.lot_id, ScoringDimension.sort_order)
            )
        ).scalars()
        for d in dim_rows:
            dim_map.setdefault(d.lot_id, []).append(
                {
                    "dimension_id": d.dimension_id,
                    "name": d.name,
                    "max_score": float(d.max_score) if d.max_score is not None else None,
                    "weight": float(d.weight) if d.weight is not None else None,
                }
            )
        bid_count_map = dict(
            (
                await session.execute(
                    select(BidDocument.lot_id, func.count())
                    .where(
                        BidDocument.lot_id.in_(lot_ids),
                        BidDocument.status != BidStatus.DISQUALIFIED,
                    )
                    .group_by(BidDocument.lot_id)
                )
            ).all()
        )

    def _fmt(v):
        return float(v) if v is not None else None

    items = []
    for l in rows:
        p = proj_map.get(l.project_id)
        items.append(
            {
                "lot_id": l.lot_id,
                "lot_code": l.lot_code,
                "name": l.name,
                "budget": _fmt(l.budget),
                "status": l.status,
                "project_id": l.project_id,
                "project_code": p.project_code if p else None,
                "project_name": p.name if p else None,
                "type": p.type if p else None,
                "region": p.region if p else None,
                "dimensions": dim_map.get(l.lot_id, []),
                "bid_count": bid_count_map.get(l.lot_id, 0),
                "has_bid": l.lot_id in my_lots,
            }
        )

    filters = {
        "types": list((await session.execute(select(Project.type).distinct())).scalars().all()),
        "regions": list(
            (
                await session.execute(
                    select(Project.region).distinct().where(Project.region.is_not(None))
                )
            ).scalars().all()
        ),
    }
    logger.info("supplier.market_listed", supplier_id=supplier_id, total=total)
    return items, total, filters


async def list_my_bids(
    session: AsyncSession,
    supplier_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """我的投标（P6.5）：当前供应商全部标书 + 标段/项目信息 + 解析状态。"""
    from sqlalchemy import func

    base = (
        select(BidDocument, Lot, Project)
        .join(Lot, BidDocument.lot_id == Lot.lot_id)
        .join(Project, Lot.project_id == Project.project_id)
        .where(BidDocument.supplier_id == supplier_id)
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(BidDocument.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = []
    for bid, lot, proj in rows:
        items.append(
            {
                "bid_id": bid.bid_id,
                "lot_id": bid.lot_id,
                "lot_code": lot.lot_code,
                "lot_name": lot.name,
                "lot_status": lot.status,
                "project_id": proj.project_id,
                "project_name": proj.name,
                "bid_amount": float(bid.bid_amount) if bid.bid_amount is not None else None,
                "duration": bid.duration,
                "team_size": bid.team_size,
                "status": bid.status,
                "parsing_step": bid.parsing_step or 0,
                "created_at": bid.created_at.isoformat() if bid.created_at else None,
            }
        )
    logger.info("supplier.bids_listed", supplier_id=supplier_id, total=total)
    return items, total


async def get_my_bid_detail(session: AsyncSession, supplier_id: str, bid_id: str) -> dict:
    """我的标书详情（P6.5）：结构化信息 + 解析状态 + 文件访问地址。

    归属校验：非本供应商标书按不存在处理（不泄露他人标书存在性）。
    """
    # 局部 import 避免与 bid_document_service 潜在循环依赖
    from app.services.bid_document_service import BidNotFoundError, get_bid

    bid = await session.get(BidDocument, bid_id)
    if bid is None or bid.supplier_id != supplier_id:
        raise BidNotFoundError(f"标书不存在: {bid_id}")
    _, signed = await get_bid(session, bid_id)
    lot = await session.get(Lot, bid.lot_id)
    proj = await session.get(Project, lot.project_id) if lot else None
    logger.info("supplier.bid_detail", supplier_id=supplier_id, bid_id=bid_id)
    return {
        "bid_id": bid.bid_id,
        "lot_id": bid.lot_id,
        "lot_code": lot.lot_code if lot else None,
        "lot_name": lot.name if lot else None,
        "lot_status": lot.status if lot else None,
        "project_id": proj.project_id if proj else None,
        "project_name": proj.name if proj else None,
        "bid_amount": float(bid.bid_amount) if bid.bid_amount is not None else None,
        "duration": bid.duration,
        "team_size": bid.team_size,
        "status": bid.status,
        "parsing_step": bid.parsing_step or 0,
        "structured_data": bid.structured_data,
        "file_url": bid.file_url,
        "presigned_url": signed,
        "created_at": bid.created_at.isoformat() if bid.created_at else None,
    }


async def list_my_results(session: AsyncSession, supplier_id: str) -> list[dict]:
    """投标结果（P6.5）：三态判定 + 结果详情数据。

    - lot 终态（EVALUATED/AWARDED）：复用评标汇总 rank——rank=1 已中标(WINNER)，>1 未中标(LOSER)
    - lot 非终态：评审中(UNDER_REVIEW，投标已提交、评审未出结果)
    - 标书 DISQUALIFIED（黑名单级联废标）：废标
    结果详情含排名、各维度得分、中标方信息（结果详情页数据源）。
    """
    # 局部 import 避免与 closeout_service 潜在循环依赖
    from app.services import closeout_service as closeout

    rows = (
        await session.execute(
            select(BidDocument, Lot, Project)
            .join(Lot, BidDocument.lot_id == Lot.lot_id)
            .join(Project, Lot.project_id == Project.project_id)
            .where(BidDocument.supplier_id == supplier_id)
            .order_by(Lot.status, BidDocument.created_at.desc())
        )
    ).all()
    items = []
    for bid, lot, proj in rows:
        item = {
            "bid_id": bid.bid_id,
            "lot_id": bid.lot_id,
            "lot_code": lot.lot_code,
            "lot_name": lot.name,
            "project_id": proj.project_id,
            "project_name": proj.name,
            "lot_status": lot.status,
            "bid_amount": float(bid.bid_amount) if bid.bid_amount is not None else None,
            "result_status": None,
            "rank": None,
            "weighted_total": None,
            "dimension_scores": [],
            "winner_supplier_name": None,
            "winner_bid_amount": None,
            "winner_weighted_total": None,
        }
        if bid.status == BidStatus.DISQUALIFIED:
            item["result_status"] = "DISQUALIFIED"
            items.append(item)
            continue
        if lot.status not in ("EVALUATED", "AWARDED"):
            item["result_status"] = "UNDER_REVIEW"
            items.append(item)
            continue
        try:
            summary = await closeout.get_lot_summary(session, lot_id=lot.lot_id)
        except Exception as e:  # noqa: BLE001  汇总异常（无 FROZEN 标书等）按评审中兜底
            logger.warning("supplier.result_summary_fail", lot_id=lot.lot_id, error=str(e))
            item["result_status"] = "UNDER_REVIEW"
            items.append(item)
            continue
        entry = next((x for x in summary["bids"] if x["bid_id"] == bid.bid_id), None)
        winner = min(summary["bids"], key=lambda x: x["rank"]) if summary["bids"] else None
        if entry is None:
            # 本标书未进汇总（非 FROZEN 状态）→ 按评审中兜底，避免误判
            item["result_status"] = "UNDER_REVIEW"
        else:
            item["rank"] = entry["rank"]
            item["weighted_total"] = entry["weighted_total"]
            item["dimension_scores"] = entry["dimension_scores"]
            item["result_status"] = "WINNER" if entry["rank"] == 1 else "LOSER"
        if winner is not None:
            item["winner_supplier_name"] = winner["supplier_name"]
            item["winner_bid_amount"] = winner["bid_amount"]
            item["winner_weighted_total"] = winner["weighted_total"]
        items.append(item)
    logger.info("supplier.results_listed", supplier_id=supplier_id, count=len(items))
    return items
