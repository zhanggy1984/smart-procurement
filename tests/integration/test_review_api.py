"""P7.3 评审 API 集成测试（task.md #10/#11/#12）。

成功：创建评审工作台（bid FROZEN）；报价维度 SSE 纯公式（thinking→price_calc→done）；
暂存/提交；追问 chat SSE。
错误：bid 未 FROZEN 400；维度不属标段 400；幂等 Key 重复 422；断路器 OPEN → 503。
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.core.database import session_factory


async def _close(client, pm_headers, lot_id):
    return await client.post(f"/api/v1/lots/{lot_id}/close-bidding", headers=pm_headers)


async def _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed, amounts=None):
    """lot + 3 投标 + PARSED + 金额 + close(LOW) → FROZEN + UNDER_REVIEW。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids)
    if amounts:
        async with session_factory() as s:
            for b, amt in zip(bids, amounts):
                await s.execute(text("UPDATE bid_document SET bid_amount=:a WHERE bid_id=:b"),
                                {"a": amt, "b": b})
            await s.commit()
    r = await _close(client, pm_headers, lot["lot_id"])
    assert r.status_code == 200, r.text
    assert r.json()["risk"] == "LOW"
    return lot, bids


async def _dim_id(client, pm_headers, lot_id, name):
    r = await client.get(f"/api/v1/lots/{lot_id}/dimensions", headers=pm_headers)
    assert r.status_code == 200
    return next(d["dimension_id"] for d in r.json()["items"] if d["name"] == name)


def _parse_sse(lines: list[str]) -> list[dict]:
    events, cur = [], {}
    for ln in lines:
        if ln.startswith("id: "):
            cur["id"] = int(ln[4:])
        elif ln.startswith("event: "):
            cur["event"] = ln[7:]
        elif ln.startswith("data: "):
            cur["data"] = json.loads(ln[6:])
        elif ln == "":
            events.append(cur)
            cur = {}
    if cur:
        events.append(cur)
    return events


# ==================== 创建评审工作台 ====================


@pytest.mark.asyncio
async def test_create_review_success(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """bid FROZEN + 维度归属匹配 → 201 DRAFT。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    resp = await client.post("/api/v1/reviews", headers=exp_headers,
                             json={"bid_id": bids[0], "dimension_id": price_dim})
    assert resp.status_code == 201
    assert resp.json()["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_create_review_bid_not_frozen_400(client, pm_headers, exp_headers, lot_factory, bid_factory):
    """bid 未 FROZEN（SUBMITTED）→ 400。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    resp = await client.post("/api/v1/reviews", headers=exp_headers,
                             json={"bid_id": bids[0], "dimension_id": price_dim})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_review_dimension_mismatch_400(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """维度不属该标段 → 400。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    other = await lot_factory()  # 另一个 lot 的维度
    other_dim = await _dim_id(client, pm_headers, other["lot_id"], "报价")
    resp = await client.post("/api/v1/reviews", headers=exp_headers,
                             json={"bid_id": bids[0], "dimension_id": other_dim})
    assert resp.status_code == 400


# ==================== SSE 报价评分 ====================


@pytest.mark.asyncio
async def test_stream_score_price_formula(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """报价维度 SSE：thinking→price_calc→done，公式可审计。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": price_dim})
    review_id = r.json()["review_id"]
    async with client.stream("POST", f"/api/v1/reviews/{review_id}/score", headers=exp_headers) as sr:
        assert sr.status_code == 200
        lines = [ln async for ln in sr.aiter_lines()]
    events = _parse_sse(lines)
    assert [e["event"] for e in events] == ["thinking", "price_calc", "done"]
    calc = next(e["data"]["result"] for e in events if e["event"] == "price_calc")
    # 基准价 = (100+120+80)/3 = 100，报价 100 → 满分
    assert calc["calculatedScore"] == 20.0
    assert calc["basePrice"] == 100.0
    assert [e["id"] for e in events] == [1, 2, 3]  # seq 递增（断流续推用）


@pytest.mark.asyncio
async def test_stream_score_idempotency_422(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """X-Idempotency-Key 重复 → 422。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": price_dim})
    review_id = r.json()["review_id"]
    # Redis 为共享中间件（test schema 无法隔离），幂等 key 需每次唯一避免残留冲突
    key = f"itest-idem-{uuid.uuid4().hex}"
    h = {**exp_headers, "X-Idempotency-Key": key}
    # 与 price_formula 一致用 stream 读取 SSE（普通 post 读流式响应在整文件运行时
    # 偶发 'Event loop is closed'）
    async with client.stream("POST", f"/api/v1/reviews/{review_id}/score", headers=h) as r1:
        assert r1.status_code == 200
        async for _ in r1.aiter_lines():
            pass
    async with client.stream("POST", f"/api/v1/reviews/{review_id}/score", headers=h) as r2:
        assert r2.status_code == 422


@pytest.mark.asyncio
async def test_stream_score_circuit_open_503(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """断路器 OPEN → 503（前端切换纯人工评审）。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": price_dim})
    review_id = r.json()["review_id"]
    fake = MagicMock()
    fake.circuit_state = "OPEN"
    with patch("app.api.v1.reviews.get_client", return_value=fake):
        resp = await client.post(f"/api/v1/reviews/{review_id}/score", headers=exp_headers)
    assert resp.status_code == 503
    assert "AI" in resp.json()["detail"]


# ==================== 暂存 / 提交 ====================


@pytest.mark.asyncio
async def test_save_and_submit_review(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """暂存 DRAFT → 提交 CONFIRMED（score 与建议一致）。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": price_dim})
    review_id = r.json()["review_id"]
    r = await client.put(f"/api/v1/reviews/{review_id}/score", headers=exp_headers,
                         json={"score": 20.0, "comment": "报价公式", "ai_suggestion": {"score": 20.0}})
    assert r.status_code == 200
    assert r.json()["status"] == "DRAFT"
    r = await client.post(f"/api/v1/reviews/{review_id}/submit", headers=exp_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "CONFIRMED"


# ==================== SSE 追问对话 ====================


@pytest.mark.asyncio
async def test_stream_chat_with_fake_llm(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """chat SSE：thinking→thought*→done，历史追加。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    tech_dim = await _dim_id(client, pm_headers, lot["lot_id"], "技术")
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": tech_dim})
    review_id = r.json()["review_id"]

    fake = MagicMock()
    fake.circuit_state = "CLOSED"

    async def _stream(prompt, max_tokens=1024):
        yield "第一句"
        yield "第二句"

    fake.chat_stream = _stream
    with patch("app.api.v1.reviews.get_client", return_value=fake):
        async with client.stream("POST", f"/api/v1/reviews/{review_id}/chat",
                                 headers=exp_headers, json={"question": "方案实施周期多久？"}) as sr:
            assert sr.status_code == 200
            lines = [ln async for ln in sr.aiter_lines()]
    events = _parse_sse(lines)
    names = [e["event"] for e in events]
    assert names[0] == "thinking"
    assert names[-1] == "done"
    assert names.count("thought") == 2  # 两个 delta
    # 对话历史已落库
    from sqlalchemy import text

    async with session_factory() as s:
        n = (await s.execute(text(
            "SELECT COUNT(*) FROM conversation_message WHERE review_id=:r"),
            {"r": review_id})).scalar()
    assert n == 2  # user + assistant
