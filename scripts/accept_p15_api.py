"""P1.5 标书管理 API 验收脚本（本地 uvicorn :8001）。

覆盖 task.md P1.5 验收：
- 上传 3 份标书 PDF + 1 份 DOCX → MySQL bid_document 有记录 → MinIO 可下载
- 状态从 SUBMITTED 开始流转（parsing_step=0，freeze_hash 保持 NULL=未封存）
- GET 详情（含结构化数据 + 动态预签名 URL）/ GET status
- POST retry-parse（PARSE_FAILED → SUBMITTED + parsing_step=0）
- 错误路径：非 PDF/DOCX 422 / 重复投标 409 / lot 不存在 404 / lot 非 BIDDING 400
  / 拉黑供应商 400 / 非授权 403 / 超大文件 413 / 未失败 retry 400
- 供应商账号绑定：supplier_01 自投绑定 SUP-001；显式代投他人 → 403

前置：本地 uvicorn 起在 :8001；合成数据已导入（P1.1）。
用法: poetry run python scripts/accept_p15_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
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

# 验收使用的固定投标组合（全部为合成数据中"未投标"的组合，避免与 52 条合成投标冲突）
# LOT-001: 已投 SUP-001/002/003 | LOT-002: 007/010/013/017 | LOT-003: 010/014/015/017
# LOT-004: 006/007/010 | LOT-005: 011/013/016
COMBOS = [
    ("LOT-001", "SUP-004"),
    ("LOT-002", "SUP-001"),
    ("LOT-003", "SUP-001"),
    ("LOT-004", "SUP-001"),
    ("LOT-005", "SUP-001"),  # supplier_01 自投绑定
]


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


def make_pdf(company: str, amount: int) -> bytes:
    """构造合法 magic bytes 的仿真 PDF（%PDF 开头）。"""
    body = f"标书: {company} 技术方案\n报价 {amount} 元\n工期 180 天".encode("utf-8")
    return b"%PDF-1.4\n" + body + b"\n%%EOF\n"


def make_docx(company: str) -> bytes:
    """构造合法 magic bytes 的仿真 DOCX（zip 头 PK\\x03\\x04）。"""
    return b"PK\x03\x04" + f"word/document.xml 商务标 {company}".encode("utf-8")


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")


async def login(username: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败 {username}: {r.status_code} {r.text}"
        return r.json()["access_token"]


async def upload(token: str, lot_id: str, filename: str, content: bytes, supplier_id: str | None = None) -> httpx.Response:
    files = {"file": (filename, content, "application/octet-stream")}
    data = {"supplier_id": supplier_id} if supplier_id else None
    async with httpx.AsyncClient(timeout=120.0) as client:
        return await client.post(
            f"{BASE}/lots/{lot_id}/bids",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data=data,
        )


async def cleanup(engine) -> None:
    """清理验收残留（幂等）：删除固定组合标书 + 构造的 retry 记录 + MinIO 对象。"""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM bid_document WHERE bid_id='BID-TEST-RETRY'"))
        for lot, sup in COMBOS:
            await conn.execute(
                text("DELETE FROM bid_document WHERE lot_id=:lot AND supplier_id=:sup"),
                {"lot": lot, "sup": sup},
            )
    client = get_minio_client()
    for lot, _ in COMBOS:
        remove_prefix(client, f"bids/{lot}/")


async def main() -> None:
    global PASS, FAIL
    engine = await db()

    # 幂等：开头清上次残留
    await cleanup(engine)
    print("[cleanup] 验收前残留已清理")

    token = await login("admin", TEST_PASSWORD)
    check("admin 登录", bool(token))

    # ==================== 上传 3 PDF + 1 DOCX ====================
    print("\n[上传] 3 PDF + 1 DOCX（admin 代传）")
    pdfs = {
        ("LOT-001", "SUP-004"): ("supp001.pdf", make_pdf("华北立讯通信有限公司", 3_280_000)),
        ("LOT-002", "SUP-001"): ("supp002.pdf", make_pdf("东北泰安安防有限公司", 5_120_000)),
        ("LOT-003", "SUP-001"): ("supp003.pdf", make_pdf("东北泰安安防有限公司", 4_500_000)),
    }
    docx = {("LOT-004", "SUP-001"): ("supp004.docx", make_docx("东北泰安安防有限公司"))}

    uploaded: list[tuple[str, str, bytes]] = []  # (bid_id, presigned_url, content)
    for (lot, sup), (fname, content) in pdfs.items():
        r = await upload(token, lot, fname, content, supplier_id=sup)
        check(f"上传 {lot}+{sup} 201", r.status_code == 201, f"status={r.status_code} {r.text[:200]}")
        if r.status_code == 201:
            body = r.json()
            check(f"{lot} status=SUBMITTED", body["status"] == "SUBMITTED", str(body))
            check(f"{lot} parsing_step=0", body["parsing_step"] == 0, str(body))
            check(f"{lot} freeze 语义=未封存", body.get("freeze_hash") is None, str(body))
            check(f"{lot} 预签名 URL 存在", body["presigned_url"].startswith("http"), str(body))
            uploaded.append((body["bid_id"], body["presigned_url"], content))
    for (lot, sup), (fname, content) in docx.items():
        r = await upload(token, lot, fname, content, supplier_id=sup)
        check(f"DOCX 上传 {lot}+{sup} 201", r.status_code == 201, f"status={r.status_code} {r.text[:200]}")
        if r.status_code == 201:
            body = r.json()
            check("DOCX status=SUBMITTED", body["status"] == "SUBMITTED", str(body))
            uploaded.append((body["bid_id"], body["presigned_url"], content))
    check("共上传 4 份", len(uploaded) == 4, f"actual={len(uploaded)}")

    # ==================== MySQL 记录 ====================
    print("\n[MySQL] 记录确认")
    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT bid_id, lot_id, supplier_id, status, parsing_step, freeze_hash FROM bid_document "
            "WHERE file_url LIKE 'bids/%' AND ("
            "(lot_id IN ('LOT-001','LOT-002','LOT-003','LOT-004') AND supplier_id='SUP-001') "
            "OR (lot_id='LOT-001' AND supplier_id='SUP-004'))"
        ))).all()
    check("新增 4 条 bid_document（admin 上传）", len(rows) == 4, f"actual={len(rows)}")
    # P2.1 起上传自动触发解析，status 会被 worker 推进（SUBMITTED→PARSING→PARSED）
    check("全部进入解析流（SUBMITTED/PARSING/PARSED）+ parsing_step 初始 0",
          all(r.status in ("SUBMITTED", "PARSING", "PARSED") and r.parsing_step == 0 for r in rows), str(rows))
    check("freeze_hash 全部 NULL（未封存）", all(r.freeze_hash is None for r in rows), str(rows))

    # ==================== MinIO 可下载 ====================
    print("\n[MinIO] 预签名 URL 下载验证")
    for bid_id, url, content in uploaded:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
        check(f"{bid_id} MinIO 下载 200", r.status_code == 200, f"status={r.status_code}")
        check(f"{bid_id} 内容一致", r.content == content, f"len={len(r.content)} vs {len(content)}")

    # ==================== GET 详情 / status ====================
    print("\n[查询] 详情 / 进度")
    first_bid_id, first_url, _ = uploaded[0]
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/bids/{first_bid_id}", headers={"Authorization": f"Bearer {token}"})
    check("GET 详情 200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        body = r.json()
        check("详情 bid_id 匹配", body["bid_id"] == first_bid_id, str(body))
        check("详情 structured_data 字段在位", "structured_data" in body, str(body))
        check("详情含动态预签名 URL", body["presigned_url"].startswith("http"), str(body))
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/bids/{first_bid_id}/status", headers={"Authorization": f"Bearer {token}"})
    check("GET status 200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        body = r.json()
        check("status 进入解析流", body["status"] in ("SUBMITTED", "PARSING", "PARSED"), str(body))
        check("status parsing_step 初始 0", body["parsing_step"] == 0, str(body))

    # 合成数据标书（BID-001 含结构化数据）详情验证"含结构化数据"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/bids/BID-001", headers={"Authorization": f"Bearer {token}"})
    check("合成标书 BID-001 详情 200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        body = r.json()
        check("BID-001 含结构化数据", body.get("structured_data", {}).get("quality_cert") == "CMMI3", str(body.get("structured_data")))

    # ==================== 供应商账号绑定 ====================
    print("\n[供应商绑定] supplier_01 自投 LOT-005")
    sup_token = await login("supplier_01", TEST_PASSWORD)
    r = await upload(sup_token, "LOT-005", "self_bid.pdf", make_pdf("东北泰安安防有限公司", 6_000_000))
    check("supplier_01 自投 201（自动绑定 SUP-001）", r.status_code == 201, f"status={r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        check("自投 supplier_id=SUP-001", r.json()["supplier_id"] == "SUP-001", str(r.json()))
        uploaded.append((r.json()["bid_id"], r.json()["presigned_url"], b""))
    # 自投落库校验
    async with engine.begin() as conn:
        self_cnt = (await conn.execute(text(
            "SELECT COUNT(*) FROM bid_document WHERE file_url LIKE 'bids/%' AND lot_id='LOT-005' AND supplier_id='SUP-001'"
        ))).scalar_one()
    check("自投标书落库=1", self_cnt == 1, f"actual={self_cnt}")
    # 显式代投他人 → 403
    r = await upload(sup_token, "LOT-005", "other.pdf", make_pdf("x", 1), supplier_id="SUP-013")
    check("supplier_01 代投 SUP-013 → 403", r.status_code == 403, f"status={r.status_code}")
    # SUPPLIER 无详情查看权限
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/bids/{first_bid_id}", headers={"Authorization": f"Bearer {sup_token}"})
    check("supplier_01 查看详情 → 403", r.status_code == 403, f"status={r.status_code}")

    # ==================== 错误路径 ====================
    print("\n[错误路径]")
    # 非 PDF/DOCX → 422
    r = await upload(token, "LOT-005", "bad.txt", b"this is plain text, not a bid file")
    check("非 PDF/DOCX → 422", r.status_code == 422, f"status={r.status_code}")
    # 重复投标 → 409
    r = await upload(token, "LOT-001", "again.pdf", make_pdf("华北立讯通信有限公司", 1), supplier_id="SUP-004")
    check("重复投标 → 409", r.status_code == 409, f"status={r.status_code}")
    # lot 不存在 → 404
    r = await upload(token, "LOT-999", "x.pdf", make_pdf("x", 1), supplier_id="SUP-004")
    check("lot 不存在 → 404", r.status_code == 404, f"status={r.status_code}")
    # 拉黑供应商 → 400（P1.1 修复：SUP-005 常驻拉黑，脚本不还原；同时作为数据修复门禁）
    async with engine.begin() as conn:
        cnt = (await conn.execute(text(
            "SELECT COUNT(*) FROM supplier WHERE supplier_id='SUP-005' AND blacklisted=TRUE AND status='INACTIVE'"
        ))).scalar_one()
    check("SUP-005 数据修复（常驻拉黑）", cnt == 1, f"cnt={cnt}")
    r = await upload(token, "LOT-006", "x.pdf", make_pdf("西北云启云计有限公司", 1), supplier_id="SUP-005")
    check("拉黑供应商 → 400", r.status_code == 400, f"status={r.status_code}")
    # 非授权（REVIEW_EXPERT）→ 403
    expert_token = await login("expert_01", TEST_PASSWORD)
    r = await upload(expert_token, "LOT-005", "x.pdf", make_pdf("x", 1))
    check("专家上传 → 403", r.status_code == 403, f"status={r.status_code}")
    # 超大文件 → 413（51MB，构造慢但一次性）
    big = b"%PDF" + b"\0" * (51 * 1024 * 1024)
    r = await upload(token, "LOT-005", "big.pdf", big, supplier_id="SUP-004")
    check("超大文件 → 413", r.status_code == 413, f"status={r.status_code}")
    del big
    # lot 非 BIDDING → 400（临时改 LOT-006，finally 恢复）
    try:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE lot SET status='PRE_SCREEN' WHERE lot_id='LOT-006'"))
        r = await upload(token, "LOT-006", "x.pdf", make_pdf("x", 1), supplier_id="SUP-001")
        check("lot 非 BIDDING → 400", r.status_code == 400, f"status={r.status_code}")
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE lot SET status='BIDDING' WHERE lot_id='LOT-006'"))

    # ==================== retry-parse ====================
    print("\n[retry-parse]")
    # 构造 PARSE_FAILED 记录（绕过上传，直接插库）
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO bid_document (bid_id, lot_id, supplier_id, status, parsing_step, created_at, updated_at) "
            "VALUES ('BID-TEST-RETRY', 'LOT-010', 'SUP-004', 'PARSE_FAILED', 3, NOW(), NOW())"
        ))
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/bids/BID-TEST-RETRY/retry-parse", headers={"Authorization": f"Bearer {token}"})
    check("retry-parse 200", r.status_code == 200, f"status={r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        body = r.json()
        check("retry 后进入解析流", body["status"] in ("SUBMITTED", "PARSING", "PARSED"), str(body))
        check("retry 后 parsing_step=0", body["parsing_step"] == 0, str(body))
    # 未失败状态 retry → 400
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/bids/{first_bid_id}/retry-parse", headers={"Authorization": f"Bearer {token}"})
    check("SUBMITTED 状态 retry → 400", r.status_code == 400, f"status={r.status_code}")

    # ==================== 清理 ====================
    await cleanup(engine)
    async with engine.begin() as conn:
        left = (await conn.execute(text(
            "SELECT COUNT(*) FROM bid_document WHERE bid_id='BID-TEST-RETRY' OR (file_url LIKE 'bids/%' AND ("
            "lot_id IN ('LOT-001','LOT-002','LOT-003','LOT-004','LOT-005') AND supplier_id='SUP-001' OR "
            "(lot_id='LOT-001' AND supplier_id='SUP-004')))"
        ))).scalar_one()
    check("清理后无残留记录", left == 0, f"actual={left}")
    print("[cleanup] 验收残留已清理（MySQL + MinIO）")

    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
