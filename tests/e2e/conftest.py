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
# E2E 账号统一口令 = app 侧导入建号口令（expert_service.py:32 / supplier_service.py:38 的
# INITIAL_PASSWORD），也与运行栈演示账号一致（登录页提示「演示账号（密码均为 123456）」）。
# 原先写的 "Smart@2026" 是 scripts/synthetic/generators.py 的**合成数据集**常量——E2E 走的是
# /experts/import、/suppliers/import 建号路径，产品在那里发的是 123456，两者对不上，
# 于是 fixture 建/导入的账号登录必然失败。此处对齐产品实际值，不引入第二套口令约定。
PASSWORD = "123456"
PREFIX = "E2E"

# 连主库（sp-app 真实数据）。用 root 保证清理权限。
#
# 此处原有一个 MYSQL_DSN 常量（读 MYSQL_URL，缺省拼主库 DSN），已于 2026-09-22 删除：
# 它是**死变量**，全仓无读取方，实际连库参数由下方 MYSQL_PORT + MYSQL_ROOT_PASSWORD
# 决定（见 _sql）。留着它会让人以为 E2E 的库地址由 MYSQL_URL 控制——已被误导过一次。
#
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


def clear_first_login_gate(usernames) -> None:
    """把账号标记为「已完成首登改密」（users.must_change_password = 0）。

    为什么需要：自查 #6（58b4ad7）给 users 加了 must_change_password（建号时显式写 True：
    expert_service.py:160 / supplier_service.py:175），未改密账号除改密端点外业务 API
    一律 403（app/api/deps.py:58）。E2E 要用的是「可调业务 API」的账号，故进使用路径即清。

    调用点只有两处，都在 conftest 的「取用账号」收口上：`login`（UI 表单）与 `Api.__init__`
    （HTTP 直连）。**刻意不放在导入工厂里**——导入工厂只覆盖 /experts/import、/suppliers/import
    两条 API 路径，而 E2E-1/E2E-2 走的是 /admin/experts、/admin/suppliers **UI 上传页**，
    根本经过不了工厂；放在工厂里会漏，且每加一条导入路径就要再补一次。

    为什么直接改库而不是走 /api/auth/change-password：该端点要求新密码满足复杂度
    （≥8 位 + 大小写 + 数字，app/core/security.py:54），而产品导入口令 123456 不满足，
    拿它当新密码必然 400；改写成 fixture 自定的强口令，又会造出「测试账号口令 ≠ 产品
    导入口令」的第二套约定，下次产品改口令时同样静默失效。同款「建号即置 0」做法见
    scripts/import_synthetic_mysql.py + scripts/synthetic/generators.py（合成演示账号
    显式写 must_change_password=False，注释即「防旧 JSON 缺省」）。
    """
    for username in usernames:
        _sql("UPDATE users SET must_change_password = 0 WHERE username = :u", {"u": username})


def _seed_admin_pm() -> None:
    """预置 admin + pm 登录账号（专家/供应商由各流导入自动建号）。

    must_change_password 显式写 0：本函数造的是驱动业务流程的**预置账号**（语义=
    「已入职用户」），不是走首登改密流程的新账号；不写就取列默认 1，随后所有业务 API
    都会被 403 拦死。语义与 scripts/synthetic/generators.py 的合成演示账号一致。
    """
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
            "email, is_active, must_change_password, created_at, updated_at) "
            "VALUES (:uid, :u, :h, :r, :d, NULL, 1, 0, :now, :now)",
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
    clear_first_login_gate([username])  # E2E 把账号当「已入职」用（见该函数 docstring）
    # 诊断挂点（2026-09-22 排查用）：现象是登录 POST 返回 200、sp_token 已落 localStorage，
    # 但页面停在 /login 不跳转、也不再发任何请求。收集浏览器 console/pageerror，失败时打印。
    _diag: list[str] = []
    page.on("console", lambda m: _diag.append(f"console.{m.type}: {m.text}"))
    page.on("pageerror", lambda e: _diag.append(f"pageerror: {e}"))
    page.goto(f"{BASE_URL}/login")
    page.get_by_placeholder("请输入用户名").fill(username)
    page.get_by_placeholder("请输入密码").fill(password)
    page.get_by_role("button", name=re.compile("登")).click()
    try:
        page.wait_for_function("() => localStorage.getItem('sp_token') !== null", timeout=15000)
        # 原为 wait_for_load_state("networkidle")，2026-09-22 改。networkidle 的语义是「500ms
        # 内无任何网络连接」，本应用有持续后台轮询，可能永远达不到 idle。换成真正的完成信号：
        # 登录成功后 router 会离开 /login。
        page.wait_for_function("() => !location.pathname.startsWith('/login')", timeout=15000)
    except Exception:
        print("\n===LOGIN DIAG===")
        print("url:", page.url)
        print("storage:", page.evaluate("() => JSON.stringify(localStorage)"))
        print("diag log:")
        for line in _diag[-40:]:
            print("  ", line)
        print("===END LOGIN DIAG===")
        raise


# ==================== API client（数据准备/校验用） ====================


class Api:
    """httpx 直连 nginx（同源 /api/v1），带角色 token。用于前置数据准备 + 业务断言。"""

    def __init__(self, user_id: str, username: str, password: str = PASSWORD):
        import httpx

        # trust_env=False：本机 WinINET 系统代理(127.0.0.1:15490)会被 httpcore 读走，
        # 劫持 localhost:18080 请求致 502/10054；E2E 直连本地部署，禁用系统代理。
        self._client = httpx.Client(base_url=BASE_URL, timeout=60, trust_env=False)
        clear_first_login_gate([username])  # E2E 把账号当「已入职」用（见该函数 docstring）
        # 登录路由 /api/auth/login（不带 v1）：T15 e65b437 起 auth 单独挂 /api，
        # app/api/v1/__init__.py 注释「auth 不再挂 /api/v1」。业务端点仍是 /api/v1（见 request()）。
        r = self._client.post("/api/auth/login", json={"username": username, "password": password})
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
