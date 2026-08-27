"""P3.6 SSE 完整实现验收脚本（本地 uvicorn :8001 + TestClient）。

覆盖 task.md P3.6 验收：
- X-Request-ID 中间件：不带头自动生成 + 响应头回传；带头透传
- SSE 断流续推：完整 score 流（报价维度）→ 记录 seq → Last-Event-ID 重连补发
- 断流缓存过期 → event:reset（前端全量重拉信号）
- 断路器 OPEN → score 端点 503（TestClient 同进程 monkeypatch）

前置：uvicorn :8001 + 本机 arq worker；bge-m3；MySQL/Neo4j/Milvus/Redis。
用法: poetry run python scripts/accept_p36_api.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import uuid

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
    lines = ["供应商：华远云控", "投标总报价：3,280,000元", "第一章 技术方案", "我方采用微服务架构等关键技术路线。"]
    y = 780
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 20
    c.save()
    return buf.getvalue()


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")


async def login(username: str, base: str = BASE) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{base}/auth/login", json={"username": username, "password": TEST_PASSWORD})
        assert r.status_code == 200, f"登录失败 {username}"
        return r.json()["access_token"]


async def read_sse(events: list, client: httpx.AsyncClient, r: httpx.Response) -> None:
    cur_event, cur_data = None, []
    async for line in r.aiter_lines():
        if line.startswith("id:"):
            events.append(("seq", int(line[3:].strip())))
        elif line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].strip())
        elif line == "" and cur_event:
            try:
                events.append((cur_event, json.loads("".join(cur_data)) if cur_data else {}))
            except json.JSONDecodeError:
                events.append((cur_event, {}))
            cur_event, cur_data = None, []


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

    # ==================== X-Request-ID 中间件（TestClient 同进程） ====================
    print("\n[X-Request-ID] 中间件")
    from starlette.testclient import TestClient

    import app.main as main_mod

    with TestClient(main_mod.app) as tc:
        r = tc.get("/health/live")
        rid_gen = r.headers.get("X-Request-ID")
        check("不带头自动生成并回传", bool(rid_gen), str(r.headers))
        r2 = tc.get("/health/live", headers={"X-Request-ID": "test-req-123"})
        check("带头透传同值", r2.headers.get("X-Request-ID") == "test-req-123",
              f"got={r2.headers.get('X-Request-ID')}")

        # ==================== 断路器 OPEN → 503 ====================
        print("\n[降级] 断路器 OPEN → score 503")
        from app.ai.llm.deepseek_client import get_client

        get_client()._circuit._state = "OPEN"
        tk = tc.post("/api/v1/auth/login", json={"username": "expert_01", "password": TEST_PASSWORD}).json()["access_token"]
        r3 = tc.post("/api/v1/reviews/REV-NOTEXIST/score", headers={"Authorization": f"Bearer {tk}"})
        check("断路器 OPEN → 503", r3.status_code == 503, f"{r3.status_code} {r3.text[:80]}")
        get_client()._circuit._state = "CLOSED"

    # ==================== SSE 断流续推（报价维度，真实 uvicorn） ====================
    print("\n[SSE 续推] 报价维度")
    admin = await login("admin")
    files = {"file": ("标书.pdf", make_pdf(), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/lots/{LOT_ID}/bids", headers={"Authorization": f"Bearer {admin}"},
                              files=files, data={"supplier_id": SUP_ID})
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
        price_dim = (await conn.execute(text(
            "SELECT dimension_id FROM scoring_dimension WHERE lot_id=:l AND name='报价'"), {"l": LOT_ID}
        )).scalar_one()
    # 评审归属校验前置（P4.2 分配）：EXP-001 分配报价维度，否则 create_review 403
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO lot_expert_assignment (lot_id, expert_id, dimension_ids, status) "
                 "VALUES (:lot, :exp, :dims, 'PENDING_DECLARATION')"),
            {"lot": LOT_ID, "exp": "EXP-001", "dims": json.dumps([price_dim])},
        )
    print("  [setup] 专家-标段分配已前置（评审归属校验）")
    expert = await login("expert_01")
    eheaders = {"Authorization": f"Bearer {expert}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews", headers=eheaders,
                              json={"bid_id": bid_id, "dimension_id": price_dim})
    rid = r.json()["review_id"]

    # 完整流（报价 3 帧：thinking/price_calc/done，seq 1/2/3）
    events_full: list = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", f"{BASE}/reviews/{rid}/score", headers=eheaders) as r:
            await read_sse(events_full, client, r)
    check("完整流含 3 帧", len(events_full) >= 6, f"events={events_full}")  # 3 事件 + 3 seq
    seqs = [d for e, d in events_full if e == "seq"]
    check("seq 递增 1,2,3", seqs == [1, 2, 3], f"seqs={seqs}")

    # 断流续推：Last-Event-ID=1 → 补发 seq>1（price_calc + done）
    events_resume: list = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", f"{BASE}/reviews/{rid}/score", headers={**eheaders, "Last-Event-ID": "1"}) as r:
            await read_sse(events_resume, client, r)
    resume_names = [e for e, _ in events_resume if e != "seq"]
    check("续推补发剩余事件（无 thinking）", "thinking" not in resume_names and "price_calc" in resume_names and "done" in resume_names,
          str(resume_names))
    check("续推无 reset（缓存命中）", "reset" not in resume_names, str(resume_names))

    # 缓存过期 → reset
    import redis.asyncio as aioredis
    r_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r_redis.delete(f"sse:cache:{rid}")
    await r_redis.aclose()
    events_reset: list = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", f"{BASE}/reviews/{rid}/score", headers={**eheaders, "Last-Event-ID": "2"}) as r:
            await read_sse(events_reset, client, r)
    reset_names = [e for e, _ in events_reset if e != "seq"]
    check("缓存过期 → event:reset", "reset" in reset_names, str(reset_names))

    await cleanup(engine, bid_id)
    print("\n[cleanup] 验收残留已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
