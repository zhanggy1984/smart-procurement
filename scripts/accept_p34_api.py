"""P3.4 评审工作台业务逻辑验收脚本（本地 uvicorn :8001）。

覆盖 task.md P3.4 验收：
- 维度评分暂存（DRAFT）：save_score 可反复保存，状态 DRAFT 可见
- 提交锁定：submit → CONFIRMED（采纳 AI 建议）/ MANUAL_ADJUSTED（手动改过）
- 提交后不可回改：锁定状态 save_score → 400（ReviewLockedError）
- 报价评审剥离已在 P3.3 验证（price_calc 公式）

前置：uvicorn :8001 + 本机 arq worker；bge-m3；MySQL/Neo4j/Milvus/Redis。
用法: poetry run python scripts/accept_p34_api.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402
from app.core.minio_client import get_minio_client, remove_prefix  # noqa: E402

BASE = "http://localhost:8001/api/v1"
PASS = 0
FAIL = 0

LOT_ID = "LOT-003"
SUP_ID = "SUP-001"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_pdf() -> bytes:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 12)
    lines = ["供应商：华远云控", "投标总报价：3,280,000元",
             "第一章 技术方案", "我方采用微服务架构、容器化部署等关键技术路线。",
             "我方具备CMMI3质量管理体系认证。"]
    y = 780
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 20
    c.save()
    return buf.getvalue()


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "123456")


async def login(username: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": TEST_PASSWORD})
        assert r.status_code == 200, f"登录失败 {username}"
        return r.json()["access_token"]


async def cleanup(engine, bid_id: str | None = None) -> None:
    async with engine.begin() as conn:
        if bid_id:
            rev_rows = (await conn.execute(text("SELECT review_id FROM expert_review WHERE bid_id=:b"), {"b": bid_id})).all()
            rev_ids = tuple(r[0] for r in rev_rows)
            if rev_ids:
                await conn.execute(text("DELETE FROM conversation_message WHERE review_id IN :ids"), {"ids": rev_ids})
            await conn.execute(text("DELETE FROM expert_review WHERE bid_id=:b"), {"b": bid_id})
        await conn.execute(
            text("DELETE FROM lot_expert_assignment WHERE lot_id=:lot AND expert_id='EXP-001'"),
            {"lot": LOT_ID},
        )
        await conn.execute(text("DELETE FROM bid_document WHERE lot_id=:lot AND file_url LIKE 'bids/%'"), {"lot": LOT_ID})
    remove_prefix(get_minio_client(), f"bids/{LOT_ID}/")


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)
    await cleanup(engine)
    print("[cleanup] 验收前残留已清理")

    admin = await login("admin")
    # 上传 → 解析 → FROZEN
    files = {"file": ("标书.pdf", make_pdf(), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/lots/{LOT_ID}/bids", headers={"Authorization": f"Bearer {admin}"},
                              files=files, data={"supplier_id": SUP_ID})
    assert r.status_code == 201, f"上传失败 {r.status_code}"
    bid_id = r.json()["bid_id"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{BASE}/bids/{bid_id}/status", headers={"Authorization": f"Bearer {admin}"})
        if r.json()["status"] == "PARSED":
            break
        await asyncio.sleep(5)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE bid_document SET status='FROZEN' WHERE bid_id=:b"), {"b": bid_id})
    print("  [setup] 标书 PARSED + FROZEN")

    # 维度
    async with engine.connect() as conn:
        dims = (await conn.execute(text(
            "SELECT dimension_id, name, max_score FROM scoring_dimension WHERE lot_id=:lot ORDER BY sort_order LIMIT 3"
        ), {"lot": LOT_ID})).all()
    check("获取 ≥2 个维度", len(dims) >= 2, f"dims={len(dims)}")
    d1, d2 = dims[0], dims[1]

    # 评审归属校验前置（P4.2 分配）：EXP-001 分配 d1/d2 维度，否则 create_review 403
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO lot_expert_assignment (lot_id, expert_id, dimension_ids, status) "
                 "VALUES (:lot, :exp, :dims, 'PENDING_DECLARATION')"),
            {"lot": LOT_ID, "exp": "EXP-001",
             "dims": json.dumps([d1.dimension_id, d2.dimension_id])},
        )
    print("  [setup] 专家-标段分配已前置（评审归属校验）")

    expert = await login("expert_01")
    headers = {"Authorization": f"Bearer {expert}"}

    def create_review(dim_id: str) -> str:
        return f"{BASE}/reviews"

    # 维度1：采纳 AI 建议 → CONFIRMED
    print("\n[维度1] 暂存 → 提交 CONFIRMED")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews", headers=headers,
                              json={"bid_id": bid_id, "dimension_id": d1.dimension_id})
    rid1 = r.json()["review_id"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/reviews/{rid1}/score", headers=headers,
                             json={"score": 28, "comment": "采纳AI建议", "ai_suggestion": {"score": 28}})
    check("维度1 暂存 DRAFT", r.status_code == 200 and r.json()["status"] == "DRAFT", f"{r.status_code} {r.text[:100]}")
    # 暂存可反复保存
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/reviews/{rid1}/score", headers=headers,
                             json={"score": 27, "comment": "调整一次", "ai_suggestion": {"score": 28}})
    check("DRAFT 可再次保存", r.status_code == 200 and r.json()["score"] == "27.00", f"{r.text[:80]}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews/{rid1}/submit", headers=headers)
    check("维度1 提交 → MANUAL_ADJUSTED（27≠28）", r.status_code == 200 and r.json()["status"] == "MANUAL_ADJUSTED",
          f"{r.status_code} {r.text[:100]}")
    # 锁定后不可回改
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/reviews/{rid1}/score", headers=headers,
                             json={"score": 10, "comment": "想改"},
                             )
    check("锁定后保存 → 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

    # 维度2：不带 ai_suggestion → CONFIRMED
    print("\n[维度2] 提交 CONFIRMED（未手动调整）")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews", headers=headers,
                              json={"bid_id": bid_id, "dimension_id": d2.dimension_id})
    rid2 = r.json()["review_id"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/reviews/{rid2}/score", headers=headers,
                             json={"score": 20, "comment": "无AI建议", "ai_suggestion": None})
        r = await client.post(f"{BASE}/reviews/{rid2}/submit", headers=headers)
    check("维度2 提交 → CONFIRMED", r.status_code == 200 and r.json()["status"] == "CONFIRMED",
          f"{r.status_code} {r.text[:100]}")
    # 重复提交幂等
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews/{rid2}/submit", headers=headers)
    check("重复提交幂等 200", r.status_code == 200, f"{r.status_code}")

    # DB 层面确认：全部维度锁定
    async with engine.connect() as conn:
        statuses = (await conn.execute(text(
            "SELECT dimension_id, status FROM expert_review WHERE bid_id=:b ORDER BY dimension_id"), {"b": bid_id}
        )).all()
    locked = all(s in ("CONFIRMED", "MANUAL_ADJUSTED") for _, s in statuses)
    check(f"所有评审统一锁定（{len(statuses)} 条）", locked and len(statuses) == 2, str(statuses))

    await cleanup(engine, bid_id)
    print("\n[cleanup] 验收残留已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
