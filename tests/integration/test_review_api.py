"""P7.3 评审 API 集成测试（task.md #10/#11/#12）。

成功：创建评审工作台（bid FROZEN）；报价维度 SSE 纯公式（thinking→price_calc→done）；
暂存/提交；追问 chat SSE。
错误：bid 未 FROZEN 400；维度不属标段 400；幂等 Key 重复 422；断路器 OPEN → 503。
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

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


async def _assign_lot_expert(lot_id: str, expert_id: str, dim_ids: list[str]) -> None:
    """前置专家-标段分配（评审归属校验必需，P4.2 分配数据直接 ORM 落库）。"""
    from app.core.database import session_factory
    from app.models.lot_expert_assignment import LotExpertAssignment

    async with session_factory() as s:
        s.add(LotExpertAssignment(lot_id=lot_id, expert_id=expert_id, dimension_ids=dim_ids))
        await s.commit()


# ==================== 创建评审工作台 ====================


@pytest.mark.asyncio
async def test_create_review_success(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """bid FROZEN + 维度归属匹配 + 专家已分配 → 201 DRAFT。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [price_dim])
    resp = await client.post("/api/v1/reviews", headers=exp_headers,
                             json={"bid_id": bids[0], "dimension_id": price_dim})
    assert resp.status_code == 201
    assert resp.json()["status"] == "DRAFT"


@pytest.mark.asyncio
async def test_create_review_expert_not_assigned_403(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """专家未被分配标段 → 403（评审水平越权防护）。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    # 不建 assignment，直接越权创建
    resp = await client.post("/api/v1/reviews", headers=exp_headers,
                             json={"bid_id": bids[0], "dimension_id": price_dim})
    assert resp.status_code == 403
    assert "未被分配" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_review_dimension_not_in_assignment_403(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """专家已分配标段但维度不在负责维度 → 403。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", ["ITEST-D-OTHER"])
    resp = await client.post("/api/v1/reviews", headers=exp_headers,
                             json={"bid_id": bids[0], "dimension_id": price_dim})
    assert resp.status_code == 403
    assert "不在专家" in resp.json()["detail"]


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
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [price_dim])
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": price_dim})
    review_id = r.json()["review_id"]
    async with client.stream("POST", f"/api/v1/reviews/{review_id}/score", headers=exp_headers) as sr:
        assert sr.status_code == 200
        lines = [ln async for ln in sr.aiter_lines()]
    events = _parse_sse(lines)
    # 契约改造后 meta 为首帧（§5.1）
    assert [e["event"] for e in events] == ["meta", "thinking", "price_calc", "done"]
    calc = next(e["data"]["result"] for e in events if e["event"] == "price_calc")
    # 基准价 = (100+120+80)/3 = 100，报价 100 → 满分
    assert calc["calculatedScore"] == 20.0
    assert calc["basePrice"] == 100.0
    assert [e["id"] for e in events] == [1, 2, 3, 4]  # seq 递增（断流续推用）
    # done 显式结构化分数（评测端不依赖正则提取）
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["score"] == 20.0


@pytest.mark.asyncio
async def test_stream_score_idempotency_422(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """X-Idempotency-Key 重复 → 422。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [price_dim])
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
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [price_dim])
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
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [price_dim])
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
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [tech_dim])
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": tech_dim})
    review_id = r.json()["review_id"]

    fake = MagicMock()
    fake.circuit_state = "CLOSED"

    async def _stream(prompt, max_tokens=1024):
        # 契约改造后 yield (delta, usage_or_None)
        yield "第一句", None
        yield "第二句", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    fake.chat_stream = _stream
    with patch("app.api.v1.reviews.get_client", return_value=fake):
        async with client.stream("POST", f"/api/v1/reviews/{review_id}/chat",
                                 headers=exp_headers, json={"question": "方案实施周期多久？"}) as sr:
            assert sr.status_code == 200
            lines = [ln async for ln in sr.aiter_lines()]
    events = _parse_sse(lines)
    names = [e["event"] for e in events]
    assert names[0] == "meta"  # 契约 meta 首帧
    assert names[-1] == "done"
    # 契约三发对齐：agent_loop 作答轮聚合增量后产单个 answer 帧 → 切分一次
    # （reasoning==answer==thought 各 1，verify_sp_e2e 的对齐断言在真实流上验）
    assert names.count("thought") == 1
    assert names.count("reasoning") == 1
    assert names.count("answer") == 1
    assert "usage" in names  # usage 事件（聚合 mock 的 usage）
    # 对话历史已落库
    from sqlalchemy import text

    async with session_factory() as s:
        n = (await s.execute(text(
            "SELECT COUNT(*) FROM conversation_message WHERE review_id=:r"),
            {"r": review_id})).scalar()
    assert n == 2  # user + assistant


# ==================== P8 异常兜底：chat 503 / SSE error 帧 ====================


@pytest.mark.asyncio
async def test_chat_returns_503_when_circuit_open(client, exp_headers):
    """断路器 OPEN → chat 503（与评分流对齐，前端仅 503 才降级纯人工评审）。"""
    fake = MagicMock()
    fake.circuit_state = "OPEN"
    with patch("app.api.v1.reviews.get_client", return_value=fake):
        resp = await client.post("/api/v1/reviews/ITEST-R-NOTEXIST/chat",
                                 headers=exp_headers, json={"question": "任何问题"})
    assert resp.status_code == 503
    assert "AI" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_score_stream_error_frame_on_service_failure(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """评分服务中途故障 → gen() 补 error 帧收尾（前端提示重试，不静默断流）。"""
    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    price_dim = await _dim_id(client, pm_headers, lot["lot_id"], "报价")
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [price_dim])
    r = await client.post("/api/v1/reviews", headers=exp_headers,
                          json={"bid_id": bids[0], "dimension_id": price_dim})
    review_id = r.json()["review_id"]

    async def _broken_score(session, *, review_id, expert_id):
        raise RuntimeError("评分服务故障")
        yield  # pragma: no cover  保持 async generator 身份

    with patch("app.api.v1.reviews.svc.stream_score", new=_broken_score):
        async with client.stream("POST", f"/api/v1/reviews/{review_id}/score", headers=exp_headers) as sr:
            assert sr.status_code == 200
            lines = [ln async for ln in sr.aiter_lines()]
    events = _parse_sse(lines)
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["detail"] == "评分流中断，请重试"


# ==================== ST1 评分语义缓存（同 bid×dim 二次调用重放） ====================


class _FakeRedis:
    """内存 Redis 替身（仅服务层评分缓存用，不污染共享 Redis）。

    写路径自原子化改造后走 r.pipeline(transaction=True)（MULTI/EXEC：delete/rpush/expire
    缓冲，execute 统一生效），读路径仍 lrange。"""
    def __init__(self) -> None:
        self._store: dict[str, list[str]] = {}

    async def lrange(self, key, start=0, end=-1):
        vals = self._store.get(key, [])
        return vals if (start, end) == (0, -1) else vals[start:end or None]

    def pipeline(self, transaction=True):
        return _FakePipeline(self._store)


class _FakePipeline:
    """MULTI/EXEC 替身：缓冲 delete/rpush/expire，execute 时按序应用到共享 store。"""

    def __init__(self, store: dict[str, list[str]]) -> None:
        self._store = store
        self._cmds: list[tuple] = []

    def delete(self, key):
        self._cmds.append(("del", key))

    def rpush(self, key, *values):
        self._cmds.append(("rpush", key, values))

    def expire(self, key, ttl):
        self._cmds.append(("expire", key, ttl))

    async def execute(self):
        for cmd in self._cmds:
            if cmd[0] == "del":
                self._store.pop(cmd[1], None)
            elif cmd[0] == "rpush":
                self._store.setdefault(cmd[1], []).extend(cmd[2])
            # expire：TTL 是替身不模拟的观测语义，无存储状态
        return [True] * len(self._cmds)


@pytest.mark.asyncio
async def test_score_cache_second_call_replays_without_llm(
    client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed
):
    """ST1：同 bid×dim 两次 POST /score（两个 review_id）→ 首次调 LLM、第二次重放首次结果。

    验证：事件序一致、done.score 一致、usage 透传首次值、LLM 仅首次被调。
    mock retrieve_with_meta（集成测试无 Milvus 索引，隔离缓存行为本身）。
    """
    from app.ai.rag.retriever import RetrievalResult

    lot, bids = await _frozen_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed,
                                  amounts=[100, 120, 80])
    tech_dim = await _dim_id(client, pm_headers, lot["lot_id"], "技术")
    await _assign_lot_expert(lot["lot_id"], "ITEST-E1", [tech_dim])
    r1 = await client.post("/api/v1/reviews", headers=exp_headers,
                           json={"bid_id": bids[0], "dimension_id": tech_dim})
    r2 = await client.post("/api/v1/reviews", headers=exp_headers,
                           json={"bid_id": bids[0], "dimension_id": tech_dim})
    assert r1.status_code == 201 and r2.status_code == 201
    review1, review2 = r1.json()["review_id"], r2.json()["review_id"]
    assert review1 != review2  # 两个 review 实例，同一 bid×dim

    result = RetrievalResult(
        chunk_id="ITEST-CHUNK-1", bid_id=bids[0], lot_id=lot["lot_id"],
        content="标书技术方案：微服务架构，分层清晰", chapter_title="技术方案",
        page_range=[3, 3], score=0.8, source="vector",
    )
    meta = {"source_count": 1, "max_score": 0.8, "semantic_ok": True, "confidence_band": "high"}

    async def _fake_retrieve(query, **kwargs):
        return [result], None, meta

    calls: list = []
    fake = MagicMock()
    fake.circuit_state = "CLOSED"

    async def _stream(prompt, max_tokens=2048):
        calls.append(prompt)
        yield "<thinking>依据技术方案合理性判断。</thinking>", {
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
        }
        yield "<answer>方案完整可行。\n分数: 25.0</answer>", None

    fake.chat_stream = _stream
    fake_redis = _FakeRedis()
    # get_client：reviews 层 circuit 检查 + service 层 chat_stream 都需替换
    with patch("app.api.v1.reviews.get_client", return_value=fake), \
            patch("app.services.review_service.get_client", return_value=fake), \
            patch("app.services.review_service.get_redis", return_value=fake_redis), \
            patch("app.services.review_service.retrieve_with_meta", new=_fake_retrieve):
        async with client.stream("POST", f"/api/v1/reviews/{review1}/score",
                                 headers=exp_headers) as sr:
            assert sr.status_code == 200
            ev1 = _parse_sse([ln async for ln in sr.aiter_lines()])
        async with client.stream("POST", f"/api/v1/reviews/{review2}/score",
                                 headers=exp_headers) as sr:
            assert sr.status_code == 200
            ev2 = _parse_sse([ln async for ln in sr.aiter_lines()])

    assert len(calls) == 1  # 仅首次真实调 LLM，第二次缓存重放
    names1 = [e["event"] for e in ev1]
    names2 = [e["event"] for e in ev2]
    assert names1 == names2  # 事件序零偏差（契约 §5.1）
    # 命中返回首次结果：done.score / usage 一致（usage 透传首次真实值，不发 0）
    d1 = next(e for e in ev1 if e["event"] == "done")["data"]
    d2 = next(e for e in ev2 if e["event"] == "done")["data"]
    assert d1["score"] == d2["score"] == 25.0
    u1 = next(e for e in ev1 if e["event"] == "usage")["data"]
    u2 = next(e for e in ev2 if e["event"] == "usage")["data"]
    assert u1["total_tokens"] == u2["total_tokens"] == 150
    # 重放段（meta 之后全部帧）事件序一致（meta 每次重生成带新 ts，不比值）
    assert names2[1:] == names1[1:]
