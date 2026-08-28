"""P2.1 标书异步解析流水线验收脚本（本地 uvicorn :8001 + arq worker）。

覆盖 task.md P2.1 验收：
- 上传 3 份**合法 PDF**（reportlab 生成，含报价/工期/团队/质量认证/质保字段）
  → API 自动 enqueue → worker 7 步解析 → status=PARSED + parsing_step=NULL
- 结构化提取准确率：bid_amount/duration/team_size/structured_data 对照原值
- Milvus chunks 入库（chunk 数 > 0，embedding 1024 维）
- Neo4j BidDocument 节点 + BELONGS_TO/SUBMITTED_BY 关系
- 僵尸扫描：构造 PARSING + parsing_step>0 + updated_at 超时 → PARSE_FAILED
- DOCX 压缩炸弹防御：高压缩比 zip（>100:1）→ 上传 → 解析立即 PARSE_FAILED

前置：本地 uvicorn 起在 :8001；本机 arq worker 运行（poetry run arq app.tasks.worker.WorkerSettings）；
MySQL/Neo4j/Milvus/Redis 可达；embedding 后端就绪（bge-m3 容器或本机 torch）。
用法: poetry run python scripts/accept_p21_api.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import zipfile

import httpx
from neo4j import GraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402
from app.core.minio_client import get_minio_client, remove_prefix  # noqa: E402

BASE = "http://localhost:8001/api/v1"

PASS = 0
FAIL = 0

# 固定未投标组合（合成数据校验过：LOT-003/004/009 均无 SUP-001 投标）
# LOT-003: 010/015/014/017 | LOT-004: 010/006/007 | LOT-009: 010/020/017
COMBOS = [
    ("LOT-003", "SUP-001"),
    ("LOT-004", "SUP-001"),
    ("LOT-009", "SUP-001"),
]

# 三份标书内容参数（结构化提取断言基准）
DOCS = [
    {"company": "华北立讯通信有限公司", "amount": 3_280_000, "duration": 180, "team": 25, "cert": "ISO9001", "warranty": 36},
    {"company": "华东智联数据科技有限公司", "amount": 5_120_000, "duration": 240, "team": 18, "cert": "CMMI3", "warranty": 24},
    {"company": "华南安泽软件有限公司", "amount": 4_500_000, "duration": 200, "team": 30, "cert": "ISO27001", "warranty": 48},
]

ZOMBIE_BID = "BID-ZOMBIE-21"
BOMB_BID_LOT = "LOT-006"  # 压缩炸弹专用组合（SUP-001 未投 LOT-006）


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def db() -> AsyncEngine:
    return create_async_engine(settings.database_url)


def make_pdf(company: str, amount: int, duration: int, team: int, cert: str, warranty: int) -> bytes:
    """reportlab 生成含结构化字段的合法中文 PDF（pdfplumber 可提取）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 12)
    lines = [
        f"供应商：{company}",
        f"投标总报价：{amount:,}元",
        f"工期：{duration}天",
        f"项目团队：{team}人",
        f"{cert}质量管理体系认证",
        f"质保期：{warranty}个月",
        "第一章 公司概况",
        "我方是一家专业从事信息系统集成与软件开发的企业，成立于2008年。",
        "第二章 技术方案",
        "本方案基于微服务架构，包含需求分析、系统设计、实施交付与售后保障。",
    ]
    y = 780
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 20
    c.save()
    return buf.getvalue()


def make_bomb_docx() -> bytes:
    """构造压缩炸弹 DOCX：单 entry 全 'A'（200KB→~1KB，压缩比 >100:1）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"A" * (200 * 1024))
    return buf.getvalue()


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")


async def login(username: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败 {username}: {r.status_code} {r.text}"
        return r.json()["access_token"]


async def upload(token: str, lot_id: str, filename: str, content: bytes, supplier_id: str) -> httpx.Response:
    files = {"file": (filename, content, "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        return await client.post(
            f"{BASE}/lots/{lot_id}/bids",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data={"supplier_id": supplier_id},
        )


async def bid_status(token: str, bid_id: str) -> tuple[str, int | None]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/bids/{bid_id}/status", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return f"HTTP{r.status_code}", None
        return r.json()["status"], r.json().get("parsing_step")


async def wait_parsed(token: str, bid_id: str, timeout: float = 300) -> str:
    """轮询直到 PARSED/PARSE_FAILED（超时返回当前状态）。"""
    deadline = time.monotonic() + timeout
    last = "PENDING"
    while time.monotonic() < deadline:
        last, _ = await bid_status(token, bid_id)
        if last in ("PARSED", "PARSE_FAILED"):
            return last
        await asyncio.sleep(5)
    return last


async def enqueue(task: str) -> None:
    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(task)
    finally:
        await pool.aclose()


def milvus_chunks(bid_id: str) -> tuple[int, int | None]:
    """返回 (chunk 数, embedding 维度)。Milvus query。"""
    from app.core.milvus import get_collection

    collection = get_collection()
    collection.load()
    rows = collection.query(
        expr=f'bid_id == "{bid_id}"',
        output_fields=["chunk_id", "embedding"],
    )
    dim = len(rows[0]["embedding"]) if rows else None
    return len(rows), dim


def neo4j_bid(driver, bid_id: str, lot_id: str, supplier_id: str) -> tuple[bool, bool, bool]:
    """返回 (节点存在, BELONGS_TO, SUBMITTED_BY)。"""
    with driver.session() as session:
        node = session.run("MATCH (b:BidDocument {bidId:$id}) RETURN b.bidId", id=bid_id).single()
        belongs = session.run(
            "MATCH (b:BidDocument {bidId:$bid})-[:BELONGS_TO]->(l:Lot {lotId:$lot}) RETURN count(*)",
            bid=bid_id, lot=lot_id,
        ).single()[0]
        submitted = session.run(
            "MATCH (b:BidDocument {bidId:$bid})-[:SUBMITTED_BY]->(s:Supplier {supplierId:$sup}) RETURN count(*)",
            bid=bid_id, sup=supplier_id,
        ).single()[0]
    return node is not None, belongs > 0, submitted > 0


async def cleanup(engine, driver) -> None:
    """清验收残留（幂等）：MySQL + MinIO + Milvus + Neo4j。

    上传生成的 bid_id 是随机值，不能硬编码——按组合 + file_url LIKE 'bids/%'
    过滤真实上传的标书（合成数据 file_url 为空，不受影响）；僵尸记录单独删。
    """
    combos = list(COMBOS)
    async with engine.begin() as conn:
        # 先查残留实际 bid_id（供 Milvus/Neo4j 清理），再删 MySQL
        rows = (await conn.execute(
            text(
                "SELECT bid_id FROM bid_document "
                "WHERE bid_id=:zb OR (file_url LIKE 'bids/%' AND lot_id IN ('LOT-003','LOT-004','LOT-009','LOT-006'))"
            ),
            {"zb": ZOMBIE_BID},
        )).all()
        bid_ids = [r.bid_id for r in rows]
        for lot, sup in combos:
            await conn.execute(
                text("DELETE FROM bid_document WHERE lot_id=:lot AND supplier_id=:sup AND file_url LIKE 'bids/%'"),
                {"lot": lot, "sup": sup},
            )
        await conn.execute(text("DELETE FROM bid_document WHERE bid_id=:zb"), {"zb": ZOMBIE_BID})
        await conn.execute(
            text("DELETE FROM bid_document WHERE lot_id=:lot AND supplier_id=:sup AND file_url LIKE 'bids/%'"),
            {"lot": BOMB_BID_LOT, "sup": "SUP-001"},
        )
    client = get_minio_client()
    for lot, _ in combos:
        remove_prefix(client, f"bids/{lot}/")
    remove_prefix(client, f"bids/{BOMB_BID_LOT}/")
    # Milvus chunks + Neo4j 节点
    from app.core.milvus import get_collection

    try:
        collection = get_collection()
        for bid_id in bid_ids:
            collection.delete(f'bid_id == "{bid_id}"')
    except Exception:  # noqa: BLE001  collection 不存在等
        pass
    with driver.session() as session:
        if bid_ids:
            session.run(
                "MATCH (b:BidDocument) WHERE b.bidId IN $ids DETACH DELETE b",
                ids=bid_ids,
            )


async def main() -> None:
    global PASS, FAIL
    engine = await db()
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

    # 幂等：清上次残留
    await cleanup(engine, driver)
    print("[cleanup] 验收前残留已清理")

    # 前置检查：Redis 可达（worker 队列依赖）
    from arq import create_pool
    from arq.connections import RedisSettings

    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await pool.ping()
        await pool.aclose()
        check("Redis 可达", True)
    except Exception as e:  # noqa: BLE001
        check("Redis 可达", False, str(e))
        driver.close()
        await engine.dispose()
        sys.exit(1)

    token = await login("admin", TEST_PASSWORD)
    check("admin 登录", bool(token))

    # ==================== 上传 3 份合法 PDF → 自动解析 ====================
    print("\n[上传] 3 份 reportlab 合法 PDF（含结构化字段）→ API 自动触发解析")
    bid_ids: list[str] = []
    for (lot, sup), doc in zip(COMBOS, DOCS):
        r = await upload(token, lot, f"{doc['company']}_标书.pdf", make_pdf(**doc), sup)
        check(f"上传 {lot}+{sup} 201", r.status_code == 201, f"status={r.status_code} {r.text[:200]}")
        if r.status_code == 201:
            body = r.json()
            bid_ids.append(body["bid_id"])
            check(f"{body['bid_id']} 初始 status=SUBMITTED", body["status"] == "SUBMITTED", str(body))
    check("3 份上传成功", len(bid_ids) == 3, f"actual={len(bid_ids)}")

    # ==================== 轮询解析完成 ====================
    print("\n[解析] 轮询 7 步流水线 → PARSED + parsing_step=NULL")
    parsed_status: dict[str, str] = {}
    for i, (bid_id, doc) in enumerate(zip(bid_ids, DOCS)):
        st = await wait_parsed(token, bid_id)
        parsed_status[bid_id] = st
        check(f"{bid_id} 解析终态 PARSED", st == "PARSED", f"status={st}")
        # 状态接口展示 parsing_step=NULL
        s, step = await bid_status(token, bid_id)
        check(f"{bid_id} parsing_step=NULL", step is None, f"step={step}")

    # ==================== MySQL 结构化提取准确率 ====================
    print("\n[MySQL] 结构化字段提取（对照原值）")
    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT bid_id, bid_amount, duration, team_size, structured_data, status, parsing_step "
            "FROM bid_document WHERE bid_id IN :ids"
        ), {"ids": tuple(bid_ids)})).all()
    by_id = {r.bid_id: r for r in rows}
    correct = 0
    total_fields = 0
    for bid_id, doc in zip(bid_ids, DOCS):
        r = by_id.get(bid_id)
        if r is None:
            check(f"{bid_id} MySQL 记录存在", False)
            continue
        # 提取准确率：bid_amount/duration/team_size 三元组
        got = (int(r.bid_amount) if r.bid_amount else None,
               r.duration, r.team_size)
        want = (doc["amount"], doc["duration"], doc["team"])
        ok = got == want
        check(f"{bid_id} 结构化字段提取一致（{got} == {want}）", ok, f"got={got} want={want}")
        # 结构化 JSON：quality_cert / warranty_months（原生 SQL 返回 JSON 文本，需反序列化）
        sd = json.loads(r.structured_data) if r.structured_data else {}
        cert_ok = sd.get("quality_cert") == doc["cert"]
        war_ok = sd.get("warranty_months") == doc["warranty"]
        check(f"{bid_id} quality_cert={doc['cert']}", cert_ok, str(sd))
        check(f"{bid_id} warranty_months={doc['warranty']}", war_ok, str(sd))
        if ok:
            correct += 1
        total_fields += 1
    accuracy = correct / total_fields if total_fields else 0
    check(f"结构化提取准确率 ≥95%（实际 {accuracy:.0%}）", accuracy >= 0.95, f"correct={correct}/{total_fields}")

    # ==================== Milvus 入库 ====================
    print("\n[Milvus] chunks 入库（1024 维）")
    for bid_id in bid_ids:
        n, dim = milvus_chunks(bid_id)
        check(f"{bid_id} chunks>0", n > 0, f"count={n}")
        check(f"{bid_id} embedding 1024 维", dim == 1024, f"dim={dim}")

    # ==================== Neo4j 节点 + 关系 ====================
    print("\n[Neo4j] BidDocument 节点 + BELONGS_TO/SUBMITTED_BY")
    for bid_id, (lot, sup) in zip(bid_ids, COMBOS):
        node, belongs, submitted = neo4j_bid(driver, bid_id, lot, sup)
        check(f"{bid_id} 节点存在", node)
        check(f"{bid_id} BELONGS_TO->{lot}", belongs)
        check(f"{bid_id} SUBMITTED_BY->{sup}", submitted)

    # ==================== 僵尸扫描 ====================
    print("\n[僵尸扫描] PARSING + parsing_step>0 + updated_at 超时 → PARSE_FAILED")
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO bid_document (bid_id, lot_id, supplier_id, status, parsing_step, created_at, updated_at) "
            "VALUES (:bid, 'LOT-003', 'SUP-002', 'PARSING', 3, NOW(), NOW() - INTERVAL 40 MINUTE)"
        ), {"bid": ZOMBIE_BID})
    await enqueue("scan_zombie_parsing")
    deadline = time.monotonic() + 60
    zs = "PARSING"
    while time.monotonic() < deadline:
        async with engine.begin() as conn:
            zs = (await conn.execute(
                text("SELECT status FROM bid_document WHERE bid_id=:bid"), {"bid": ZOMBIE_BID}
            )).scalar_one_or_none()
        if zs == "PARSE_FAILED":
            break
        await asyncio.sleep(3)
    check("僵尸记录 → PARSE_FAILED", zs == "PARSE_FAILED", f"status={zs}")

    # ==================== DOCX 压缩炸弹防御 ====================
    print("\n[DOCX 压缩炸弹] 高压缩比 zip（>100:1）→ 解析立即 PARSE_FAILED")
    r = await upload(token, BOMB_BID_LOT, "bomb.docx", make_bomb_docx(), "SUP-001")
    check("压缩炸弹上传 201（magic bytes 通过）", r.status_code == 201, f"status={r.status_code}")
    if r.status_code == 201:
        bomb_bid = r.json()["bid_id"]
        st = await wait_parsed(token, bomb_bid, timeout=120)
        check("压缩炸弹 → PARSE_FAILED（不重试）", st == "PARSE_FAILED", f"status={st}")

    # ==================== 清理 ====================
    await cleanup(engine, driver)
    print("\n[cleanup] 验收残留已清理（MySQL + MinIO + Milvus + Neo4j）")
    driver.close()
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
