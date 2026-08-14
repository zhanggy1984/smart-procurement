"""专家管理服务（P1.4）。

导入：Excel 行 → 逐行校验（region/tag 受控、身份证格式）→ 错误收集 →
单事务写 users（登录账号）+ expert + expert_specialization → outbox EXPERT_CREATED
→ Neo4j 直同步（失败仅告警，outbox 兜底）。

状态：PUT status 三态（ACTIVE/INACTIVE/BLACKLISTED）+ DELETE 逻辑删除→INACTIVE，
非 ACTIVE 状态同步禁用登录账号（users.is_active=False）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.constants import EXPERT_TAGS, REGIONS
from app.core.crypto import encrypt_id_number, generate_id, hash_id_number
from app.models.expert import Expert, ExpertSpecialization, ExpertStatus
from app.models.outbox import OutboxEventType
from app.models.user import Role, User
from app.services import neo4j_sync
from app.services.outbox import write_outbox_event

logger = structlog.get_logger(__name__)

# 导入初始密码（与合成数据一致，满足复杂度：≥8 位 + 大小写 + 数字）
INITIAL_PASSWORD = "Smart@2026"


class ExpertImportError(ValueError):
    """导入校验失败（errors: 行级错误列表）→ 422。"""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__(f"导入校验失败: {len(errors)} 处错误")


class ExpertNotFoundError(ValueError):
    """专家不存在 → 404。"""


class InvalidExpertStatusError(ValueError):
    """状态非法 → 422。"""


async def _sync_neo4j(name: str, coro) -> None:
    """执行 Neo4j 同步，失败仅告警（outbox 事件可兜底重放）。"""
    try:
        await coro
    except Exception as e:  # noqa: BLE001  Neo4j 短暂不可用不应阻断 MySQL 主链路
        logger.warning("neo4j_sync_failed", operation=name, error=str(e))


def _validate_id_number(id_number: str) -> bool:
    """身份证号校验：15 位纯数字，或 18 位（17 位数字 + 末位数字/X 校验码）。

    faker 生成的合法身份证末位校验码可能为 X（非数字），isdigit() 会误判。
    """
    if len(id_number) == 15:
        return id_number.isdigit()
    if len(id_number) == 18:
        return id_number[:-1].isdigit() and (id_number[-1].isdigit() or id_number[-1] in ("X", "x"))
    return False


async def import_experts(
    session: AsyncSession,
    rows: list[dict],
    *,
    operator_id: str,
) -> dict:
    """批量导入专家（单事务）：建登录账号 + expert + 标签。

    rows 为 importer.parse_expert_excel 输出。校验失败抛 ExpertImportError
    （errors 带行号与字段）；返回 {"imported": n, "skipped": m}。
    去重口径：id_number_hash 已存在 → skipped（幂等重导不报错）。
    """
    # 预查库内身份证哈希 + 已用用户名，避免逐行查询
    existing_hashes = set(
        (await session.scalars(select(Expert.id_number_hash).where(Expert.id_number_hash.is_not(None)))).all()
    )
    used_usernames = set((await session.scalars(select(User.username))).all())

    errors: list[dict] = []
    experts: list[Expert] = []
    spec_rows: list[ExpertSpecialization] = []
    new_users: list[User] = []
    skipped = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 新增计数 → 登录用户名（expert_{seq:04d}，跳过库内已用序号）
    seq = 1

    def _next_username() -> str:
        # 两位序号（expert_01），与合成数据/前端约定一致（P6 登录错位会踩坑）
        nonlocal seq
        while f"expert_{seq:02d}" in used_usernames:
            seq += 1
        username = f"expert_{seq:02d}"
        used_usernames.add(username)
        seq += 1
        return username

    for idx, row in enumerate(rows, start=2):  # 第 1 行为表头
        name = (row.get("姓名") or "").strip()
        id_number = (row.get("身份证号") or "").strip()

        # ---- 基础校验（必填 + 格式 + 去重） ----
        if not name:
            errors.append({"line": idx, "field": "姓名", "message": "姓名为必填"})
        if not _validate_id_number(id_number):
            errors.append({"line": idx, "field": "身份证号", "message": "身份证号必须为 15/18 位数字"})
            continue
        if hash_id_number(id_number) in existing_hashes:
            skipped += 1
            continue

        # ---- 受控值校验（region / tag） ----
        region = (row.get("地区") or "").strip()
        if region and region not in REGIONS:
            errors.append({"line": idx, "field": "地区", "message": f"地区非法，必须是受控值: {REGIONS}"})
        tags = [t.strip() for t in (row.get("专业标签") or "").replace("；", ";").split(";") if t.strip()]
        invalid_tags = [t for t in tags if t not in EXPERT_TAGS]
        if invalid_tags:
            errors.append({"line": idx, "field": "专业标签", "message": f"标签非法: {invalid_tags}，必须来自受控词表"})

        # ---- 从业年限 ----
        experience: Optional[int] = None
        exp_str = (row.get("从业年限") or "").strip()
        if exp_str:
            try:
                experience = int(exp_str)
            except ValueError:
                errors.append({"line": idx, "field": "从业年限", "message": "从业年限必须为整数"})
                continue

        if any(e["line"] == idx for e in errors):
            continue  # 该行已有错误，不再入队

        # ---- 组装入库对象（不落库，最后统一提交） ----
        # 编号可选：填了复用（迁移/验收对齐合成 ID），留空生成随机
        expert_id = (row.get("编号") or "").strip() or generate_id("EXP")
        email = (row.get("邮箱") or "").strip() or None
        phone = (row.get("电话") or "").strip() or None
        user = User(
            user_id=generate_id("U"),
            username=_next_username(),
            password_hash=security.hash_password(INITIAL_PASSWORD),
            role=Role.REVIEW_EXPERT,
            display_name=name,
            email=email,
            phone=phone,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        new_users.append(user)
        experts.append(
            Expert(
                expert_id=expert_id,
                user_id=user.user_id,
                name=name,
                organization=(row.get("单位") or "").strip() or None,
                region=region or None,
                experience=experience,
                email=email,
                phone=phone,
                id_number_encrypted=encrypt_id_number(id_number),
                id_number_hash=hash_id_number(id_number),
                status=ExpertStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        for tag in tags:
            spec_rows.append(ExpertSpecialization(expert_id=expert_id, tag=tag))

    if errors:
        raise ExpertImportError(errors)

    # ---- 单事务写入 ----
    session.add_all(new_users)
    session.add_all(experts)
    session.add_all(spec_rows)
    for e in experts:
        await write_outbox_event(
            session,
            aggregate_id=e.expert_id,
            event_type=OutboxEventType.EXPERT_CREATED,
            payload={"expert_id": e.expert_id, "name": e.name},
        )
    await session.commit()

    # ---- Neo4j 直同步（commit 后，失败仅告警） ----
    for e in experts:
        await _sync_neo4j(
            "upsert_expert",
            neo4j_sync.upsert_expert(
                e.expert_id,
                name=e.name,
                organization=e.organization,
                region=e.region,
                experience=e.experience,
                status=e.status,
            ),
        )
    logger.info(
        "experts_imported",
        imported=len(experts),
        skipped=skipped,
        operator=operator_id,
    )
    return {"imported": len(experts), "skipped": skipped}


async def update_status(
    session: AsyncSession,
    expert_id: str,
    new_status: str,
    *,
    operator_id: str,
) -> Expert:
    """启用/停用/拉黑。INACTIVE/BLACKLISTED 同步禁用登录账号。"""
    if new_status not in ExpertStatus.ALL:
        raise InvalidExpertStatusError(f"状态非法: {new_status}，必须为 {ExpertStatus.ALL}")

    expert = await session.get(Expert, expert_id)
    if expert is None:
        raise ExpertNotFoundError(f"专家不存在: {expert_id}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expert.status = new_status
    expert.updated_at = now

    # 同步登录账号启用状态：仅 ACTIVE 可登录
    if expert.user_id:
        user = await session.get(User, expert.user_id)
        if user is not None:
            user.is_active = new_status == ExpertStatus.ACTIVE
            user.updated_at = now

    await session.commit()
    await session.refresh(expert)
    await _sync_neo4j(
        "upsert_expert_status",
        neo4j_sync.upsert_expert(
            expert.expert_id,
            name=expert.name,
            organization=expert.organization,
            region=expert.region,
            experience=expert.experience,
            status=expert.status,
        ),
    )
    logger.info("expert_status_updated", expert_id=expert_id, status=new_status, operator=operator_id)
    return expert


async def list_experts(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """管理端专家列表（分页 + 关键词，P6.2 补：专家管理页数据源）。"""
    stmt = select(Expert).order_by(Expert.created_at.desc())
    count_stmt = select(func.count()).select_from(Expert)
    if keyword:
        like = f"%{keyword}%"
        cond = or_(Expert.name.like(like), Expert.organization.like(like), Expert.region.like(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = (await session.scalar(count_stmt)) or 0
    items = (await session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    # 专业标签批量查询，避免逐专家 N+1
    tags: dict[str, list[str]] = {}
    if items:
        rows = (
            await session.scalars(
                select(ExpertSpecialization).where(
                    ExpertSpecialization.expert_id.in_([e.expert_id for e in items])
                )
            )
        ).all()
        for r in rows:
            tags.setdefault(r.expert_id, []).append(r.tag)
    return {
        "total": total,
        "items": [
            {
                "expert_id": e.expert_id,
                "name": e.name,
                "organization": e.organization,
                "region": e.region,
                "experience": e.experience,
                "tags": tags.get(e.expert_id, []),
                "status": e.status,
                "created_at": e.created_at,
            }
            for e in items
        ],
    }


async def delete_expert(
    session: AsyncSession,
    expert_id: str,
    *,
    operator_id: str,
) -> Expert:
    """逻辑删除 → INACTIVE + 禁用登录账号（不物理删除，保留审计链路）。"""
    return await update_status(session, expert_id, ExpertStatus.INACTIVE, operator_id=operator_id)
