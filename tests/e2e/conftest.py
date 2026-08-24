"""P7.4 浏览器级 E2E 基础设施。

Playwright 连 nginx（http://localhost:8080）→ 真实 sp-app 容器 + 中间件，
验证"前端 UI + 后端 + 存储"整链路（集成测试是进程内 ASGITransport，这里走真实部署）。

数据隔离：
- 业务数据一律 `E2E-` 前缀（project_id/lot_id/bid_id/expert_id/supplier_id/user_id）
- 管理员/项目经理账号 conftest 预置（专家/供应商通过导入自动建登录账号，系统约定）
- 每条测试后清理 MySQL（E2E- 主键） + Neo4j（E2E- 节点） + Milvus/MinIO（E2E 标书）

前置依赖（P7.4 环境）：
1. `docker compose up -d`（8 容器全 healthy）
2. `docker compose up -d nginx`（web/dist 已 build）
3. playwright 已装且 chromium 已下载
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone

import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8080")
PASSWORD = "Smart@2026"
PREFIX = "E2E"

# 连主库（sp-app 真实数据）。用 root 保证清理权限。
MYSQL_DSN = os.environ.get(
    "MYSQL_URL", "mysql+asyncmy://smart:smart_procurement_dev@localhost:3306/smart_procurement"
)
# 宿主 MySQL 端口（同机多栈冲突时经 .env 重映射为 13306，故参数化）
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "neo4j_dev_pass")

MILVUS_HOST = os.environ.get("MILVUS_HOST", "localhost")
MILVUS_PORT = int(os.environ.get("MILVUS_PORT", "19530"))
MILVUS_COLLECTION = os.environ.get("MILVUS_COLLECTION", "bid_documents")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minio_dev_pass")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "bid-files")


# ==================== 同步 DB 访问（asyncmy + asyncio.run） ====================
# E2E 测试用 Playwright sync API（普通 def），DB 操作用 asyncio.run 包一层，
# 不与 pytest-asyncio loop 冲突（e2e 测试无 asyncio 标记）。

def _run(coro):
    """在独立线程执行 async 协程。

    pytest-asyncio asyncio_mode=auto 使测试运行在 event loop 内，直接
    asyncio.run() 会报 "cannot be called from a running event loop"。
    子线程有自己的 loop，规避冲突。
    """
    import threading

    box: dict = {}

    def _worker():
        box["v"] = asyncio.run(coro)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return box["v"]


def _sql(sql: str, params: dict | list | None = None) -> list[tuple]:
    """执行 SQL 并返回行。

    asyncmy 用 `%s` 位置占位符（MySQLdb 风格），不支持 `:name` 命名参数。
    dict 参数自动转 `%s` + 顺序元组（按 SQL 中 :name 出现顺序），
    调用方无需感知底层驱动差异。
    """

    async def _do():
        from asyncmy import connect

        if isinstance(params, dict):
            import re

            ordered: list = []

            def _sub(m):
                ordered.append(params[m.group(1)])
                return "%s"

            sql2 = re.sub(r":([A-Za-z_]\w*)", _sub, sql)
            exec_params: tuple = tuple(ordered)
        else:
            sql2, exec_params = sql, (tuple(params) if params else ())

        conn = await connect(host="localhost", port=MYSQL_PORT, user="root",
                             password=os.environ.get("MYSQL_ROOT_PASSWORD", "root_dev_pass"),
                             database="smart_procurement", charset="utf8mb4")
        try:
            cur = conn.cursor()
            await cur.execute(sql2, exec_params)
            rows = await cur.fetchall()
            await conn.commit()
            return rows or []
        finally:
            conn.close()

    return _run(_do())


def _execute(sql: str, params: dict | None = None) -> None:
    _sql(sql, params)


# ==================== 预置管理员/项目经理 ====================


def _seed_admin_pm() -> None:
    """预置 admin + pm 登录账号（专家/供应商由各流导入自动建号）。"""
    from app.core.security import hash_password  # 复用项目 hash（bcrypt，与后端一致）

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    h = hash_password(PASSWORD)
    rows = [
        ("E2E-U-ADMIN", "e2e_admin", "ADMIN", "E2E管理员"),
        ("E2E-U-PM", "e2e_pm", "PROJECT_MANAGER", "E2E项目经理"),
    ]
    for user_id, username, role, display in rows:
        _execute(
            "INSERT IGNORE INTO users (user_id, username, password_hash, role, display_name, "
            "email, is_active, created_at, updated_at) "
            "VALUES (:uid, :u, :h, :r, :d, NULL, 1, :now, :now)",
            {"uid": user_id, "u": username, "h": h, "r": role, "d": display, "now": now},
        )


# ==================== 数据清理 ====================


def _cleanup_mysql() -> list[str]:
    """删除所有 E2E 数据，返回 E2E bid_id 列表（供 Milvus/MinIO 精确清理）。

    project/lot/bid 主键为 LOT-xxx/BID-xxx（非 E2E- 前缀），按业务编码列
    （project_code/lot_code）关联删除；其余表按 E2E- 主键前缀删除。
    """
    # ---- 项目/标段/标书业务链（无外键，先子后父）----
    lot_ids = [r[0] for r in _sql("SELECT lot_id FROM lot WHERE lot_code LIKE 'E2E-%%'")]
    bids: list[str] = []
    dims: list[str] = []
    if lot_ids:
        ph = ",".join(["%s"] * len(lot_ids))
        dims = [r[0] for r in _sql(
            f"SELECT dimension_id FROM scoring_dimension WHERE lot_id IN ({ph})", tuple(lot_ids))]
        bids = [r[0] for r in _sql(
            f"SELECT bid_id FROM bid_document WHERE lot_id IN ({ph})", tuple(lot_ids))]
        # 含 lot_id 的子表
        for table in ("expert_conflict_declaration", "lot_expert_assignment",
                      "lot_expert_criteria", "scoring_dimension", "bid_document",
                      "award_result"):
            try:
                _execute(f"DELETE FROM `{table}` WHERE lot_id IN ({ph})", tuple(lot_ids))
            except Exception:  # noqa: BLE001  单表失败不阻断整体清理
                pass
        if dims:
            phd = ",".join(["%s"] * len(dims))
            try:
                _execute(f"DELETE FROM scoring_criterion WHERE dimension_id IN ({phd})", tuple(dims))
            except Exception:  # noqa: BLE001
                pass
        if bids:
            phb = ",".join(["%s"] * len(bids))
            try:
                _execute(f"DELETE FROM expert_review WHERE bid_id IN ({phb})", tuple(bids))
            except Exception:  # noqa: BLE001
                pass
        _execute("DELETE FROM lot WHERE lot_code LIKE 'E2E-%%'")
    _execute("DELETE FROM project WHERE project_code LIKE 'E2E-%%'")

    # ---- 所有 E2E 登录账号：user_id 为 generate_id("U") 的 U-xxx，非 E2E- 前缀主键删除删不到；
    #      按 display_name LIKE 'E2E%' 直接删（含 admin/pm，由下个测试 _seed_admin_pm 幂等重建）。
    #      不依赖 supplier/expert 表（表行可能已删，无法反查 name 导致残留累积）----
    try:
        _execute("DELETE FROM users WHERE display_name LIKE 'E2E%%'")
    except Exception:  # noqa: BLE001
        pass

    # ---- E2E- 主键前缀表（supplier/expert/users 等）----
    tables = _sql(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA='smart_procurement' AND CONSTRAINT_NAME='PRIMARY' "
        "ORDER BY TABLE_NAME"
    )
    seen: set[str] = set()
    for table, col in tables:
        if table in seen:
            continue
        seen.add(table)
        try:
            _execute(f"DELETE FROM `{table}` WHERE `{col}` LIKE '{PREFIX}-%%'")
        except Exception:  # noqa: BLE001  单表失败不阻断整体清理
            pass

    # ---- user_id 关联残留（notification/audit_log 主键非 E2E-）----
    for table in ("notification", "audit_log"):
        try:
            _execute(f"DELETE FROM `{table}` WHERE user_id LIKE '{PREFIX}-%%'")
        except Exception:  # noqa: BLE001
            pass
    return bids


def _cleanup_neo4j() -> None:
    """删除 E2E- 前缀节点（Neo4j 共享，无法隔离）。"""

    async def _do():
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        try:
            async with driver.session() as s:
                await s.run(
                    "MATCH (n) WHERE n.expertId STARTS WITH 'E2E-' OR n.supplierId STARTS WITH 'E2E-' "
                    "OR n.projectId STARTS WITH 'E2E-' OR n.lotId STARTS WITH 'E2E-' "
                    "OR n.dimensionId STARTS WITH 'E2E-' OR n.bidId STARTS WITH 'E2E-' "
                    "DETACH DELETE n"
                )
        finally:
            await driver.close()

    _run(_do())


def _cleanup_milvus(bid_ids: list[str]) -> None:
    """删除 E2E 标书向量（bid_id 为 BID-xxx，需按 id 精确删）。"""

    try:
        from pymilvus import connections, utility

        connections.connect(alias="e2e_clean", host=MILVUS_HOST, port=MILVUS_PORT)
        if utility.has_collection(MILVUS_COLLECTION, using="e2e_clean"):
            from pymilvus import Collection

            col = Collection(MILVUS_COLLECTION, using="e2e_clean")
            for b in bid_ids:
                try:
                    col.delete(f'bid_id == "{b}"')
                except Exception:  # noqa: BLE001  单个缺失不阻断
                    pass
            col.flush()
    except Exception:  # noqa: BLE001  Milvus 故障不阻断
        pass
    finally:
        try:
            connections.disconnect("e2e_clean")
        except Exception:  # noqa: BLE001
            pass


def _cleanup_minio(bid_ids: list[str]) -> None:
    """删除 E2E 标书对象（object key 含 bid_id，按 id 精确删）。"""

    try:
        from minio import Minio

        client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY,
                       secret_key=MINIO_SECRET_KEY, secure=False)
        if client.bucket_exists(MINIO_BUCKET):
            for b in bid_ids:
                for key in (f"bids/{b}.pdf", f"bids/{b}", b):
                    try:
                        client.remove_object(MINIO_BUCKET, key)
                    except Exception:  # noqa: BLE001  单个缺失不阻断
                        pass
    except Exception:  # noqa: BLE001  MinIO 故障不阻断
        pass


def cleanup_e2e_data() -> None:
    bids = _cleanup_mysql()
    _cleanup_neo4j()
    _cleanup_milvus(bids)
    _cleanup_minio(bids)


# ==================== fixtures ====================


@pytest.fixture(scope="session", autouse=True)
def _seed_and_final_cleanup():
    _seed_admin_pm()
    yield
    cleanup_e2e_data()  # 会话结束兜底清理


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """每测试独立 context（cookie 隔离），1600x900 视口。"""
    ctx = browser.new_context(viewport={"width": 1600, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    yield pg
    ctx.close()


@pytest.fixture(autouse=True)
def _seed_and_cleanup_after_test():
    # 每个测试前预置 admin/pm：cleanup 会 `DELETE users WHERE user_id LIKE 'E2E-%'`
    # 连带删掉预置账号，而 seed 是 session scope 只跑一次，故须在每个测试前重种（幂等 INSERT IGNORE）。
    _seed_admin_pm()
    yield
    cleanup_e2e_data()


# ==================== 登录 helper（UI 表单） ====================


def login(page, username: str, password: str = PASSWORD) -> None:
    """走真实登录表单，登录成功（localStorage 落 token）。"""
    page.goto(f"{BASE_URL}/login")
    page.get_by_placeholder("请输入用户名").fill(username)
    page.get_by_placeholder("请输入密码").fill(password)
    page.get_by_role("button", name=re.compile("登")).click()
    page.wait_for_function("() => localStorage.getItem('sp_token') !== null", timeout=15000)
    page.wait_for_load_state("networkidle")


# ==================== API client（数据准备/校验用） ====================


class Api:
    """httpx 直连 nginx（同源 /api/v1），带角色 token。用于前置数据准备 + 业务断言。"""

    def __init__(self, user_id: str, username: str, password: str = PASSWORD):
        import httpx

        # trust_env=False：本机 WinINET 系统代理(127.0.0.1:15490)会被 httpcore 读走，
        # 劫持 localhost:18080 请求致 502/10054；E2E 直连本地部署，禁用系统代理。
        self._client = httpx.Client(base_url=BASE_URL, timeout=60, trust_env=False)
        r = self._client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败 {username}: {r.text}"
        self.token = r.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def request(self, method: str, path: str, **kw):
        r = self._client.request(method, f"/api/v1{path}", headers=self.headers, **kw)
        return r

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def close(self):
        self._client.close()


@pytest.fixture
def admin_api():
    a = Api("E2E-U-ADMIN", "e2e_admin")
    yield a
    a.close()


@pytest.fixture
def pm_api():
    a = Api("E2E-U-PM", "e2e_pm")
    yield a
    a.close()
