"""P4.4 站内信通知系统验收脚本（本地 uvicorn :8001）。

覆盖 task.md P4.4 验收：
- 通知生成（service.send 模拟业务触发）→ 未读数 +N
- GET /notifications 分页查询 + 未读数
- PUT /notifications/{id}/read 单条已读
- PUT /notifications/read-all 全部已读 → 未读归零
- 非本人标记已读 → 404

前置：uvicorn :8001；MySQL。
用法: poetry run python scripts/accept_p44_api.py
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
from app.core.database import session_factory  # noqa: E402

BASE = "http://localhost:8001/api/v1"
PASS = 0
FAIL = 0


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
        assert r.status_code == 200, f"登录失败 {username}"
        return r.json()["access_token"]


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)

    # 清测试通知
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM notification WHERE related_id LIKE 'ACC44%'"))
    print("[cleanup] 测试通知已清理")

    # 专家账号 user_id
    async with engine.connect() as conn:
        uid = (await conn.execute(text(
            "SELECT user_id FROM users WHERE username='expert_01'"))).scalar_one()
    print(f"  [setup] expert_01 user_id={uid}")

    # 模拟业务触发：写 3 条通知（分配/申报/告警类型）
    from app.services import notification_service as ns

    async with session_factory() as session:
        for i, (typ, title) in enumerate([("ASSIGNMENT_NOTICE", "新的评审任务"),
                                          ("DECLARATION_RESULT", "回避申报完成"),
                                          ("REVIEW_REMIND", "评审进度提醒")], start=1):
            await ns.send(session, user_id=uid, type=typ, title=title,
                          content=f"测试通知 {i}", related_id=f"ACC44-{i}")

    token = await login("expert_01")
    headers = {"Authorization": f"Bearer {token}"}

    # 未读数
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/notifications/unread-count", headers=headers)
    check("未读数 = 3", r.json()["unread_count"] == 3, str(r.json()))

    # 分页查询
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/notifications?page=1&page_size=10", headers=headers)
    body = r.json()
    check("GET /notifications 返回 3 条", len(body["notifications"]) == 3, f"n={len(body['notifications'])}")
    check("查询带 unread_count=3", body["unread_count"] == 3, str(body["unread_count"]))
    first_id = body["notifications"][0]["id"]
    check("通知含 title/type", body["notifications"][0]["title"] == "新的评审任务",
          str(body["notifications"][0]))

    # 未读筛选
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/notifications?unread_only=true", headers=headers)
    check("unread_only=3", len(r.json()["notifications"]) == 3, f"n={len(r.json()['notifications'])}")

    # 单条已读
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/notifications/{first_id}/read", headers=headers)
    check("单条已读 200", r.status_code == 200 and r.json()["is_read"] is True, f"{r.status_code}")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/notifications/unread-count", headers=headers)
    check("已读后未读=2", r.json()["unread_count"] == 2, str(r.json()))

    # 非本人标记已读 → 404
    other = await login("expert_02")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/notifications/{first_id}/read",
                             headers={"Authorization": f"Bearer {other}"})
    check("非本人标记已读 → 404", r.status_code == 404, f"{r.status_code}")

    # 全部已读
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.put(f"{BASE}/notifications/read-all", headers=headers)
    check("read-all 更新 ≥2", r.json()["updated"] >= 2, str(r.json()))
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{BASE}/notifications/unread-count", headers=headers)
    check("全部已读 → 未读归零", r.json()["unread_count"] == 0, str(r.json()))

    # 清理
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM notification WHERE related_id LIKE 'ACC44%'"))
    print("\n[cleanup] 测试通知已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
