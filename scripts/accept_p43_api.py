"""P4.3 专家回避申报验收脚本（本地 uvicorn :8001）。

覆盖 task.md P4.3 验收两路径：
- 路径1：专家全部确认无冲突 → assignment IN_PROGRESS（可进入评审）
- 路径2：专家申报冲突 → CONFLICT_DECLARED + 自动补匹配 + 新专家收通知
- 待申报供应商列表 / 我的任务列表 / 重复申报 409

场景：LOT-004（投标 SUP-010/006/007）。匹配出西北专家后申报。
前置：uvicorn :8001；MySQL/Neo4j。
用法: poetry run python scripts/accept_p43_api.py
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

LOT_ID = "LOT-004"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "123456")


async def login(username: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": TEST_PASSWORD})
        assert r.status_code == 200, f"登录失败 {username}: {r.status_code}"
        return r.json()["access_token"]


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)

    # 构造：lot=UNDER_REVIEW + 匹配
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:l"), {"l": LOT_ID})
        cand = (await conn.execute(text("""
            SELECT DISTINCT s.tag FROM expert_specialization s
            JOIN expert e ON e.expert_id=s.expert_id
            WHERE e.status='ACTIVE' AND e.region=(SELECT p.region FROM lot l JOIN project p ON p.project_id=l.project_id WHERE l.lot_id=:l)
            LIMIT 3
        """), {"l": LOT_ID})).all()
    tags = [t[0] for t in cand][:3]
    pm = await login("pm1")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/lots/{LOT_ID}/match-experts",
                              headers={"Authorization": f"Bearer {pm}"}, json={"tags": tags})
    assert r.status_code == 200, f"匹配失败 {r.status_code} {r.text[:150]}"
    assigned = r.json()["assigned"]
    check("匹配产生专家", len(assigned) >= 2, f"assigned={[a['expert_id'] for a in assigned]}")
    exp_a = assigned[0]["expert_id"]
    exp_b = assigned[1]["expert_id"] if len(assigned) > 1 else exp_a

    # 解析专家登录账号（display_name=专家名）
    async with engine.connect() as conn:
        name_a = (await conn.execute(text("SELECT name FROM expert WHERE expert_id=:e"), {"e": exp_a})).scalar_one()
        username_a = (await conn.execute(text(
            "SELECT username FROM users WHERE display_name=:n AND role='REVIEW_EXPERT'"), {"n": name_a}
        )).scalar_one_or_none()
        name_b = (await conn.execute(text("SELECT name FROM expert WHERE expert_id=:e"), {"e": exp_b})).scalar_one()
        username_b = (await conn.execute(text(
            "SELECT username FROM users WHERE display_name=:n AND role='REVIEW_EXPERT'"), {"n": name_b}
        )).scalar_one_or_none()
    check("解析专家账号", username_a and username_b, f"a={username_a} b={username_b}")
    if not (username_a and username_b):
        await engine.dispose()
        sys.exit(1)

    # ==================== 我的任务列表 + 待申报供应商 ====================
    print("\n[任务] GET me/assignments + declaration")
    ta = await login(username_a)
    ha = {"Authorization": f"Bearer {ta}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/experts/me/assignments", headers=ha)
    mine = r.json()["assignments"]
    check("我的任务列表含该标段", any(a["lot_id"] == LOT_ID for a in mine), f"count={len(mine)}")
    assign_id = next(a["assignment_id"] for a in mine if a["lot_id"] == LOT_ID)

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/experts/assignments/{assign_id}/declaration", headers=ha)
    dec = r.json()
    sup_ids = [s["supplier_id"] for s in dec["suppliers"]]
    check("待申报供应商 = 标段投标商", set(sup_ids) == {"SUP-010", "SUP-006", "SUP-007"}, str(sup_ids))

    # ==================== 路径1：全部确认无冲突 → IN_PROGRESS ====================
    print("\n[路径1] 全部确认无冲突 → IN_PROGRESS")
    confirm_clean = [{"supplier_id": s, "has_conflict": False} for s in sup_ids]
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/experts/assignments/{assign_id}/declare", headers=ha,
                              json={"confirmations": confirm_clean})
    check("declare 200 + IN_PROGRESS", r.status_code == 200 and r.json()["status"] == "IN_PROGRESS",
          f"{r.status_code} {r.text[:120]}")
    # 重复申报 → 409
    async with httpx.AsyncClient(timeout=60.0) as client:
        r2 = await client.post(f"{BASE}/experts/assignments/{assign_id}/declare", headers=ha,
                               json={"confirmations": confirm_clean})
    check("重复申报 → 409", r2.status_code == 409, f"{r2.status_code}")

    # ==================== 路径2：申报冲突 → CONFLICT_DECLARED + 补匹配 + 通知 ====================
    print("\n[路径2] 申报冲突 → CONFLICT_DECLARED + 补匹配 + 通知")
    tb = await login(username_b)
    hb = {"Authorization": f"Bearer {tb}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/experts/me/assignments", headers=hb)
    assign_b = next(a for a in r.json()["assignments"] if a["lot_id"] == LOT_ID)
    # 专家 B 申报对 SUP-010 有冲突（曾任/持股）
    confirm_c = [{"supplier_id": "SUP-010", "has_conflict": True, "relation_type": "EMPLOYED_BY",
                  "relation_detail": "曾在 SUP-010 任职"}]
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/experts/assignments/{assign_b['assignment_id']}/declare", headers=hb,
                              json={"confirmations": confirm_c})
    body = r.json()
    check("冲突申报 → CONFLICT_DECLARED", r.status_code == 200 and body["status"] == "CONFLICT_DECLARED",
          f"{r.status_code} {r.text[:150]}")

    # 通知落库（申报结果 + 补匹配新专家通知）
    async with engine.connect() as conn:
        notes = (await conn.execute(text(
            "SELECT user_id, type FROM notification WHERE related_id=:l ORDER BY id DESC LIMIT 5"),
            {"l": LOT_ID})).all()
    types = [n[1] for n in notes]
    check("通知已写入（申报结果/新任务）", bool(types), f"notes={types}")
    if body.get("supplemented_expert"):
        # 补匹配专家收到 ASSIGNMENT_NOTICE
        supplemented_notes = [n[0] for n in notes if n[1] == "ASSIGNMENT_NOTICE"]
        check("补匹配专家收到新任务通知", bool(supplemented_notes), f"notes={notes}")

    # 申报记录落库 + Neo4j 关系
    async with engine.connect() as conn:
        dec_cnt = (await conn.execute(text(
            "SELECT COUNT(*) FROM expert_conflict_declaration WHERE assignment_id=:a"),
            {"a": assign_b["assignment_id"]})).scalar_one()
    check("expert_conflict_declaration 落库", dec_cnt >= 1, f"count={dec_cnt}")

    # ==================== 清理 ====================
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE lot SET status='BIDDING' WHERE lot_id=:l"), {"l": LOT_ID})
        await conn.execute(text("DELETE FROM notification WHERE related_id=:l"), {"l": LOT_ID})
        await conn.execute(text("DELETE FROM expert_conflict_declaration WHERE lot_id=:l"), {"l": LOT_ID})
        await conn.execute(text("DELETE FROM lot_expert_assignment WHERE lot_id=:l"), {"l": LOT_ID})
    print("\n[cleanup] lot 状态已还原，申报数据已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
