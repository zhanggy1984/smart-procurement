"""围串标检测（P5.1 初筛 close-bidding / P5.2-5.3 深度检测扩展）。

close_bidding()：PM 关闭投标。
- 校验 lot=BIDDING；有效标书（PARSED/PARSING）<3 → ABANDONED
- SELECT FOR UPDATE 锁 lot 行防并发（同一标段重复关闭）
- 初筛三检（不走 AI）：
  - 关系图谱粗检（Neo4j）：投标供应商间 SAME_CONTROLLER(+30)/AFFILIATE_OF(+20)/BID_TOGETHER(+10)
  - 报价异常初检（MySQL）：报价集中度（价差 <1% → +40）
  - 标书语义相似度粗检（Milvus+FAISS）：chunk 级高相似段落对 ≥7 对 → +40
    （P5.1 回归：原"标书级平均向量>0.8"对同主题专业标书区分度失效）
- 综合风险评分：LOW(≤25) 自动通过（标书 FROZEN + lot PRE_SCREEN→UNDER_REVIEW）；
  MEDIUM+ 待 PM 确认（lot→PRE_SCREEN）

验收（task.md P5.1）：3 家正常投标→LOW 自动通过；1 对 SAME_CONTROLLER→MEDIUM 待办。
"""

from __future__ import annotations

import json

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import neo4j
from app.core.database import session_factory
from app.models.bid_document import BidDocument, BidStatus
from app.models.project import Lot
from app.services import config_service

logger = structlog.get_logger(__name__)

# 状态
LOT_PRE_SCREEN = "PRE_SCREEN"
LOT_UNDER_REVIEW = "UNDER_REVIEW"
LOT_ABANDONED = "ABANDONED"
LOT_BIDDING = "BIDDING"

# 关联关系权重（分数）
_REL_SCORES = {"SAME_CONTROLLER": 30, "AFFILIATE_OF": 20, "BID_TOGETHER": 10}


class LotNotFoundError(ValueError):
    """标段不存在 → 404。"""


class LotNotBiddableError(ValueError):
    """标段不在投标期 → 400。"""


class LotNotPrescreenError(ValueError):
    """标段状态非初筛待办（PRE_SCREEN）或不可废标 → 400。"""


class BidNotInLotError(ValueError):
    """标书不属于该标段 → 400。"""


class NoValidBidsError(ValueError):
    """有效标书不足 → 400。"""


async def _graph_check(supplier_ids: list[str]) -> tuple[int, list[dict]]:
    """关系图谱粗检（Neo4j）：投标供应商间关联对。返回 (分数, 证据)。"""
    if len(supplier_ids) < 2:
        return 0, []
    driver = neo4j.get_driver()
    pairs: list[dict] = []
    async with driver.session() as session:
        # 初筛只认实质关联（同一控制人/隶属）；BID_TOGETHER（共同投标）是正常行为，
        # 不计入初筛风险，留给 P5.3 深度检测综合评分。
        result = await session.run(
            "MATCH (a:Supplier)-[r:SAME_CONTROLLER|AFFILIATE_OF]->(b:Supplier) "
            "WHERE a.supplierId IN $sids AND b.supplierId IN $sids AND a.supplierId <> b.supplierId "
            "RETURN a.supplierId AS a, b.supplierId AS b, type(r) AS rel",
            sids=supplier_ids,
        )
        async for rec in result:
            pairs.append({"a": rec["a"], "b": rec["b"], "rel": rec["rel"]})
    score = sum(_REL_SCORES.get(p["rel"], 10) for p in pairs)
    logger.info("fraud.graph_check", pairs=pairs, score=score)
    return min(score, 100), pairs


def _price_check(amounts: list[float]) -> tuple[int, list[dict]]:
    """报价异常初检（MySQL）：报价集中度（价差 <1% 视为异常集中）。"""
    if len(amounts) < 2:
        return 0, []
    avg = sum(amounts) / len(amounts)
    if avg == 0:
        return 0, []
    spread = (max(amounts) - min(amounts)) / avg
    if spread < 0.01:
        return 40, [{"type": "PRICE_CLUSTER", "detail": f"报价集中度 {spread:.1%} <1%，异常集中"}]
    return 0, []


async def _vector_check(bid_ids: list[str]) -> tuple[int, list[dict]]:
    """标书语义相似度粗检（chunk 级高相似段落对判定）。返回 (分数, 证据)。

    P5.1 回归：原"标书级平均向量 cosine>0.8"在合成数据同主题句子池下区分度
    失效（正常标书 0.98 vs 围串标 0.99，margin 仅 0.01，任何阈值都不可分——
    平均向量抹掉段落细节后语义中心天然同向）。改用 P5.2 的 chunk 级命中对数
    判定（TEXT_SIMILAR_PAIR_THRESHOLD）：正常标书 0 对、围串标 ≥7 对，
    区分度稳固，且与深度检测口径一致（solution.md 7.1：chunk 级交叉检索才是
    语义相似度的强检测，平均向量仅作弱粗检）。
    """
    try:
        result = await deep_text_similarity("", bid_ids)
    except Exception:  # noqa: BLE001  Milvus 不可用，向量检跳过（0 分）
        return 0, []
    pair_thr = int(config_service.get_sync("fraud.similar_pair_threshold"))
    if result["bid_similar_pairs"]:
        return 40, [{
            "type": "VECTOR_SIMILAR",
            "detail": f"高相似段落对组合 {result['bid_similar_pairs']}（≥{pair_thr} 对）",
        }]
    return 0, []


async def close_bidding(session: AsyncSession, *, lot_id: str, operator_id: str) -> dict:
    """关闭投标：校验 → 三检 → 风险评分 → 状态流转。返回风险与流转结果。"""
    # SELECT FOR UPDATE 锁 lot 行防并发
    lot = (
        await session.execute(select(Lot).where(Lot.lot_id == lot_id).with_for_update())
    ).scalar_one_or_none()
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    if lot.status != LOT_BIDDING:
        raise LotNotBiddableError(f"标段状态 {lot.status} 非投标期（BIDDING）")

    bids = (
        await session.scalars(
            select(BidDocument).where(BidDocument.lot_id == lot_id)
        )
    ).all()
    valid = [b for b in bids if b.status in (BidStatus.PARSED, BidStatus.PARSING)]
    if len(valid) < 3:
        lot.status = LOT_ABANDONED
        await session.commit()
        logger.info("fraud.abandoned", lot_id=lot_id, valid=len(valid))
        raise NoValidBidsError(f"有效标书仅 {len(valid)} 家（需 ≥3），标段已 ABANDONED")

    supplier_ids = list({b.supplier_id for b in valid})
    amounts = [float(b.bid_amount) for b in valid if b.bid_amount]

    # 三检
    graph_score, graph_ev = await _graph_check(supplier_ids)
    price_score, price_ev = _price_check(amounts)
    vector_score, vector_ev = await _vector_check([b.bid_id for b in valid])
    total = graph_score + price_score + vector_score
    risk = "LOW" if total <= float(config_service.get_sync("fraud.auto_pass_threshold")) else "MEDIUM"

    # 状态流转
    if risk == "LOW":
        for b in valid:
            b.status = BidStatus.FROZEN
        lot.status = LOT_UNDER_REVIEW
        await session.commit()
        logger.info("fraud.auto_pass", lot_id=lot_id, score=total)
    else:
        lot.status = LOT_PRE_SCREEN  # PM 待办确认
        await session.commit()
        logger.info("fraud.pending_pm", lot_id=lot_id, score=total)

    return {
        "lot_id": lot_id,
        "risk": risk,
        "total_score": total,
        "scores": {"graph": graph_score, "price": price_score, "vector": vector_score},
        "evidence": {"graph": graph_ev, "price": price_ev, "vector": vector_ev},
        "next_status": lot.status,
        "bid_count": len(valid),
    }


# ==================== P5.2 深度检测 — 标书语义相似度（FAISS） ====================

# 高相似段落阈值（task.md P5.2：IP>0.85）
TEXT_SIMILARITY_THRESHOLD = 0.85

# 标书组合文本相似判定：命中高相似段落对数 ≥ 阈值才认定（围串标=整本大部分段落一致）。
# 阈值默认 7（P6.2 配置化，运行时读 fraud.similar_pair_threshold）。


def _faiss_similar_pairs(
    chunks: list[dict], threshold: float = TEXT_SIMILARITY_THRESHOLD
) -> list[dict]:
    """FAISS 批量 IP（cosine）两两相似度。返回高相似段落对（含得分）。

    用 IndexFlatIP 一次性算全部向量相似（避免逐对 Milvus 网络往返，
    task.md：FAISS 批量替代 N² 次调用）。
    """
    import faiss
    import numpy as np

    if len(chunks) < 2:
        return []
    vecs = np.array([c["embedding"] for c in chunks], dtype="float32")
    if vecs.ndim != 2:
        return []
    index = faiss.IndexFlatIP(vecs.shape[1])
    index.add(vecs)
    sims, idx = index.search(vecs, k=2)  # 每向量 top-2（含自身）
    pairs: list[dict] = []
    for i in range(len(chunks)):
        for j, s in zip(idx[i], sims[i]):
            if j != i and s > threshold and i < j:
                pairs.append(
                    {
                        "chunk_a": chunks[i]["chunk_id"],
                        "chunk_b": chunks[j]["chunk_id"],
                        "score": round(float(s), 4),
                        "bid_a": chunks[i].get("bid_id"),
                        "bid_b": chunks[j].get("bid_id"),
                    }
                )
    return pairs


async def deep_text_similarity(lot_id: str, bid_ids: list[str]) -> dict:
    """深度语义检测：全部标书 chunks → FAISS → 高相似段落对 + 标书级命中统计。

    验收（task.md P5.2）：围串标组 100% 命中高相似对，正常组 0 误报。
    """
    from app.core.milvus import get_collection

    chunks: list[dict] = []
    for bid_id in bid_ids:
        try:
            rows = get_collection().query(
                expr=f'bid_id == "{bid_id}"',
                output_fields=["chunk_id", "bid_id", "embedding"],
                limit=500,
            )
            chunks.extend(rows)
        except Exception:  # noqa: BLE001  Milvus 不可用/无数据
            continue
    pair_thr = int(config_service.get_sync("fraud.similar_pair_threshold"))
    pairs = _faiss_similar_pairs(
        chunks,
        threshold=float(config_service.get_sync("fraud.text_similarity_threshold")),
    )

    # 标书级命中：按组合统计高相似段落对数，达阈值才判定文本相似
    pair_counts: dict[tuple, int] = {}
    for p in pairs:
        if p["bid_a"] != p["bid_b"]:
            key = tuple(sorted((p["bid_a"], p["bid_b"])))
            pair_counts[key] = pair_counts.get(key, 0) + 1
    bid_hits = {k for k, v in pair_counts.items() if v >= pair_thr}
    # 只统计被判定组合的命中对数，避免正常标书偶然命中也把 text_score 拉满
    high_similar_pairs = sum(v for k, v in pair_counts.items() if k in bid_hits)
    logger.info("fraud.deep_text", lot_id=lot_id, chunks=len(chunks), pairs=len(pairs),
                bid_pairs=len(bid_hits), threshold=pair_thr)
    return {
        "lot_id": lot_id,
        "chunk_count": len(chunks),
        "high_similar_pairs": high_similar_pairs,
        "pairs": [p for p in pairs
                  if tuple(sorted((p["bid_a"], p["bid_b"]))) in bid_hits][:50],
        "bid_similar_pairs": [list(x) for x in bid_hits],
    }


# ==================== P5.3 综合深度检测 — 图 + 报价 + 文本 ====================

# 综合权重（task.md P5.3：text×0.40 + graph×0.35 + price×0.25）已配置化（P6.2），
# 运行时从系统配置读取（fraud.weight_text/graph/price），此处不再硬编码。

# 深度图检关系权重（含 BID_TOGETHER，深度检测才纳入）
_DEEP_REL_SCORES = {"SAME_CONTROLLER": 40, "AFFILIATE_OF": 30, "BID_TOGETHER": 15}


def risk_level(
    score: float,
    *,
    low_threshold: float = 25,
    critical_threshold: float = 75,
) -> str:
    """四级风险分类：LOW(≤low)/MEDIUM(≤mid)/HIGH(≤critical)/CRITICAL(>critical)。

    mid=(low+critical)/2，默认 25/75 下边界为 25/50/75，与历史行为逐点一致。
    low/critical 由 async 调用方读取系统配置（P6.2）后传入，保持本函数纯函数可单测。
    """
    mid = (low_threshold + critical_threshold) / 2
    if score <= low_threshold:
        return "LOW"
    if score <= mid:
        return "MEDIUM"
    if score <= critical_threshold:
        return "HIGH"
    return "CRITICAL"


async def _deep_graph_check(supplier_ids: list[str]) -> tuple[int, list[dict]]:
    """供应商关联（含 BID_TOGETHER）。`LIMIT 10` 上限保护（min() 语义防大结果集）。"""
    if len(supplier_ids) < 2:
        return 0, []
    driver = neo4j.get_driver()
    pairs: list[dict] = []
    async with driver.session() as session:
        result = await session.run(
            "MATCH (a:Supplier)-[r:SAME_CONTROLLER|AFFILIATE_OF|BID_TOGETHER]->(b:Supplier) "
            "WHERE a.supplierId IN $sids AND b.supplierId IN $sids AND a.supplierId <> b.supplierId "
            "WITH a, b, r LIMIT 10 "
            "RETURN a.supplierId AS a, b.supplierId AS b, type(r) AS rel",
            sids=supplier_ids,
        )
        async for rec in result:
            pairs.append({"a": rec["a"], "b": rec["b"], "rel": rec["rel"]})
    score = min(100, sum(_DEEP_REL_SCORES.get(p["rel"], 10) for p in pairs))
    return score, pairs


def _deep_price_check(amounts: list[float]) -> tuple[int, list[dict]]:
    """报价模式：集中度（价差<1%）+ 陪标（最低价异常低：最低/次低<0.85）。"""
    ev: list[dict] = []
    if len(amounts) < 2:
        return 0, ev
    avg = sum(amounts) / len(amounts)
    if avg == 0:
        return 0, ev
    spread = (max(amounts) - min(amounts)) / avg
    if spread < 0.01:
        ev.append({"type": "PRICE_CLUSTER", "detail": f"报价集中度 {spread:.1%} <1%"})
    sorted_a = sorted(amounts)
    if len(sorted_a) >= 2 and sorted_a[1] > 0 and sorted_a[0] / sorted_a[1] < 0.85:
        ev.append({"type": "BIDDING_RING", "detail": f"最低价 {sorted_a[0]} 显著低于次低 {sorted_a[1]}"})
    return min(100, len(ev) * 40), ev


async def deep_detection(lot_id: str, bid_ids: list[str]) -> dict:
    """综合深度检测：text×0.40 + graph×0.35 + price×0.25 → 四级风险。

    验收（task.md P5.3）：同一控制人 + 标书高相似 + 价格集中 → HIGH/CRITICAL 触发；
    正常组 → LOW。
    """
    from app.models.bid_document import BidDocument

    async with session_factory() as session:
        bids = (
            await session.execute(
                select(BidDocument.bid_id, BidDocument.supplier_id, BidDocument.bid_amount).where(
                    BidDocument.bid_id.in_(bid_ids)
                )
            )
        ).all()
    supplier_ids = list({b.supplier_id for b in bids})
    amounts = [float(b.bid_amount) for b in bids if b.bid_amount]

    text_result = await deep_text_similarity(lot_id, bid_ids)
    text_score = min(100, text_result["high_similar_pairs"] * 25)
    graph_score, graph_ev = await _deep_graph_check(supplier_ids)
    price_score, price_ev = _deep_price_check(amounts)

    w_text = float(config_service.get_sync("fraud.weight_text"))
    w_graph = float(config_service.get_sync("fraud.weight_graph"))
    w_price = float(config_service.get_sync("fraud.weight_price"))
    total = round(text_score * w_text + graph_score * w_graph + price_score * w_price, 1)
    level = risk_level(
        total,
        low_threshold=float(config_service.get_sync("fraud.auto_pass_threshold")),
        critical_threshold=float(config_service.get_sync("fraud.critical_threshold")),
    )
    # 一票否决（task.md P5.3 验收）：同一实控人是围串标最硬红线，加权求和会稀释强证据
    # （SAME_CONTROLLER=40 × 0.35 = 14 → 误判 LOW 放行）。仅此信号存在时也至少 HIGH。
    # 仅提 SAME_CONTROLLER；AFFILIATE_OF/BID_TOGETHER 相关但非同一主体，仍走加权。
    if any(ev.get("rel") == "SAME_CONTROLLER" for ev in graph_ev) and level != "CRITICAL":
        level = "HIGH"
    logger.info("fraud.deep_detection", lot_id=lot_id, text=text_score, graph=graph_score,
                price=price_score, total=total, level=level)
    return {
        "lot_id": lot_id,
        "risk": level,
        "total_score": total,
        "scores": {"text": text_score, "graph": graph_score, "price": price_score},
        "evidence": {"text": text_result["bid_similar_pairs"], "graph": graph_ev, "price": price_ev},
    }


async def confirm_prescreen(session: AsyncSession, *, lot_id: str, operator_id: str) -> dict:
    """PM 确认放行初筛待办（task.md P5.3 闭环）。

    PRE_SCREEN → 深度检测（text×0.4 + graph×0.35 + price×0.25）：
    - LOW/MEDIUM → 放行：有效标书 FROZEN + lot UNDER_REVIEW
    - HIGH/CRITICAL → 不放行（released=False），前端提示废标建议（防围串标自动流入评审）
    """
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    if lot.status != LOT_PRE_SCREEN:
        raise LotNotPrescreenError(f"标段状态 {lot.status} 非初筛待办（PRE_SCREEN）")

    bids = (await session.scalars(select(BidDocument).where(BidDocument.lot_id == lot_id))).all()
    valid = [b for b in bids if b.status in (BidStatus.PARSED, BidStatus.PARSING)]
    bid_ids = [b.bid_id for b in valid]
    deep = await deep_detection(lot_id, bid_ids)

    if deep["risk"] in ("HIGH", "CRITICAL"):
        logger.info("fraud.prescreen_hold", lot_id=lot_id, risk=deep["risk"], operator=operator_id)
        return {**deep, "released": False, "message": "深度检测高风险，建议废标处理"}

    for b in valid:
        b.status = BidStatus.FROZEN
    lot.status = LOT_UNDER_REVIEW
    await session.commit()
    logger.info("fraud.prescreen_release", lot_id=lot_id, risk=deep["risk"], operator=operator_id)
    return {**deep, "released": True, "message": "已放行，标段进入评审"}


async def disqualify_bid(session: AsyncSession, *, lot_id: str, bid_id: str, operator_id: str) -> dict:
    """PM 废标（task.md E2E-3）：初筛待办/评审中标记某标书 DISQUALIFIED。"""
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")
    if lot.status not in (LOT_PRE_SCREEN, LOT_UNDER_REVIEW):
        raise LotNotPrescreenError(f"标段状态 {lot.status} 不可废标（需 PRE_SCREEN/UNDER_REVIEW）")
    bid = await session.get(BidDocument, bid_id)
    if bid is None or bid.lot_id != lot_id:
        raise BidNotInLotError(f"标书 {bid_id} 不属于标段 {lot_id}")
    bid.status = BidStatus.DISQUALIFIED
    await session.commit()
    logger.info("fraud.disqualify", lot_id=lot_id, bid_id=bid_id, operator=operator_id)
    return {"lot_id": lot_id, "bid_id": bid_id, "status": bid.status}


# ==================== P5.4 围串标报告 ====================

# LLM 报告触发等级（HIGH/CRITICAL 走 LLM，LOW/MEDIUM 模板自动）
_LLM_REPORT_LEVELS = ("HIGH", "CRITICAL")


def _template_report(result: dict) -> str:
    """模板报告：风险评分 + 关键证据 + 建议措施（LOW/MEDIUM 自动生成）。"""
    return (
        f"围串标检测报告（模板）\n"
        f"风险等级：{result['risk']}（综合评分 {result['total_score']}）\n"
        f"分项：图检 {result['scores']['graph']} | 报价检 {result['scores']['price']} | "
        f"文本检 {result['scores']['text']}\n"
        f"关键证据：\n{json.dumps(result['evidence'], ensure_ascii=False)}\n"
        "建议措施：核对供应商关联关系与报价合理性，必要时人工复核标书相似段落。"
    )


async def _llm_report(result: dict) -> str:
    """LLM 报告：DeepSeek 基于检测数据生成结论+证据分析+建议（HIGH/CRITICAL）。"""
    from app.ai.llm.deepseek_client import get_client

    prompt = (
        f"你是国家级围串标分析专家。基于以下检测数据生成评审报告。\n"
        f"风险等级：{result['risk']}，综合评分：{result['total_score']}\n"
        f"检测证据：{json.dumps(result['evidence'], ensure_ascii=False)}\n"
        "请输出：风险结论（50 字内）+ 关键证据分析 + 建议措施（共 250 字内）。"
    )
    try:
        text = await get_client().chat(
            [
                {"role": "system", "content": "你是围串标检测报告撰写专家，输出严谨专业。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=600,
        )
        return text or "（LLM 报告生成失败，请查看检测数据）"
    except Exception as e:  # noqa: BLE001
        logger.warning("fraud.llm_report_failed", error=str(e))
        return "（LLM 不可用，已降级为模板报告）\n" + _template_report(result)


async def generate_report(lot_id: str, bid_ids: list[str]) -> dict:
    """生成围串标检测报告。LOW/MEDIUM 模板自动；HIGH/CRITICAL LLM（PM 触发）。"""
    result = await deep_detection(lot_id, bid_ids)
    if result["risk"] in _LLM_REPORT_LEVELS:
        text = await _llm_report(result)
        mode = "LLM"
    else:
        text = _template_report(result)
        mode = "TEMPLATE"
    logger.info("fraud.report", lot_id=lot_id, risk=result["risk"], mode=mode)
    return {
        "lot_id": lot_id,
        "risk": result["risk"],
        "total_score": result["total_score"],
        "mode": mode,
        "report": text,
        "scores": result["scores"],
        "evidence": result["evidence"],
    }
