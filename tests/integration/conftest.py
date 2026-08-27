"""P7.3 集成测试基础设施。

连本地 Docker Compose 中间件（MySQL/Redis/Neo4j/Milvus/MinIO 均已 healthy），
MySQL 使用独立 test schema（smart_procurement_test，根 conftest 已切换），
业务数据一律用 `ITEST` 前缀，Neo4j 节点 teardown 清理，不污染 P7.1 演示数据。

基础设施：
- session 级 `_prepare_test_db`：建 test 库（幂等）→ alembic upgrade head 建 22 表
- function 级 `_reset_state`：TRUNCATE 业务表 → seed 最小数据 → 测试后清理 Neo4j ITEST 节点
- `client` + `*_headers`：httpx ASGITransport 进程内调用 app.main（用最新代码，
  不依赖运行中的 sp-app 容器）
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get("MYSQL_DATABASE", "smart_procurement_test")
DB_URL = os.environ["MYSQL_URL"]

_ROOT_URL = "mysql+asyncmy://root:root_infra_pass@localhost:33061"

# 业务表（不含 alembic_version）。DDL 为逻辑外键、无 DB 约束，TRUNCATE 顺序无关。
BUSINESS_TABLES = [
    "award_result", "audit_log", "bid_document", "conversation_message",
    "dimension_calibration", "expert", "expert_conflict_declaration",
    "expert_profile", "expert_review", "expert_specialization", "lot",
    "lot_expert_assignment", "lot_expert_criteria", "notification",
    "outbox_event", "pending_conflict", "project", "scoring_criterion",
    "scoring_dimension", "supplier", "system_config", "users",
]

PASSWORD = "Smart@2026"

# 测试数据前缀（Neo4j 共享中间件无法隔离，靠前缀 + teardown 清理）
PREFIX = "ITEST"


def pytest_collection_modifyitems(items):
    """集成测试统一 session event loop（pytest-asyncio 0.24 仅 ini 支持
    fixture loop scope，测试函数 loop 需此 hook 控制）。

    SQLAlchemy engine / Redis / Neo4j / arq 连接池是模块级单例，绑定 event
    loop。测试函数与 fixture 共享 session loop，单例跨测试复用才稳定；否则
    报 'NoneType send' / Future attached to a different loop。
    """
    for item in items:
        if item.get_closest_marker("asyncio") is not None:
            item.add_marker(pytest.mark.asyncio(loop_scope="session"))


# ==================== 建库 + 建表（session 级） ====================


async def _ensure_test_db() -> None:
    """幂等创建 test 库并授权 smart 用户。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(_ROOT_URL, pool_pre_ping=True)
    try:
        async with eng.begin() as conn:
            await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{TEST_DB}` CHARACTER SET utf8mb4"))
            await conn.execute(text(f"GRANT ALL PRIVILEGES ON `{TEST_DB}`.* TO 'smart'@'%'"))
            await conn.execute(text("FLUSH PRIVILEGES"))
    finally:
        await eng.dispose()


def _run_alembic() -> None:
    """用指向 test schema 的 MYSQL_URL 跑 alembic upgrade head（已升级则 no-op）。"""
    env = {**os.environ, "MYSQL_URL": DB_URL}
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"alembic upgrade head 失败:\n{res.stdout}\n{res.stderr}")


@pytest.fixture(scope="session", autouse=True)
async def _prepare_test_db():
    """建 test 库（幂等）+ alembic 建 22 张表。仅一次/会话。"""
    await _ensure_test_db()
    _run_alembic()


# ==================== 清表 + seed（function 级） ====================


async def _truncate_all() -> None:
    from sqlalchemy import text

    from app.core.database import session_factory

    async with session_factory() as s:
        await s.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in BUSINESS_TABLES:
            await s.execute(text(f"TRUNCATE TABLE `{t}`"))
        await s.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        await s.commit()


async def _seed_base() -> None:
    """seed 最小用户 + 专家/供应商实体（ITEST 前缀，display_name 与实体 name 对齐）。"""
    from app.core import security
    from app.core.database import session_factory
    from app.models.expert import Expert, ExpertSpecialization
    from app.models.supplier import Supplier
    from app.models.user import Role, User

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    h = security.hash_password(PASSWORD)

    def _u(uid: str, username: str, role: str, display: str) -> User:
        return User(user_id=uid, username=username, password_hash=h, role=role,
                    display_name=display, email=f"{username}@itest.local",
                    is_active=True, created_at=now, updated_at=now)

    users = [
        _u("ITEST-U-ADMIN", "admin", Role.ADMIN, "系统管理员"),
        _u("ITEST-U-PM", "pm1", Role.PROJECT_MANAGER, "项目经理甲"),
        _u("ITEST-U-E1", "exp1", Role.REVIEW_EXPERT, "集成测试专家甲"),
        _u("ITEST-U-E2", "exp2", Role.REVIEW_EXPERT, "集成测试专家乙"),
        _u("ITEST-U-S1", "sup1", Role.SUPPLIER, "集成测试供应商甲"),
        _u("ITEST-U-S2", "sup2", Role.SUPPLIER, "集成测试供应商乙"),
        _u("ITEST-U-S3", "sup3", Role.SUPPLIER, "集成测试供应商丙"),
    ]

    def _e(eid: str, name: str, region: str, exp: int, tags: list[str], uid: str | None) -> tuple[Expert, list[ExpertSpecialization]]:
        expert = Expert(expert_id=eid, user_id=uid, name=name, organization="集成测试大学",
                        region=region, experience=exp, id_number_hash="",
                        status="ACTIVE", created_at=now, updated_at=now)
        return expert, [ExpertSpecialization(expert_id=eid, tag=t) for t in tags]

    experts: list[Expert] = []
    specs: list[ExpertSpecialization] = []
    for eid, name, region, exp, tags, uid in [
        # 全部含"软件开发"公共标签：match 以该标签过滤候选时 5 人全部命中（≥ expert_count）
        ("ITEST-E1", "集成测试专家甲", "华东", 15, ["软件开发", "人工智能"], "ITEST-U-E1"),
        ("ITEST-E2", "集成测试专家乙", "华东", 12, ["软件开发", "网络安全"], "ITEST-U-E2"),
        ("ITEST-E3", "集成测试专家丙", "华东", 10, ["软件开发", "大数据"], None),
        ("ITEST-E4", "集成测试专家丁", "华东", 8, ["软件开发", "系统集成"], None),
        ("ITEST-E5", "集成测试专家戊", "华东", 6, ["软件开发", "物联网"], None),
    ]:
        e, ss = _e(eid, name, region, exp, tags, uid)
        experts.append(e)
        specs.extend(ss)

    suppliers = [
        Supplier(supplier_id="ITEST-S1", name="集成测试供应商甲",
                 uniform_credit_code="913100001111111111", legal_person="法人甲",
                 industry="软件开发", scale="LARGE", blacklisted=False,
                 status="ACTIVE", created_at=now, updated_at=now),
        Supplier(supplier_id="ITEST-S2", name="集成测试供应商乙",
                 uniform_credit_code="913100001222222222", legal_person="法人乙",
                 industry="软件", scale="MEDIUM", blacklisted=False,
                 status="ACTIVE", created_at=now, updated_at=now),
        Supplier(supplier_id="ITEST-S3", name="集成测试供应商丙",
                 uniform_credit_code="913100001333333333", legal_person="法人丙",
                 industry="软件", scale="SMALL", blacklisted=False,
                 status="ACTIVE", created_at=now, updated_at=now),
    ]

    async with session_factory() as s:
        s.add_all(users + experts + specs + suppliers)
        await s.commit()


async def _cleanup_neo4j() -> None:
    """删除 ITEST 前缀节点 + 释放 driver。

    pytest-asyncio 默认 function-level event loop：Neo4j AsyncDriver 是模块级
    单例，跨 loop 复用会崩（'NoneType' object has no attribute 'send'）。每个
    测试 teardown 释放 driver，下个测试重新创建绑定自己的 loop。
    """
    from app.core import neo4j

    try:
        driver = neo4j.get_driver()
        async with driver.session() as s:
            await s.run(
                "MATCH (n) WHERE n.expertId STARTS WITH 'ITEST' OR n.supplierId STARTS WITH 'ITEST' "
                "OR n.projectId STARTS WITH 'ITEST' OR n.lotId STARTS WITH 'ITEST' "
                "OR n.dimensionId STARTS WITH 'ITEST' OR n.bidId STARTS WITH 'ITEST' "
                "DETACH DELETE n"
            )
    except Exception:  # noqa: BLE001  Neo4j 故障不阻断测试结果
        pass
    finally:
        try:
            await neo4j.close_driver()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(autouse=True)
async def _reset_state():
    """每个测试独立：重置单例连接 → 清空业务表 → seed → 测试后清理 Neo4j 节点。

    Redis/arq pool 是模块级单例（session loop 下绑定 session loop），但底层
    连接跨测试复用可能损坏（'Event loop is closed'）。每个测试前重置单例，
    模拟"每测试全新连接"，避免前序测试遗留连接污染。
    """
    import app.api.v1.reviews as reviews_mod
    import app.tasks.dispatch as dispatch_mod

    reviews_mod._idem_pool = None
    dispatch_mod._pool = None
    await _truncate_all()
    await _seed_base()
    yield
    await _cleanup_neo4j()


# ==================== HTTP client + 鉴权 headers ====================


@pytest.fixture
async def client():
    """httpx AsyncClient 进程内调用 app.main（ASGITransport，不触发 lifespan）。

    不依赖运行中的 sp-app 容器——用当前代码 + 本地 Docker 中间件。
    """
    import httpx

    from app.main import app

    # raise_app_exceptions=False 对齐真实 ASGI 服务器：P8 全局 handler 发送
    # 500/503 响应后 Starlette ServerErrorMiddleware 总会 re-raise（uvicorn 仅记
    # 日志、响应已送达客户端）；httpx 默认 True 会把 re-raise 抛给测试而非返回响应。
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        yield c


def _bearer(user_id: str) -> dict:
    from app.core import security

    return {"Authorization": f"Bearer {security.create_access_token(user_id)}"}


@pytest.fixture
def admin_headers() -> dict:
    return _bearer("ITEST-U-ADMIN")


@pytest.fixture
def pm_headers() -> dict:
    return _bearer("ITEST-U-PM")


@pytest.fixture
def exp_headers() -> dict:
    return _bearer("ITEST-U-E1")


@pytest.fixture
def exp2_headers() -> dict:
    return _bearer("ITEST-U-E2")


@pytest.fixture
def sup_headers() -> dict:
    return _bearer("ITEST-U-S1")


@pytest.fixture
def sup2_headers() -> dict:
    return _bearer("ITEST-U-S2")


@pytest.fixture
def sup3_headers() -> dict:
    return _bearer("ITEST-U-S3")


# ==================== 数据工厂（多文件复用） ====================


def _dims_ok() -> list[dict]:
    """标准 5 维度，权重和 = 1.0（报价维度名固定，评审走纯公式）。"""
    return [
        {"name": "报价", "max_score": "20", "weight": "0.2", "criteria": []},
        {"name": "技术", "max_score": "30", "weight": "0.3", "criteria": []},
        {"name": "商务", "max_score": "25", "weight": "0.25", "criteria": []},
        {"name": "服务", "max_score": "15", "weight": "0.15", "criteria": []},
        {"name": "资信", "max_score": "10", "weight": "0.1", "criteria": []},
    ]


@pytest.fixture
def lot_factory(client, pm_headers):
    """创建 项目→标段→维度→遴选 完整链路（API 调用），返回 project_id/lot_id/dims。"""

    async def _make(*, project_budget="2000000", lot_budget="500000", dims=None):
        import secrets

        if dims is None:
            dims = _dims_ok()
        r = await client.post("/api/v1/projects", headers=pm_headers, json={
            "project_code": f"ITEST-PJ-{secrets.token_hex(3)}", "name": "集成测试项目",
            "type": "SERVICE", "region": "华东", "budget": project_budget})
        assert r.status_code == 201, r.text
        project_id = r.json()["project_id"]
        r = await client.post(f"/api/v1/projects/{project_id}/lots", headers=pm_headers, json={
            "lot_code": f"ITEST-LT-{secrets.token_hex(3)}", "name": "集成测试标段",
            "budget": lot_budget})
        assert r.status_code == 201, r.text
        lot_id = r.json()["lot_id"]
        r = await client.post(f"/api/v1/lots/{lot_id}/dimensions", headers=pm_headers,
                              json={"dimensions": dims})
        assert r.status_code == 201, r.text
        r = await client.post(f"/api/v1/lots/{lot_id}/expert-criteria", headers=pm_headers, json={
            "expert_count": 5, "min_experts_per_dimension": 1,
            "weight_specialization": "0.4", "weight_experience": "0.3",
            "weight_review_quality": "0.2", "weight_region": "0.1", "min_experience": 3})
        assert r.status_code == 201, r.text
        return {"project_id": project_id, "lot_id": lot_id, "dims": dims}

    return _make


@pytest.fixture
def bid_factory(client, sup_headers, sup2_headers, sup3_headers):
    """3 家供应商各投一份 PDF 标书 → 返回 bid_ids（lot 需 BIDDING）。"""

    async def _make(lot_id, amounts=None):
        ids = []
        for i, h in enumerate((sup_headers, sup2_headers, sup3_headers)):
            pdf = b"%PDF-1.4\n%% itest bid content\n" + f"supplier {i} tech plan ".encode() * 30
            r = await client.post(f"/api/v1/lots/{lot_id}/bids", headers=h,
                                  files={"file": ("bid.pdf", pdf, "application/pdf")})
            assert r.status_code == 201, r.text
            ids.append(r.json()["bid_id"])
        return ids

    return _make


@pytest.fixture
def set_bid_parsed():
    """把标书置 PARSED（集成测试不跑真实解析流水线，解析链路留 P7.5）。"""

    async def _make(lot_id: str, bid_ids: list[str] | None = None) -> None:
        from sqlalchemy import text

        from app.core.database import session_factory

        async with session_factory() as s:
            if bid_ids is None:
                await s.execute(
                    text("UPDATE bid_document SET status='PARSED' WHERE lot_id=:l"), {"l": lot_id}
                )
            else:
                for bid in bid_ids:
                    await s.execute(
                        text("UPDATE bid_document SET status='PARSED' WHERE bid_id=:b"), {"b": bid}
                    )
            await s.commit()

    return _make
