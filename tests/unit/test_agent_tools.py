"""P7.x agent 工具层单元测试（intent.py + tools.py）。

覆盖：
- _classify_intent 优先级边界（身份闲聊/寒暄整句/问候前缀查询/unknown 回指）
- _is_non_doc_question 豁免判定
- 三个工具执行器：retrieve_knowledge（命中/空/降级三态）、get_dimension_rubric（rubric 组装）、
  get_bid_structured_info（标量 + structured_data + 模糊字段匹配）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.agent.intent import _classify_intent, _is_non_doc_question
from app.ai.agent.tools import (
    ToolContext,
    execute_get_bid_structured_info,
    execute_get_dimension_rubric,
    execute_retrieve_knowledge,
)
from app.ai.rag.retriever import RetrievalResult
from unittest.mock import AsyncMock, MagicMock


# ==================== 意图分类 ====================


def test_intent_identity_smalltalk():
    """身份闲聊最特定：即便含疑问词（你能做什么）也判 smalltalk。"""
    assert _classify_intent("你是谁") == "smalltalk"
    assert _classify_intent("你能做什么") == "smalltalk"
    assert _classify_intent("介绍一下你自己") == "smalltalk"


def test_intent_casual_fullmatch():
    """寒暄整句（含"怎么"但整句匹配）判 smalltalk，优先于 query。"""
    assert _classify_intent("最近怎么样") == "smalltalk"
    assert _classify_intent("今天心情怎么样") == "smalltalk"
    assert _classify_intent("你在干嘛呢") == "smalltalk"
    assert _classify_intent("你呢") == "smalltalk"


def test_intent_query_beats_social():
    """查询优先于一般闲聊：带问候前缀的查询必须判 query（防空 context 编造）。"""
    assert _classify_intent("你好，技术方案怎么样") == "query"
    assert _classify_intent("报价是多少") == "query"
    assert _classify_intent("评分标准是什么") == "query"
    assert _classify_intent("标书的技术方案") == "query"  # 领域词命中


def test_intent_social_smalltalk():
    """纯问候/致谢（无查询意图）判 smalltalk。"""
    assert _classify_intent("你好") == "smalltalk"
    assert _classify_intent("谢谢") == "smalltalk"
    assert _classify_intent("再见") == "smalltalk"


def test_intent_unknown_and_referential():
    """unknown：非闲聊非明确查询；回指词（就这个）回看 history 归队。"""
    assert _classify_intent("嗯嗯") == "unknown"
    assert _classify_intent("就这个", history=["技术方案怎么样"]) == "query"
    assert _classify_intent("再详细点", history=["评分标准是什么"]) == "query"
    # 无 history 时回指词仍判 unknown（无法归队）
    assert _classify_intent("就这个") == "unknown"


def test_non_doc_question():
    """F3 豁免：纯计算/实时信息/通用常识命中；标书相关问题不命中。"""
    assert _is_non_doc_question("17×23等于多少")
    assert _is_non_doc_question("计算 17*23")
    assert _is_non_doc_question("今天是星期几")
    assert not _is_non_doc_question("技术方案的评审标准")
    assert not _is_non_doc_question("")


# ==================== 工具执行器 ====================


def _ctx(bid=None, dim=None, session=None):
    return ToolContext(
        session=session or AsyncMock(),
        review=MagicMock(),
        bid=bid or MagicMock(bid_id="B1", lot_id="LOT-1"),
        dimension=dim,
    )


@pytest.mark.asyncio
async def test_retrieve_knowledge_hit(monkeypatch):
    """命中：context 按 [来源N] 格式化 + 检索质量元信息透出。"""
    from app.ai.agent import tools as tools_mod

    result = RetrievalResult(
        chunk_id="c1", bid_id="B1", lot_id="LOT-1",
        content="标书技术方案：微服务架构", chapter_title="技术方案", page_range=[3, 3],
        score=0.8, source="vector",
    )
    meta = {"max_score": 0.8, "semantic_ok": True, "source_count": 1, "confidence_band": "high"}

    async def _fake(query, **kwargs):
        return [result], None, meta

    monkeypatch.setattr(tools_mod, "retrieve_with_meta", _fake)
    ctx = _ctx()
    out = await execute_retrieve_knowledge(ctx, "技术方案怎么样")
    assert out["source_count"] == 1
    assert "[来源1]" in out["context"]
    assert "微服务架构" in out["context"]
    assert out["confidence_band"] == "high"
    assert "error" not in out


@pytest.mark.asyncio
async def test_retrieve_knowledge_empty(monkeypatch):
    """检索正常但空：source_count=0（走三路兜底"未找到"，非 error 语义）。"""
    from app.ai.agent import tools as tools_mod

    meta = {"max_score": None, "semantic_ok": True, "source_count": 0, "confidence_band": "none"}

    async def _fake(query, **kwargs):
        return [], None, meta

    monkeypatch.setattr(tools_mod, "retrieve_with_meta", _fake)
    out = await execute_retrieve_knowledge(_ctx(), "不存在的内容")
    assert out["source_count"] == 0
    assert "error" not in out
    assert out["context"] == ""


@pytest.mark.asyncio
async def test_retrieve_knowledge_degraded(monkeypatch):
    """检索降级（hint 非空）且无结果：error 字段注入（走 LLM 兜底声明可信度偏低）。"""
    from app.ai.agent import tools as tools_mod

    meta = {"max_score": None, "semantic_ok": False, "source_count": 0, "confidence_band": "none"}

    async def _fake(query, **kwargs):
        return [], "语义检索超时，已降级为关键词", meta

    monkeypatch.setattr(tools_mod, "retrieve_with_meta", _fake)
    out = await execute_retrieve_knowledge(_ctx(), "技术方案")
    assert out["source_count"] == 0
    assert out["error"] == "语义检索超时，已降级为关键词"


@pytest.mark.asyncio
async def test_retrieve_knowledge_bid_none():
    """bid 不存在：直接 error（无法检索）。"""
    out = await execute_retrieve_knowledge(_ctx(bid=None), "技术方案")
    assert out["source_count"] == 0
    assert "error" in out


@pytest.mark.asyncio
async def test_get_dimension_rubric():
    """rubric：维度 + 评分项组装（scoring_rubric 优先，description 兜底）。"""
    # SimpleNamespace：MagicMock(name=...) 的 name 是特殊参数（返回子 mock 而非字符串）
    c1 = SimpleNamespace(name="架构合理性", max_score=10, scoring_rubric="分层清晰", description="")
    c2 = SimpleNamespace(name="性能", max_score=10, scoring_rubric=None, description="响应时间合理")
    criteria_res = MagicMock()
    criteria_res.all.return_value = [c1, c2]
    session = AsyncMock()  # AsyncSession.scalars 为 async 方法
    session.scalars.return_value = criteria_res
    # SimpleNamespace：避免 MagicMock(name=...) 的 name 特殊属性（返回 mock 而非字符串）
    dim = SimpleNamespace(dimension_id="D1", name="技术方案", max_score=30)
    out = await execute_get_dimension_rubric(_ctx(dim=dim, session=session))
    assert out["dimension"] == {"name": "技术方案", "max_score": 30}
    assert out["source_count"] == 2
    assert out["criteria"][0] == {"name": "架构合理性", "max_score": 10, "rubric": "分层清晰"}
    assert out["criteria"][1]["rubric"] == "响应时间合理"


@pytest.mark.asyncio
async def test_get_dimension_rubric_none_dim():
    """无关联维度：error（工具不可用）。"""
    out = await execute_get_dimension_rubric(_ctx(dim=None))
    assert "error" in out


@pytest.mark.asyncio
async def test_get_bid_structured_info():
    """结构化数据：标量字段 + structured_data 合并。"""
    bid = MagicMock(bid_amount=100.0, duration=30, team_size=10, structured_data={"CMMI3": True})
    out = await execute_get_bid_structured_info(_ctx(bid=bid))
    assert out["fields"]["报价金额"] == "100.0"
    assert out["fields"]["工期"] == 30
    assert out["fields"]["CMMI3"] is True
    assert out["source_count"] == 4


@pytest.mark.asyncio
async def test_get_bid_structured_info_field_filter():
    """field 过滤 + 模糊匹配（"资质"→"资质证书"）。"""
    bid = MagicMock(
        bid_amount=None, duration=None, team_size=None,
        structured_data={"资质证书": "CMMI3、ISO9001", "服务网点": 5},
    )
    out = await execute_get_bid_structured_info(_ctx(bid=bid), field="资质")
    assert out["fields"] == {"资质证书": "CMMI3、ISO9001"}
    # 未匹配字段 → error
    out2 = await execute_get_bid_structured_info(_ctx(bid=bid), field="不存在的字段")
    assert "error" in out2
