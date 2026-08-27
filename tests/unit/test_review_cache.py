"""ST1 评分结果语义缓存单元测试（review_service 缓存 helper + stream_score 缓存分支）。

覆盖：
- 未命中 → 真实调 LLM，正常走完写缓存（rpush+expire）
- 命中 → 重放帧、不调 LLM（重放 = 首次结果，用户已拍板）
- flush：按 bid 前缀删 / 全量删
- Redis 读挂 → miss 走真实流（fail-open）
- Redis 写挂 → 不抛（fail-open）

关键契约：重放帧带首次真实 usage（评测断言 usage 齐全且为正），事件序零偏差。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.rag.retriever import RetrievalResult
from app.models.bid_document import BidDocument
from app.models.expert_review import ExpertReview
from app.models.project import ScoringDimension
from app.services import review_service as svc
from app.services.review_service import flush_score_cache, stream_score

# 复用 test_review_service.py 的 meta 形状（build_score_prompt 依赖的检索质量元信息）
_META = {"source_count": 1, "max_score": 0.8, "semantic_ok": True, "confidence_band": "high"}


def _result() -> RetrievalResult:
    return RetrievalResult(
        chunk_id="c1", bid_id="B1", lot_id="LOT-1",
        content="标书技术方案：微服务架构，分层清晰", chapter_title="技术方案", page_range=[3, 3],
        score=0.8, source="vector",
    )


def _setup_session() -> AsyncMock:
    """构造 review/dim/bid + rubric 标准 session mock（与 test_review_service.py 一致）。

    session.get 按 model class 映射而非顺序 list side_effect——同一测试内多次 collect
    （首跑 miss + 二跑 hit）都会重复 get，list 会被首跑消费耗尽抛 StopAsyncIteration。
    """
    session = AsyncMock()
    review = MagicMock(review_id="R1", expert_id="E1", dimension_id="D1", bid_id="B1")
    dim = MagicMock(dimension_id="D1", name="技术方案", max_score=30)
    bid = MagicMock(bid_id="B1", lot_id="LOT-1", bid_amount=None, structured_data=None)
    by_cls = {ExpertReview: review, ScoringDimension: dim, BidDocument: bid}
    session.get.side_effect = lambda cls, pk: by_cls[cls]
    criterion = MagicMock(name="架构合理性", max_score=10, scoring_rubric="分层清晰", description="")
    criteria_res = MagicMock()
    criteria_res.all.return_value = [criterion]
    session.scalars.return_value = criteria_res
    return session


def _fake_retriever(monkeypatch):
    async def _fake(query, **kwargs):
        return [_result()], None, _META

    monkeypatch.setattr(svc, "retrieve_with_meta", _fake)


def _fake_llm(monkeypatch, calls: list):
    """mock chat_stream：<thinking>推理段 + <answer>结论段，调用记入 calls。"""
    async def _stream(prompt, max_tokens=2048):
        calls.append(prompt)
        yield "<thinking>依据标书技术方案合理性判断。</thinking>", None
        yield "<answer>方案完整可行。\n分数: 25.0</answer>", None

    client = MagicMock()
    client.chat_stream = _stream
    monkeypatch.setattr(svc, "get_client", lambda: client)
    return client


async def _collect(session):
    return [f async for f in stream_score(session, review_id="R1", expert_id="E1")]


def _parse_frames(frames: list[str]) -> list[dict]:
    out = []
    for fr in frames:
        d = {"id": None, "event": None, "data": None}
        for line in fr.strip().split("\n"):
            if line.startswith("id: "):
                d["id"] = int(line[4:])
            elif line.startswith("event: "):
                d["event"] = line[7:]
            elif line.startswith("data: "):
                d["data"] = json.loads(line[6:])
        out.append(d)
    return out


def _redis_with_pipe(monkeypatch, *, miss=False, write_down=False):
    """评分缓存 redis mock：原子写走 r.pipeline(transaction=True)（delete/rpush/expire
    缓冲，execute 提交），读走 lrange。write_down 模拟写提交抛错（fail-open）。"""
    redis = MagicMock()
    redis.lrange = AsyncMock(return_value=None if miss else [])
    pipe = MagicMock()
    pipe.delete = MagicMock()
    pipe.rpush = MagicMock()
    pipe.expire = MagicMock()
    pipe.execute = AsyncMock(
        side_effect=ConnectionError("redis down") if write_down else None
    )
    redis.pipeline = MagicMock(return_value=pipe)
    monkeypatch.setattr(svc, "get_redis", lambda: redis)
    return redis, pipe


@pytest.mark.asyncio
async def test_score_cache_miss_real_llm_then_save(monkeypatch):
    """未命中：调真实 LLM 一次，正常走完（score/usage/done）后帧写入缓存。"""
    session = _setup_session()
    _fake_retriever(monkeypatch)
    calls: list = []
    _fake_llm(monkeypatch, calls)

    redis, pipe = _redis_with_pipe(monkeypatch, miss=True)

    frames = await _collect(session)
    events = _parse_frames(frames)
    names = [e["event"] for e in events]
    # 事件序契约（§5.1）：meta 首帧 → thinking(RETRIEVING) → source/tool_call → REASONING
    # → reasoning/answer/thought → score → usage → done，seq 严格递增
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert "score" in names and "usage" in names
    seqs = [e["id"] for e in events]
    assert seqs == sorted(seqs) and seqs[0] == 1 and len(set(seqs)) == len(seqs)

    # 真实调 LLM 一次
    assert len(calls) == 1
    # done 结构化分数透出（评测端直接取，不依赖正则）
    done = events[-1]["data"]
    assert done["score"] == 25.0

    # 缓存写入：pipeline 内 rpush(key, *frames) 帧 = 除 meta 首帧外全部（含 done），
    # execute 原子提交 + TTL 24h
    saved_frames = list(pipe.rpush.call_args.args[1:])
    assert saved_frames == frames[1:]
    assert pipe.execute.await_count == 1


@pytest.mark.asyncio
async def test_score_cache_hit_replays_no_llm(monkeypatch):
    """命中：重放首次帧、不调 LLM；重放流与首次流字节一致（事件序/usage 零偏差）。"""
    session = _setup_session()
    _fake_retriever(monkeypatch)
    calls: list = []
    _fake_llm(monkeypatch, calls)

    redis, pipe = _redis_with_pipe(monkeypatch)

    # 首跑：miss → 真实流
    redis.lrange.return_value = None
    first = await _collect(session)
    assert len(calls) == 1

    # 二跑：模拟 Redis 已缓存（取首跑写入的全部帧）
    cached = list(pipe.rpush.call_args.args[1:])
    assert cached
    redis.lrange.return_value = cached
    second = await _collect(session)

    # 重放 = 首次结果：缓存段（meta 之后全部帧）字节级一致——meta 帧每次重生成
    # 带新 ts（ts 是 sse_event 注入的观测字段，不属于缓存内容），只比 second[1:]==first[1:]
    assert second[1:] == first[1:]
    # 事件序与 seq 完整复现（含 usage 透传首次值）
    assert [e["event"] for e in _parse_frames(second)] == [e["event"] for e in _parse_frames(first)]
    assert [e["id"] for e in _parse_frames(second)] == [e["id"] for e in _parse_frames(first)]
    # 未再调 LLM
    assert len(calls) == 1
    # 未重复写缓存（命中即 return，不触发 execute 第二遍）
    assert pipe.execute.await_count == 1


@pytest.mark.asyncio
async def test_score_cache_flush_by_bid_and_full(monkeypatch):
    """flush：指定标书按前缀删；None 全量删。"""
    flushed: list[str] = []

    async def _fake_flush_keys(prefix: str) -> int:
        flushed.append(prefix)
        return 0

    monkeypatch.setattr(svc, "flush_keys", _fake_flush_keys)

    assert await flush_score_cache(bid_id="B1") == 0
    assert await flush_score_cache() == 0
    assert flushed == ["score:scache:B1:", "score:scache:"]


@pytest.mark.asyncio
async def test_score_cache_redis_read_down_failopen(monkeypatch):
    """Redis 读挂（lrange 抛错）→ miss，走真实流，不阻断评分。"""
    session = _setup_session()
    _fake_retriever(monkeypatch)
    calls: list = []
    _fake_llm(monkeypatch, calls)

    redis = MagicMock()
    redis.lrange = AsyncMock(side_effect=ConnectionError("redis down"))
    monkeypatch.setattr(svc, "get_redis", lambda: redis)

    frames = await _collect(session)  # 不抛
    events = _parse_frames(frames)
    assert events[-1]["event"] == "done"
    assert len(calls) == 1  # 读挂仍真实评分


@pytest.mark.asyncio
async def test_score_cache_redis_write_down_failopen(monkeypatch):
    """Redis 写挂（rpush 抛错）→ 不抛，评分流正常走完。"""
    session = _setup_session()
    _fake_retriever(monkeypatch)
    calls: list = []
    _fake_llm(monkeypatch, calls)

    redis, pipe = _redis_with_pipe(monkeypatch, miss=True, write_down=True)

    frames = await _collect(session)  # 不抛
    events = _parse_frames(frames)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["score"] == 25.0
    assert len(calls) == 1
