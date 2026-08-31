"""P3.5 评审收尾 API 验收脚本（本地 uvicorn :8001 + arq worker）。

覆盖 task.md P3.5 验收：
- 标书解析 → FROZEN → 评审提交锁定 → lot=UNDER_REVIEW → complete-review → EVALUATED + 报告
- 报告下载 GET /lots/{id}/summary/report → PDF
- submit-for-award：项目全部 lot 终态 → AWARDED + 归档 job → expert_profile 更新
- 错误路径：非 UNDER_REVIEW lot complete → 400

前置：uvicorn :8001 + 本机 arq worker；bge-m3；MySQL/Neo4j/Milvus/Redis。
用法: poetry run python scripts/accept_p35_api.py
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
             "第一章 技术方案", "我方采用微服务架构、容器化部署等关键技术路线。"]
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


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)

    # 记录并构造状态
    async with engine.begin() as conn:
        proj_id = (await conn.execute(text("SELECT project_id FROM lot WHERE lot_id=:l"), {"l": LOT_ID})).scalar_one()
        all_lots = (await conn.execute(text("SELECT lot_id, status FROM lot WHERE project_id=:p"), {"p": proj_id})).all()
        orig_statuses = {r.lot_id: r.status for r in all_lots}
    print(f"  [setup] {LOT_ID} 属于 {proj_id}，其 lot: {orig_statuses}")

    admin = await login("admin")
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
    check("标书 PARSED", r.json()["status"] == "PARSED")

    async with engine.begin() as conn:
        await conn.execute(text("UPDATE bid_document SET status='FROZEN' WHERE bid_id=:b"), {"b": bid_id})
        await conn.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:l"), {"l": LOT_ID})

    # 评审提交锁定
    async with engine.connect() as conn:
        dim = (await conn.execute(text(
            "SELECT dimension_id FROM scoring_dimension WHERE lot_id=:l AND name='技术方案'"), {"l": LOT_ID}
        )).scalar_one()
    # 评审归属校验前置（P4.2 分配）：EXP-001 分配技术方案维度，否则 create_review 403
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO lot_expert_assignment (lot_id, expert_id, dimension_ids, status) "
                 "VALUES (:lot, :exp, :dims, 'PENDING_DECLARATION')"),
            {"lot": LOT_ID, "exp": "EXP-001", "dims": json.dumps([dim])},
        )
    print("  [setup] 专家-标段分配已前置（评审归属校验）")
    expert = await login("expert_01")
    eheaders = {"Authorization": f"Bearer {expert}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews", headers=eheaders,
                              json={"bid_id": bid_id, "dimension_id": dim})
    rid = r.json()["review_id"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        await client.put(f"{BASE}/reviews/{rid}/score", headers=eheaders,
                         json={"score": 25, "comment": "评审完成", "ai_suggestion": None})
        await client.post(f"{BASE}/reviews/{rid}/submit", headers=eheaders)
    print("  [setup] 评审已提交锁定")

    # ==================== complete-review ====================
    print("\n[complete-review]")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/lots/{LOT_ID}/complete-review", headers={"Authorization": f"Bearer {admin}"})
    check("complete-review 200 + EVALUATED", r.status_code == 200 and r.json()["status"] == "EVALUATED",
          f"{r.status_code} {r.text[:150]}")
    # 错误路径：非 UNDER_REVIEW lot → 400
    async with httpx.AsyncClient(timeout=60.0) as client:
        r2 = await client.post(f"{BASE}/lots/LOT-005/complete-review", headers={"Authorization": f"Bearer {admin}"})
    check("非 UNDER_REVIEW lot → 400", r2.status_code == 400, f"{r2.status_code}")

    # ==================== 报告下载 ====================
    print("\n[报告]")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r3 = await client.get(f"{BASE}/lots/{LOT_ID}/summary/report", headers={"Authorization": f"Bearer {admin}"})
    check("报告下载 200 + PDF", r3.status_code == 200 and r3.content[:4] == b"%PDF",
          f"{r3.status_code} head={r3.content[:5]!r}")

    # ==================== submit-for-award ====================
    print("\n[submit-for-award]")
    # 先测未完成 → 400
    async with httpx.AsyncClient(timeout=60.0) as client:
        r4 = await client.post(f"{BASE}/projects/{proj_id}/submit-for-award", headers={"Authorization": f"Bearer {admin}"})
    check("项目有未完成 lot → 400", r4.status_code == 400, f"{r4.status_code} {r4.text[:100]}")
    # 其他 lot 置终态 → AWARDED + 归档
    async with engine.begin() as conn:
        for lid, st in orig_statuses.items():
            if lid != LOT_ID:
                await conn.execute(text("UPDATE lot SET status='ABANDONED' WHERE lot_id=:l"), {"l": lid})
    async with httpx.AsyncClient(timeout=60.0) as client:
        r5 = await client.post(f"{BASE}/projects/{proj_id}/submit-for-award", headers={"Authorization": f"Bearer {admin}"})
    check("submit-for-award 200 + AWARDED", r5.status_code == 200 and r5.json()["status"] == "AWARDED",
          f"{r5.status_code} {r5.text[:150]}")

    # ==================== 归档 job → expert_profile ====================
    print("\n[归档] expert_profile 更新")
    async with engine.connect() as conn:
        exp = (await conn.execute(text(
            "SELECT display_name FROM users WHERE username='expert_01'"), {})).scalar_one()
        expert_ent = (await conn.execute(text(
            "SELECT expert_id FROM expert WHERE name=:n"), {"n": exp})).scalar_one()
    deadline = time.monotonic() + 90
    profile_ok = False
    while time.monotonic() < deadline:
        async with engine.connect() as conn:
            tr = (await conn.execute(text(
                "SELECT total_reviews FROM expert_profile WHERE expert_id=:e"), {"e": expert_ent}
            )).scalar_one_or_none()
        if tr is not None and tr >= 1:
            profile_ok = True
            break
        await asyncio.sleep(3)
    check(f"expert_profile.total_reviews 更新（{expert_ent} ≥1）", profile_ok, f"total={tr}")

    # ==================== 清理：还原 lot 状态 + 删测试数据 ====================
    async with engine.begin() as conn:
        for lid, st in orig_statuses.items():
            await conn.execute(text("UPDATE lot SET status=:s WHERE lot_id=:l"), {"s": st, "l": lid})
        rev_rows = (await conn.execute(text("SELECT review_id FROM expert_review WHERE bid_id=:b"), {"b": bid_id})).all()
        rev_ids = tuple(r[0] for r in rev_rows)
        if rev_ids:
            await conn.execute(text("DELETE FROM conversation_message WHERE review_id IN :ids"), {"ids": rev_ids})
        await conn.execute(text("DELETE FROM expert_review WHERE bid_id=:b"), {"b": bid_id})
        await conn.execute(
            text("DELETE FROM lot_expert_assignment WHERE lot_id=:lot AND expert_id='EXP-001'"),
            {"lot": LOT_ID},
        )
        await conn.execute(text("DELETE FROM bid_document WHERE bid_id=:b"), {"b": bid_id})
    remove_prefix(get_minio_client(), f"bids/{LOT_ID}/")
    print("\n[cleanup] 状态已还原，验收数据已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
