"""P7.2 围串标服务流程单元测试（补深：close_bidding / 深度检测 / 报告）。

覆盖（演示链路场景3 关键）：
- close_bidding：LOW 自动通过（FROZEN + UNDER_REVIEW）；MEDIUM 待 PM（PRE_SCREEN）；
  有效标书 <3 → ABANDONED + NoValidBidsError；非 BIDDING / lot 不存在 → 400/404
- _vector_check：Milvus 不可用 → 降级 0 分（不阻断主流程）
- deep_detection：text×0.40 + graph×0.35 + price×0.25 综合评分 → 四级风险
- generate_report：LOW/MEDIUM 模板自动；HIGH/CRITICAL LLM；LLM 不可用降级模板
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.bid_document import BidStatus
from app.services.fraud_detection_service import (
    LotNotBiddableError,
    LotNotFoundError,
    NoValidBidsError,
    _template_report,
    _vector_check,
    close_bidding,
    deep_detection,
    deep_text_similarity,
    generate_report,
    _llm_report,
)


def _mk_bid(bid_id: str, supplier_id: str, amount, status=BidStatus.PARSED):
    b = MagicMock()
    b.bid_id = bid_id
    b.supplier_id = supplier_id
    b.bid_amount = amount
    b.status = status
    return b


@pytest.mark.asyncio
async def test_close_bidding_low_auto_pass():
    """三检全 0 → LOW 自动通过：标书 FROZEN + lot=UNDER_REVIEW。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.lot_id = "LOT-1"
    lot.status = "BIDDING"
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = lot
    session.scalar.return_value = lot
    session.execute.return_value = exec_result

    bids = [_mk_bid("BID-1", "S1", 100), _mk_bid("BID-2", "S2", 110), _mk_bid("BID-3", "S3", 120)]
    bid_result = MagicMock()
    bid_result.all.return_value = bids
    session.scalars.return_value = bid_result

    with patch("app.services.fraud_detection_service._graph_check", new=AsyncMock(return_value=(0, []))), \
         patch("app.services.fraud_detection_service._vector_check", new=AsyncMock(return_value=(0, []))):
        res = await close_bidding(session, lot_id="LOT-1", operator_id="U-1")
    assert res["risk"] == "LOW"
    assert res["total_score"] == 0
    assert res["scores"] == {"graph": 0, "price": 0, "vector": 0}
    assert lot.status == "UNDER_REVIEW"
    assert all(b.status == BidStatus.FROZEN for b in bids)


@pytest.mark.asyncio
async def test_close_bidding_medium_pending_pm():
    """图检命中 SAME_CONTROLLER(+30) → MEDIUM → lot=PRE_SCREEN 待 PM 确认，标书不 FROZEN。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.status = "BIDDING"
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = lot
    session.scalar.return_value = lot
    session.execute.return_value = exec_result

    bids = [_mk_bid("BID-1", "S1", 100), _mk_bid("BID-2", "S2", 110), _mk_bid("BID-3", "S3", 120)]
    bid_result = MagicMock()
    bid_result.all.return_value = bids
    session.scalars.return_value = bid_result

    graph_ev = [{"a": "S1", "b": "S2", "rel": "SAME_CONTROLLER"}]
    with patch("app.services.fraud_detection_service._graph_check", new=AsyncMock(return_value=(30, graph_ev))), \
         patch("app.services.fraud_detection_service._vector_check", new=AsyncMock(return_value=(0, []))):
        res = await close_bidding(session, lot_id="LOT-1", operator_id="U-1")
    assert res["risk"] == "MEDIUM"
    assert res["total_score"] == 30
    assert res["next_status"] == "PRE_SCREEN"
    assert lot.status == "PRE_SCREEN"
    assert bids[0].status == BidStatus.PARSED  # 未封存


@pytest.mark.asyncio
async def test_close_bidding_insufficient_abandons():
    """有效标书 <3 → 标段 ABANDONED + NoValidBidsError。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.status = "BIDDING"
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = lot
    session.scalar.return_value = lot
    session.execute.return_value = exec_result

    bids = [_mk_bid("BID-1", "S1", 100), _mk_bid("BID-2", "S2", 110)]  # 仅 2 家
    bid_result = MagicMock()
    bid_result.all.return_value = bids
    session.scalars.return_value = bid_result

    with pytest.raises(NoValidBidsError):
        await close_bidding(session, lot_id="LOT-1", operator_id="U-1")
    assert lot.status == "ABANDONED"


@pytest.mark.asyncio
async def test_close_bidding_not_biddable():
    """lot 非 BIDDING → 400。"""
    session = AsyncMock()
    lot = MagicMock()
    lot.status = "UNDER_REVIEW"
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = lot
    session.scalar.return_value = lot
    session.execute.return_value = exec_result
    with pytest.raises(LotNotBiddableError):
        await close_bidding(session, lot_id="LOT-1", operator_id="U-1")


@pytest.mark.asyncio
async def test_close_bidding_lot_not_found():
    """lot 不存在 → 404。"""
    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.scalar.return_value = None
    session.execute.return_value = exec_result
    with pytest.raises(LotNotFoundError):
        await close_bidding(session, lot_id="LOT-X", operator_id="U-1")


@pytest.mark.asyncio
async def test_vector_check_milvus_down_degraded():
    """Milvus 不可用 → 向量检跳过（0 分），不阻断 close_bidding。"""
    with patch("app.services.fraud_detection_service.deep_text_similarity",
               new=AsyncMock(side_effect=RuntimeError("milvus down"))):
        assert await _vector_check(["BID-1", "BID-2"]) == (0, [])


@pytest.mark.asyncio
async def test_deep_detection_high():
    """综合评分：text(100)×0.40 + graph(40)×0.35 + price(0)×0.25 = 54 → HIGH。"""
    sf = MagicMock()  # session_factory() 是同步调用，工厂不能用 AsyncMock（AsyncMock() 返回 coroutine）
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    sf.return_value = ctx

    rows = [SimpleNamespace(bid_id="BID-1", supplier_id="S1", bid_amount=100),
            SimpleNamespace(bid_id="BID-2", supplier_id="S2", bid_amount=110),
            SimpleNamespace(bid_id="BID-3", supplier_id="S3", bid_amount=120)]
    row_result = MagicMock()
    row_result.all.return_value = rows
    session.execute.return_value = row_result

    text_ev = [["BID-1", "BID-2"]]
    with patch("app.services.fraud_detection_service.session_factory", sf), \
         patch("app.services.fraud_detection_service.deep_text_similarity",
               new=AsyncMock(return_value={"high_similar_pairs": 4, "bid_similar_pairs": text_ev})), \
         patch("app.services.fraud_detection_service._deep_graph_check",
               new=AsyncMock(return_value=(40, [{"a": "S1", "b": "S2", "rel": "SAME_CONTROLLER"}]))):
        res = await deep_detection("LOT-1", ["BID-1", "BID-2", "BID-3"])
    assert res["risk"] == "HIGH"
    assert res["total_score"] == 54.0
    assert res["scores"] == {"text": 100, "graph": 40, "price": 0}
    assert res["evidence"]["text"] == text_ev


@pytest.mark.asyncio
async def test_deep_detection_low():
    """无实质关联 → LOW（text/price 均 0）。"""
    sf = MagicMock()  # session_factory() 是同步调用，工厂不能用 AsyncMock（AsyncMock() 返回 coroutine）
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    sf.return_value = ctx

    rows = [SimpleNamespace(bid_id="BID-1", supplier_id="S1", bid_amount=100),
            SimpleNamespace(bid_id="BID-2", supplier_id="S2", bid_amount=120),
            SimpleNamespace(bid_id="BID-3", supplier_id="S3", bid_amount=150)]
    row_result = MagicMock()
    row_result.all.return_value = rows
    session.execute.return_value = row_result

    with patch("app.services.fraud_detection_service.session_factory", sf), \
         patch("app.services.fraud_detection_service.deep_text_similarity",
               new=AsyncMock(return_value={"high_similar_pairs": 0, "bid_similar_pairs": []})), \
         patch("app.services.fraud_detection_service._deep_graph_check",
               new=AsyncMock(return_value=(0, []))):
        res = await deep_detection("LOT-1", ["BID-1", "BID-2", "BID-3"])
    assert res["risk"] == "LOW"


def test_template_report_format():
    """模板报告含风险等级、分项、关键证据、建议措施。"""
    result = {
        "risk": "MEDIUM", "total_score": 30,
        "scores": {"graph": 30, "price": 0, "text": 0},
        "evidence": {"graph": [{"a": "S1", "b": "S2", "rel": "SAME_CONTROLLER"}]},
    }
    text = _template_report(result)
    assert "MEDIUM" in text
    assert "图检 30" in text
    assert "SAME_CONTROLLER" in text
    assert "建议措施" in text


@pytest.mark.asyncio
async def test_generate_report_template_for_low():
    """LOW/MEDIUM → 模板报告自动生成。"""
    base = {"risk": "LOW", "total_score": 10,
            "scores": {"graph": 0, "price": 0, "text": 10}, "evidence": {}}
    with patch("app.services.fraud_detection_service.deep_detection", new=AsyncMock(return_value=base)):
        res = await generate_report("LOT-1", ["BID-1"])
    assert res["mode"] == "TEMPLATE"
    assert "模板" in res["report"]


@pytest.mark.asyncio
async def test_generate_report_llm_for_high():
    """HIGH/CRITICAL → LLM 报告。"""
    base = {"risk": "HIGH", "total_score": 60,
            "scores": {"graph": 40, "price": 40, "text": 60}, "evidence": {}}
    with patch("app.services.fraud_detection_service.deep_detection", new=AsyncMock(return_value=base)), \
         patch("app.services.fraud_detection_service._llm_report",
               new=AsyncMock(return_value="LLM 深度分析报告")):
        res = await generate_report("LOT-1", ["BID-1"])
    assert res["mode"] == "LLM"
    assert res["report"] == "LLM 深度分析报告"


@pytest.mark.asyncio
async def test_llm_report_fallback_template():
    """LLM 不可用 → 降级为模板报告（不抛错，不伪造 LLM 输出）。"""
    client = MagicMock()
    client.chat = AsyncMock(side_effect=RuntimeError("llm down"))
    with patch("app.ai.llm.deepseek_client.get_client", return_value=client):
        text = await _llm_report({
            "risk": "HIGH", "total_score": 60,
            "scores": {"graph": 40, "price": 40, "text": 60},  # 降级后 _template_report 需要
            "evidence": {},
        })
    assert "模板报告" in text


@pytest.mark.asyncio
async def test_close_bidding_status_changed_before_lock():
    """三检期间并发状态变更 → 锁行重读拒绝（LotNotBiddableError），防重复覆盖流转。"""
    session = AsyncMock()
    lot_bidding = MagicMock()
    lot_bidding.status = "BIDDING"
    lot_changed = MagicMock()
    lot_changed.status = "UNDER_REVIEW"  # 锁行重读读到最新状态（populate_existing 刷新）
    session.scalar.return_value = lot_bidding  # 步骤1 无锁读
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = lot_changed  # 步骤3 锁行重读
    session.execute.return_value = exec_result

    bids = [_mk_bid("BID-1", "S1", 100), _mk_bid("BID-2", "S2", 110), _mk_bid("BID-3", "S3", 120)]
    bid_result = MagicMock()
    bid_result.all.return_value = bids
    session.scalars.return_value = bid_result

    with patch("app.services.fraud_detection_service._graph_check", new=AsyncMock(return_value=(0, []))), \
         patch("app.services.fraud_detection_service._vector_check", new=AsyncMock(return_value=(0, []))):
        with pytest.raises(LotNotBiddableError):
            await close_bidding(session, lot_id="LOT-1", operator_id="U-1")


@pytest.mark.asyncio
async def test_deep_text_similarity_unloads_via_thread():
    """Milvus query + FAISS 经 asyncio.to_thread 卸载（自查 #1，不阻塞事件循环）。"""
    def _no_pairs(chunks, threshold=0.85):
        return []

    mock_collection = MagicMock()
    mock_collection.query.return_value = [
        {"chunk_id": "C1", "bid_id": "BID-1", "embedding": [0.1] * 8},
    ]
    with patch("app.core.milvus.get_collection", return_value=mock_collection), \
         patch("app.services.fraud_detection_service.asyncio.to_thread") as mock_thread, \
         patch("app.services.fraud_detection_service._faiss_similar_pairs", new=_no_pairs):
        # 让 to_thread 真实执行目标函数（同时记录调用）
        mock_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        result = await deep_text_similarity("LOT-1", ["BID-1"])
    # 两个同步块（Milvus query / FAISS）均经 to_thread 卸载，不阻塞事件循环
    assert mock_thread.call_count == 2
    assert all(callable(c.args[0]) for c in mock_thread.call_args_list)
    assert result["chunk_count"] == 1
