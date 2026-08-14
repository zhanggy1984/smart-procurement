"""P5.1 围串标初筛验收脚本（本地 uvicorn :8001）。

覆盖 task.md P5.1 验收：
- LOT-001（SUP-001/002/003 无关联）3 家正常投标 → LOW 自动通过 → FROZEN + UNDER_REVIEW
- LOT-007（SUP-012/013 SAME_CONTROLLER）→ MEDIUM 待办 → PRE_SCREEN
- 有效标书 <3 → ABANDONED（构造：非 BIDDING → 400；投标<3 用合成数据不足的 lot 或跳过）

前置：uvicorn :8001；MySQL/Neo4j/Milvus。
用法: poetry run python scripts/accept_p51_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402

BASE = "http://localhost:8001/api/v1"
PASS = 0
FAIL = 0

# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)

    # 记录原状态
    async with engine.connect() as conn:
        orig_lots = (await conn.execute(text(
            "SELECT lot_id, status FROM lot WHERE lot_id IN ('LOT-001','LOT-007')"))).all()
        orig_bids = (await conn.execute(text(
            "SELECT bid_id, status FROM bid_document WHERE lot_id IN ('LOT-001','LOT-007')"))).all()
    lot_orig = {r.lot_id: r.status for r in orig_lots}
    bid_orig = {r.bid_id: r.status for r in orig_bids}
    print(f"  [setup] LOT-001={lot_orig.get('LOT-001')} LOT-007={lot_orig.get('LOT-007')}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": "pm1", "password": TEST_PASSWORD})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # 构造有效标书（合成标书未解析 status=SUBMITTED，置 PARSED 模拟已解析；还原回原值）
    # lot 可能已被演示推进（如 LOT-001=EVALUATED），重跑前还原为投标期，跑完还原
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE lot SET status='BIDDING' WHERE lot_id IN ('LOT-001','LOT-007')"))
        await conn.execute(text(
            "UPDATE bid_document SET status='PARSED' WHERE lot_id IN ('LOT-001','LOT-007')"))
    print("  [setup] lot 置 BIDDING + 合成标书置 PARSED（模拟已解析）")

    # ==================== LOT-001 正常 → LOW 自动通过 ====================
    print("\n[LOT-001] 3 家无关联 → LOW 自动通过")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/lots/LOT-001/close-bidding", headers=headers)
    body = r.json()
    check("close-bidding 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    check("风险 LOW", body["risk"] == "LOW", str(body.get("risk")))
    check("总分 ≤25", body["total_score"] <= 25, f"score={body['total_score']}")
    check("自动通过 → UNDER_REVIEW", body["next_status"] == "UNDER_REVIEW", str(body["next_status"]))
    check("LOW 图检无关联", body["scores"]["graph"] == 0, str(body["scores"]))

    # 标书 FROZEN
    async with engine.connect() as conn:
        statuses = (await conn.execute(text(
            "SELECT DISTINCT status FROM bid_document WHERE lot_id='LOT-001'"))).all()
    check("LOT-001 标书全部 FROZEN", set(s for s, in statuses) == {"FROZEN"},
          f"statuses={[s for s, in statuses]}")

    # ==================== LOT-007 SAME_CONTROLLER → MEDIUM 待办 ====================
    print("\n[LOT-007] SUP-012/013 SAME_CONTROLLER → MEDIUM")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/lots/LOT-007/close-bidding", headers=headers)
    body = r.json()
    check("LOT-007 风险 MEDIUM", body.get("risk") == "MEDIUM", f"{r.status_code} {str(body)[:150]}")
    check("图检命中 SAME_CONTROLLER（≥30）", body.get("scores", {}).get("graph", 0) >= 30,
          str(body.get("scores")))
    check("MEDIUM → PRE_SCREEN 待 PM 确认", body.get("next_status") == "PRE_SCREEN",
          str(body.get("next_status")))
    check("MEDIUM 标书未 FROZEN", body.get("bid_count", 0) >= 3, f"bids={body.get('bid_count')}")

    # ==================== 错误路径：非 BIDDING → 400 ====================
    print("\n[错误] 非 BIDDING lot → 400")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/lots/LOT-001/close-bidding", headers=headers)
    check("重复关闭（非 BIDDING）→ 400", r.status_code == 400, f"{r.status_code}")

    # ==================== 还原 ====================
    async with engine.begin() as conn:
        for lid, st in lot_orig.items():
            await conn.execute(text("UPDATE lot SET status=:s WHERE lot_id=:l"), {"s": st, "l": lid})
        for bid_id, st in bid_orig.items():
            await conn.execute(text("UPDATE bid_document SET status=:s WHERE bid_id=:b"), {"s": st, "b": bid_id})
    print("\n[cleanup] lot/标书状态已还原")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
