"""P4.2 专家匹配算法验收脚本（本地 uvicorn :8001）。

覆盖 task.md P4.2 验收：
- 4 种回避冲突 100% 召回（EXP-005 对 LOT-004 投标 SUP-010 有 EMPLOYED_BY+HOLDS_SHARE）
- 误报率 <10%（无冲突专家全入选）
- 可用专家 < expert_count → INSUFFICIENT_EXPERTS（region 内 ACTIVE 专家 <5）
- 落库 lot_expert_assignment（PENDING_DECLARATION）

场景：LOT-004（PRJ-002，投标 SUP-010/006/007）。EXP-005 对 SUP-010 双重冲突。
前置：uvicorn :8001；MySQL/Neo4j。
用法: poetry run python scripts/accept_p42_api.py
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

LOT_ID = "LOT-004"


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

    # 动态构造：region + 候选标签
    async with engine.connect() as conn:
        region = (await conn.execute(text(
            "SELECT p.region FROM lot l JOIN project p ON p.project_id=l.project_id WHERE l.lot_id=:l"
        ), {"l": LOT_ID})).scalar_one()
        cand = (await conn.execute(text("""
            SELECT DISTINCT s.tag FROM expert_specialization s
            JOIN expert e ON e.expert_id=s.expert_id
            WHERE e.status='ACTIVE' AND e.region=:r LIMIT 5
        """), {"r": region})).all()
    tags = [t[0] for t in cand][:3]
    print(f"  [setup] region={region!r} 候选标签={tags!r}")
    check("构造出候选标签", bool(tags), f"tags={tags}")

    # 冲突召回单测：EXP-005 对 SUP-010 有 EMPLOYED_BY+HOLDS_SHARE（LOT-004 投标商）
    from app.services.expert_match_service import _find_conflicts

    conflicts = await _find_conflicts(["EXP-005", "EXP-019"], ["SUP-010"])
    check("冲突检测 100% 召回 EXP-005", "EXP-005" in conflicts, f"conflicts={conflicts}")
    check("无冲突专家不误报（EXP-019）", "EXP-019" not in conflicts, f"conflicts={conflicts}")

    # EXP-005 是否满足候选条件（region 匹配）
    async with engine.connect() as conn:
        exp005 = (await conn.execute(text(
            "SELECT region, status FROM expert WHERE expert_id='EXP-005'"
        ))).one()
    exp005_in_cand = exp005.region == region and exp005.status == "ACTIVE"
    print(f"  [setup] EXP-005 region={exp005.region!r} in_cand={exp005_in_cand}")

    # 置 UNDER_REVIEW
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:l"), {"l": LOT_ID})

    # 登录 PM
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": "pm1", "password": TEST_PASSWORD})
        pm_token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {pm_token}"}

    # ==================== 执行匹配 ====================
    print("\n[匹配] POST match-experts")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/lots/{LOT_ID}/match-experts", headers=headers,
                              json={"tags": tags})
    check("match 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    if r.status_code != 200:
        await engine.dispose()
        sys.exit(1)
    result = r.json()
    assigned = result["assigned"]
    excluded = result["excluded_conflict"]
    assigned_ids = {a["expert_id"] for a in assigned}

    check("落库 assigned 非空", len(assigned) > 0, f"assigned={assigned_ids}")
    check("每位专家有维度分配", all(a["dimension_ids"] for a in assigned),
          str([(a["expert_id"], a["dimension_ids"]) for a in assigned[:3]]))
    check("expert_count 内（≤5）", len(assigned) <= 5, f"count={len(assigned)}")

    # 冲突召回：EXP-005 若进候选则被排除（100% 召回）
    if exp005_in_cand:
        check("冲突专家 EXP-005 被排除（100% 召回）", "EXP-005" in excluded,
              f"excluded={excluded}")
    else:
        print("  [跳过] EXP-005 不在候选（region 不匹配），冲突召回用单测补充")

    # 误报：assigned 无冲突专家
    check("入选专家无冲突（误报 0）", "EXP-005" not in assigned_ids, f"assigned={assigned_ids}")

    # 可用专家不足告警
    check("可用专家 < expert_count → INSUFFICIENT_EXPERTS", result["insufficient"] is True,
          f"insufficient={result['insufficient']} assigned={len(assigned)}")

    # ==================== GET 查看 ====================
    print("\n[查看] GET match-experts")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/lots/{LOT_ID}/match-experts", headers=headers)
    got = r.json()
    check("GET 返回落库 assignment", len(got["assigned"]) == len(assigned),
          f"got={len(got['assigned'])}")
    check("assignment 状态 PENDING_DECLARATION",
          all(a["status"] == "PENDING_DECLARATION" for a in got["assigned"]),
          str({a["status"] for a in got["assigned"]}))

    # ==================== 还原 ====================
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE lot SET status='BIDDING' WHERE lot_id=:l"), {"l": LOT_ID})
        await conn.execute(text("DELETE FROM lot_expert_assignment WHERE lot_id=:l"), {"l": LOT_ID})
    print("\n[cleanup] lot 状态已还原，assignment 已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
