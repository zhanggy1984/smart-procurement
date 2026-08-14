"""P1.6 Outbox 事件系统验收脚本。

覆盖 task.md P1.6 验收：
- 创建 Expert → outbox_event INSERT（PENDING）→ arq worker 消费 → Neo4j 自动出现节点
- FAILED 记录 reconciliation 重放 → PROCESSED + Neo4j 节点出现（MERGE 幂等）
- worker 自动清历史 PENDING 积压（P1.3/P1.4 直同步留下的未消费事件）

验证方式（真实链路）：
- 构造"直同步失败"场景：INSERT expert + outbox 事件，**不直同步 Neo4j**
- 用 arq `enqueue_job` 触发 worker 消费/reconcile（cron 每分钟兜底，验收用即时触发）
- 轮询事件状态直到 PROCESSED，断言 Neo4j 节点出现

前置：本机已起 arq worker（poetry run arq app.tasks.worker.WorkerSettings）；
MySQL/Neo4j/Redis 可达（sp-* 容器已起）。
用法: poetry run python scripts/accept_p16_api.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from neo4j import GraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402

PASS = 0
FAIL = 0

TEST_EXPERTS = ("EXP-ACC16", "EXP-ACC17")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def neo4j_count(expert_id: str) -> int:
    with GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)) as driver:
        with driver.session() as session:
            return session.run("MATCH (e:Expert {expertId:$id}) RETURN count(e)", id=expert_id).single()[0]


async def db() -> "AsyncEngine":
    return create_async_engine(settings.database_url)


async def enqueue(task: str) -> None:
    """向 arq worker 投递任务（Redis 队列）。"""
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job(task)
    finally:
        await pool.aclose()


async def event_status(engine, aggregate_id: str) -> str | None:
    async with engine.begin() as conn:
        return (await conn.execute(
            text("SELECT status FROM outbox_event WHERE aggregate_id=:a ORDER BY id DESC LIMIT 1"),
            {"a": aggregate_id},
        )).scalar_one_or_none()


async def enqueue_until(engine, task: str, aggregate_id: str, want: str, timeout: float = 120) -> bool:
    """反复投递任务 + 轮询直到事件到达期望状态。

    arq enqueue pickup 延迟实测不稳定（20-40s），单次投递会偶发超时。
    反复投递保证 worker 迟早执行；事件已处理则重复投递无副作用
    （handler 幂等，SKIP LOCKED 不重复消费）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await event_status(engine, aggregate_id) == want:
            return True
        await enqueue(task)
        await asyncio.sleep(5)
    return False


async def cleanup(engine, driver) -> None:
    """清验收残留（幂等）：expert + outbox + Neo4j 节点。"""
    ids = ("EXP-ACC16", "EXP-ACC17", "EXP-DIAG")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_event WHERE aggregate_id IN ('EXP-ACC16','EXP-ACC17','EXP-DIAG')"))
        await conn.execute(text("DELETE FROM expert WHERE expert_id IN ('EXP-ACC16','EXP-ACC17','EXP-DIAG')"))
    with driver.session() as session:
        session.run("MATCH (e:Expert) WHERE e.expertId IN ['EXP-ACC16','EXP-ACC17','EXP-DIAG'] DETACH DELETE e")


async def main() -> None:
    global PASS, FAIL
    engine = await db()
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))

    # 幂等：清上次残留
    await cleanup(engine, driver)
    print("[cleanup] 验收前残留已清理")

    # 前置检查：Redis 可达（worker 依赖）
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await pool.ping()
        await pool.aclose()
        check("Redis 可达", True)
    except Exception as e:  # noqa: BLE001
        check("Redis 可达", False, str(e))
        await engine.dispose()
        driver.close()
        sys.exit(1)

    async with engine.begin() as conn:
        pending_before = (await conn.execute(text("SELECT COUNT(*) FROM outbox_event WHERE status='PENDING'"))).scalar_one()
    print(f"  [观察] 初始 PENDING 积压: {pending_before}（已清空属正常，首次验收会有 P1.3/P1.4 遗留）")

    # ==================== 场景1：直同步失败 → worker 兜底补节点 ====================
    print("\n[场景1] 创建 Expert（直同步失败）→ outbox → worker 消费 → Neo4j 节点")
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO expert (expert_id, name, organization, region, status, created_at, updated_at) "
            "VALUES ('EXP-ACC16', '验收专家16', '验收单位A', '华中', 'ACTIVE', NOW(), NOW())"
        ))
        await conn.execute(text(
            "INSERT INTO outbox_event (aggregate_id, event_type, payload, status) "
            "VALUES ('EXP-ACC16', 'EXPERT_CREATED', :payload, 'PENDING')"
        ), {"payload": json.dumps({"expert_id": "EXP-ACC16", "name": "验收专家16"})})
    check("outbox 事件 PENDING 落库（不直同步）", await event_status(engine, "EXP-ACC16") == "PENDING")

    ok = await enqueue_until(engine, "consume_outbox", "EXP-ACC16", "PROCESSED")
    if not ok:
        print(f"  [诊断] EXP-ACC16 最终状态: {await event_status(engine, 'EXP-ACC16')}，原因见 worker 日志")
    check("worker 消费 → 事件 PROCESSED", ok)
    check("Neo4j 自动出现 EXP-ACC16 节点", neo4j_count("EXP-ACC16") == 1,
          f"count={neo4j_count('EXP-ACC16')}")

    # ==================== 场景2：FAILED → reconciliation 重放 ====================
    print("\n[场景2] FAILED 事件 → reconciliation 重放 → PROCESSED + Neo4j 节点")
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO expert (expert_id, name, organization, region, status, created_at, updated_at) "
            "VALUES ('EXP-ACC17', '验收专家17', '验收单位B', '华东', 'ACTIVE', NOW(), NOW())"
        ))
        await conn.execute(text(
            "INSERT INTO outbox_event (aggregate_id, event_type, payload, status) "
            "VALUES ('EXP-ACC17', 'EXPERT_CREATED', :payload, 'FAILED')"
        ), {"payload": json.dumps({"expert_id": "EXP-ACC17", "name": "验收专家17"})})
    check("FAILED 事件已构造", await event_status(engine, "EXP-ACC17") == "FAILED")

    ok = await enqueue_until(engine, "reconcile_outbox", "EXP-ACC17", "PROCESSED")
    check("reconciliation 重放 → PROCESSED", ok)
    check("Neo4j 出现 EXP-ACC17 节点", neo4j_count("EXP-ACC17") == 1,
          f"count={neo4j_count('EXP-ACC17')}")

    # ==================== 场景3：worker 清历史积压 ====================
    print("\n[场景3] worker 自动清 PENDING 积压（每批 50，多轮消费）")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        async with engine.begin() as conn:
            pending_now = (await conn.execute(text("SELECT COUNT(*) FROM outbox_event WHERE status='PENDING'"))).scalar_one()
        if pending_now == 0:
            break
        await enqueue("consume_outbox")
        await asyncio.sleep(3)
    check("PENDING 积压清空（worker 自动消费）", pending_now == 0, f"remaining={pending_now}")

    # ==================== 清理 ====================
    await cleanup(engine, driver)
    print("\n[cleanup] 验收残留已清理（expert + outbox + Neo4j 节点）")
    driver.close()
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
