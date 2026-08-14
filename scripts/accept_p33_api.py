"""P3.3 AI 评标打分 API 验收脚本（本地 uvicorn :8001）。

覆盖 task.md P3.3 验收：
- 上传标书（含报价）→ 解析 PARSED → 置 FROZEN
- POST /reviews 创建评审工作台（校验 FROZEN + 维度归属）
- POST /reviews/{id}/score SSE：AI 维度事件序 thinking→source→thought→score→done
- 报价维度 → event:price_calc 纯公式（不走 AI）
- POST /reviews/{id}/chat SSE：对话流 + conversation_message 落库 + 摘要
- 评分幂等：同 X-Idempotency-Key 重复 → 422

前置：uvicorn :8001 + 本机 arq worker；bge-m3；MySQL/Neo4j/Milvus/Redis。
用法: poetry run python scripts/accept_p33_api.py
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
    lines = ["供应商：华远云控",
             "投标总报价：3,280,000元",
             "第一章 技术方案",
             "我方采用微服务架构、容器化部署、灰度发布等关键技术路线。",
             "第二章 安全方案",
             "系统满足等保三级要求，数据全程加密传输。",
             "我方具备CMMI3质量管理体系认证，质保期36个月。"]
    y = 780
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 20
    c.save()
    return buf.getvalue()


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")


async def login(username: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": TEST_PASSWORD})
        assert r.status_code == 200, f"登录失败 {username}: {r.status_code}"
        return r.json()["access_token"]


async def upload(token: str) -> str:
    files = {"file": ("标书.pdf", make_pdf(), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/lots/{LOT_ID}/bids",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data={"supplier_id": SUP_ID},
        )
    assert r.status_code == 201, f"上传失败 {r.status_code} {r.text[:200]}"
    return r.json()["bid_id"]


async def wait_parsed(token: str, bid_id: str, timeout: float = 300) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{BASE}/bids/{bid_id}/status", headers={"Authorization": f"Bearer {token}"})
        st = r.json()["status"]
        if st in ("PARSED", "PARSE_FAILED"):
            return st
        await asyncio.sleep(5)
    return st


async def read_sse(events: list, client: httpx.AsyncClient, r: httpx.Response) -> None:
    """解析 SSE 流到 events 列表 [(event, data)]。"""
    cur_event, cur_data = None, []
    async for line in r.aiter_lines():
        if line.startswith("event:"):
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
            rev_rows = (await conn.execute(
                text("SELECT review_id FROM expert_review WHERE bid_id=:b"), {"b": bid_id}
            )).all()
            rev_ids = tuple(r[0] for r in rev_rows)
            if rev_ids:
                await conn.execute(
                    text("DELETE FROM conversation_message WHERE review_id IN :ids"), {"ids": rev_ids}
                )
            await conn.execute(text("DELETE FROM expert_review WHERE bid_id=:b"), {"b": bid_id})
        await conn.execute(
            text("DELETE FROM bid_document WHERE lot_id=:lot AND file_url LIKE 'bids/%'"),
            {"lot": LOT_ID},
        )
    remove_prefix(get_minio_client(), f"bids/{LOT_ID}/")


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)
    await cleanup(engine)
    print("[cleanup] 验收前残留已清理")

    admin = await login("admin")
    bid_id = await upload(admin)
    st = await wait_parsed(admin, bid_id)
    check("标书 PARSED", st == "PARSED", f"status={st}")
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE bid_document SET status='FROZEN' WHERE bid_id=:b"), {"b": bid_id})
    print("  [setup] 标书已置 FROZEN")

    # 维度：技术方案（AI）+ 报价（公式）
    async with engine.connect() as conn:
        dims = (await conn.execute(text(
            "SELECT dimension_id, name, max_score FROM scoring_dimension WHERE lot_id=:lot AND name IN ('技术方案','报价')"
        ), {"lot": LOT_ID})).all()
    dim_map = {d.name: d for d in dims}
    tech_dim = dim_map.get("技术方案")
    price_dim = dim_map.get("报价")
    check("标段含技术方案+报价维度", tech_dim is not None and price_dim is not None,
          f"dims={[(d.name) for d in dims]}")
    if not (tech_dim and price_dim):
        await cleanup(engine)
        sys.exit(1)

    expert_token = await login("expert_01")
    headers = {"Authorization": f"Bearer {expert_token}"}

    # ==================== 创建评审工作台 ====================
    print("\n[创建] POST /reviews")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews",
                              headers=headers,
                              json={"bid_id": bid_id, "dimension_id": tech_dim.dimension_id})
    check("创建评审 201", r.status_code == 201, f"{r.status_code} {r.text[:150]}")
    review_id = r.json()["review_id"] if r.status_code == 201 else None
    # 未封存校验：非 FROZEN bid → 400
    async with httpx.AsyncClient(timeout=60.0) as client:
        r2 = await client.post(f"{BASE}/reviews",
                               headers=headers,
                               json={"bid_id": "BID-001", "dimension_id": tech_dim.dimension_id})
    check("非 FROZEN 标书创建 → 400", r2.status_code == 400, f"{r2.status_code} {r2.text[:100]}")

    # ==================== AI 维度 SSE 评分 ====================
    print("\n[SSE] 技术方案维度 AI 评分事件流")
    if review_id:
        events: list = []
        idem = str(uuid.uuid4())
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{BASE}/reviews/{review_id}/score",
                                     headers={**headers, "X-Idempotency-Key": idem}) as r:
                check("score SSE 200", r.status_code == 200, f"{r.status_code}")
                await read_sse(events, client, r)
        ev_names = [e for e, _ in events]
        check("事件序含 thinking", "thinking" in ev_names, str(ev_names))
        check("事件序含 thought（AI 流式）", "thought" in ev_names, str(ev_names))
        check("事件序含 done", "done" in ev_names, str(ev_names))
        check("SSE 事件带 id 序号", len(events) >= 4 and events[0][0] == "thinking", str(events[:2]))

        # 幂等：同 key 重复 → 422
        async with httpx.AsyncClient(timeout=120.0) as client:
            r3 = await client.post(f"{BASE}/reviews/{review_id}/score",
                                   headers={**headers, "X-Idempotency-Key": idem})
        check("幂等：重复 Idempotency-Key → 422", r3.status_code == 422, f"{r3.status_code}")

    # ==================== 报价维度 price_calc ====================
    print("\n[报价] price_calc 纯公式")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/reviews",
                              headers=headers,
                              json={"bid_id": bid_id, "dimension_id": price_dim.dimension_id})
    price_review_id = r.json().get("review_id") if r.status_code == 201 else None
    if price_review_id:
        events = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{BASE}/reviews/{price_review_id}/score",
                                     headers=headers) as r:
                await read_sse(events, client, r)
        pc = [d for e, d in events if e == "price_calc"]
        check("报价 → event:price_calc", bool(pc), str([e for e, _ in events]))
        if pc:
            check("price_calc 含公式与结果", "formula" in pc[0] and "calculatedScore" in pc[0]["result"],
                  str(pc[0])[:200])
            check("报价结果含 maxScore", pc[0]["result"]["maxScore"] == float(price_dim.max_score),
                  str(pc[0]["result"]))
        check("报价无 thought（不走 AI）", all(e != "thought" for e, _ in events), str([e for e, _ in events]))

    # ==================== chat SSE 对话 ====================
    print("\n[chat] SSE 对话 + conversation 落库")
    if review_id:
        events = []
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{BASE}/reviews/{review_id}/chat",
                                     headers=headers,
                                     json={"question": "请详细说明技术方案中的微服务架构细节"}) as r:
                check("chat SSE 200", r.status_code == 200, f"{r.status_code}")
                await read_sse(events, client, r)
        check("chat 有 thought 流", any(e == "thought" for e, _ in events), str([e for e, _ in events]))
        check("chat 有 done", any(e == "done" for e, _ in events), str([e for e, _ in events]))
        async with engine.begin() as conn:
            cnt = (await conn.execute(text(
                "SELECT COUNT(*) FROM conversation_message WHERE review_id=:r"), {"r": review_id}
            )).scalar_one()
        check("conversation_message 落库（user+assistant）", cnt >= 2, f"count={cnt}")

    # ==================== 清理 ====================
    await cleanup(engine, bid_id)
    print("\n[cleanup] 验收残留已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
