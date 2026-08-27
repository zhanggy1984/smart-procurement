"""多路召回检索（P2.2）。

三路召回 + RRF 融合排序（task.md P2.2）：
- 路1 向量：Milvus 语义检索（IP 度量，filter lot_id[+bid_id]，top_k=20）
- 路2 关键词：评分维度标准（dimension.name + criterion 文本）关键术语，
  对全量 chunk 精确匹配计数
- 路3 结构化：bid_document.structured_data 精确查（如 CMMI3/ISO9001），增强

RRF 融合：score = Σ 1/(k + rank)，k=60，融合向量+关键词两路 → Top-K。
query 向量用 BGE-M3（embedder 复用），Milvus/embedding 均为阻塞 IO → asyncio.to_thread。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select

from app.ai.rag.degradation import (
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_LOW_THRESHOLD,
    SEMANTIC_TIMEOUT_SECONDS,
    classify_retrieval,
)
from app.ai.rag.embedder import get_embedder
from app.core.database import session_factory
from app.models.bid_document import BidDocument
from app.models.project import ScoringCriterion, ScoringDimension

logger = structlog.get_logger(__name__)

# 检索返回结构标准化（P7.x，参考 good-question RETRIEVE_TOOL_SCHEMA 契约风格）。
# 程序内单一事实源：评分模式 SSE tool_call 事件按此结构透出检索质量元信息，
# 评测端据此观测检索动作（命中条数/相似度/置信档位/降级状态）。
RETRIEVE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "array", "description": "命中证据（chunk_id/chapter_title/score）"},
        "source_count": {"type": "integer", "description": "命中条数（0 表示未命中）"},
        "max_score": {"type": ["number", "null"], "description": "路1 向量最高 IP（语义相似度）"},
        "confidence_band": {"type": "string", "enum": ["none", "low", "high"]},
        "semantic_ok": {"type": "boolean", "description": "路1 语义检索是否可用（超时降级为 false）"},
        "hint": {"type": ["string", "null"], "description": "降级/拒答提示文案"},
    },
}

# 关键词路默认拉取上限（单标书 chunk 数量级，全量打分保证召回）
_KEYWORD_SCAN_LIMIT = 500
# 中文停用词（关键词路过滤无区分度术语）
_STOPWORDS = {
    "的", "和", "与", "及", "了", "等", "相关", "情况", "方面", "内容",
    "进行", "是否", "能力", "包括", "以及", "具有", "要求", "根据",
}


@dataclass
class RetrievalResult:
    """单条检索结果（证据溯源用）。score 为 RRF 融合分数。"""

    chunk_id: str
    bid_id: str
    lot_id: str
    content: str
    chapter_title: str
    page_no: int
    score: float
    source: str  # vector / keyword / structured


def _keyword_terms(dimension: ScoringDimension, criteria: list[ScoringCriterion]) -> list[str]:
    """从维度名 + 评分标准文本提取检索术语（英文单词 + 中文 2-4 字词窗）。"""
    texts = [dimension.name]
    texts += [c.name or "" for c in criteria]
    texts += [c.description or "" for c in criteria]
    texts += [c.scoring_rubric or "" for c in criteria]
    terms: set[str] = set()
    for t in texts:
        if not t:
            continue
        terms.update(re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", t))
        for w in re.findall(r"[一-鿿]{2,4}", t):
            if w not in _STOPWORDS:
                terms.add(w)
    return list(terms)


def _score_keywords(chunks: list[dict], terms: list[str]) -> list[tuple[str, float]]:
    """路2：每 chunk 命中术语数（不重复计数）作得分，按分降序。"""
    scored = []
    for c in chunks:
        n = sum(1 for t in terms if t and t in (c.get("content") or ""))
        if n > 0:
            scored.append((c["chunk_id"], float(n)))
    scored.sort(key=lambda x: -x[1])
    return scored


def _search_vector(query_vec, lot_id: str, bid_id: str, top_k: int) -> list[tuple[str, float]]:
    """路1：Milvus 向量检索。返回 [(chunk_id, score)]。"""
    from app.core.milvus import get_collection

    collection = get_collection()
    collection.load()
    expr = f'lot_id == "{lot_id}"'
    if bid_id:
        expr += f' && bid_id == "{bid_id}"'
    hits = collection.search(
        data=[query_vec],
        anns_field="embedding",
        param={"metric_type": "IP", "params": {"nprobe": 16}},
        limit=top_k,
        expr=expr,
        output_fields=["chunk_id"],
    )[0]
    return [(h.entity.get("chunk_id"), h.score) for h in hits]


def _query_all_chunks(lot_id: str, bid_id: str, limit: int = _KEYWORD_SCAN_LIMIT) -> list[dict]:
    """拉取该标书全量 chunk（供路2 打分 + 结果组装）。"""
    from app.core.milvus import get_collection

    collection = get_collection()
    collection.load()
    return collection.query(
        expr=f'lot_id == "{lot_id}" && bid_id == "{bid_id}"',
        output_fields=["chunk_id", "content", "chapter_title", "page_no"],
        limit=limit,
    )


async def _structured_match(query: str, bid_id: str) -> list[tuple[str, float]]:
    """路3：structured_data 精确查。返回 [(伪 chunk_id 标记, 得分)]。"""
    async with session_factory() as session:
        bid = await session.get(BidDocument, bid_id)
    if bid is None or not bid.structured_data:
        return []
    out = []
    for k, v in bid.structured_data.items():
        if v is not None and str(v) in query:
            out.append((f"structured:{bid_id}:{k}", 1.0))
    return out


def _rrf_fuse(routes: dict[str, list[tuple[str, float]]], k: int = 60, top_n: int = 8) -> list[tuple[str, float]]:
    """RRF 融合：score = Σ 1/(k+rank)，取 Top-N。"""
    from collections import defaultdict

    agg: dict[str, float] = defaultdict(float)
    for ranked in routes.values():
        for rank, (cid, _score) in enumerate(ranked, start=1):
            agg[cid] += 1.0 / (k + rank)
    return sorted(agg.items(), key=lambda x: -x[1])[:top_n]


async def _retrieve_internal(
    query: str,
    *,
    lot_id: str,
    bid_id: str,
    dimension: ScoringDimension | None = None,
    top_k: int = 8,
    k_rrf: int = 60,
) -> tuple[list[RetrievalResult], float | None, bool]:
    """多路召回 + RRF 融合 → Top-K。返回 (results, max_semantic_score, semantic_ok)。

    dimension 提供路2 关键词（评分标准术语）；为 None 时只走向量+结构化。
    路3 结构化结果（无 chunk_id）附加在列表尾部（source='structured'）。
    Milvus 检索超时（SEMANTIC_TIMEOUT_SECONDS）→ semantic_ok=False，只走关键词+结构化。
    """
    # 路1：query 向量化 + Milvus 检索（10s 超时，超时/故障降级到关键词+结构化）
    # 异常兜底（P8）：BGE-M3 / Milvus 任一故障 → semantic_ok=False 走关键词+结构化，
    # 不让单一中间件故障整体打断检索（失败偏置而非 fail-stop）。
    semantic_ok = True
    qvec = None
    try:
        qvec = (await get_embedder().embed([query]))[0]
    except Exception as e:  # noqa: BLE001  embedding 服务不可用
        semantic_ok = False
        logger.warning("retriever.embedding_failed", lot_id=lot_id, bid_id=bid_id, error=str(e))
    if qvec is None:
        hits_v = []
    else:
        try:
            hits_v = await asyncio.wait_for(
                asyncio.to_thread(_search_vector, qvec, lot_id, bid_id, 20),
                timeout=SEMANTIC_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            semantic_ok = False
            hits_v = []
            logger.warning("retriever.semantic_timeout", lot_id=lot_id, bid_id=bid_id)
        except Exception as e:  # noqa: BLE001  非超时故障（连接/gRPC 等）
            semantic_ok = False
            hits_v = []
            logger.warning("retriever.semantic_failed", lot_id=lot_id, bid_id=bid_id, error=str(e))
    # 关键词路拉全量 chunk：Milvus query 挂也降级（结构化路仍可用）
    try:
        all_chunks = await asyncio.wait_for(
            asyncio.to_thread(_query_all_chunks, lot_id, bid_id),
            timeout=SEMANTIC_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001  含 TimeoutError
        semantic_ok = False
        all_chunks = []
        logger.warning("retriever.chunks_failed", lot_id=lot_id, bid_id=bid_id, error=str(e))

    # 路2：关键词（评分标准术语 + query 关键短语）
    terms: list[str] = []
    if dimension is not None:
        async with session_factory() as session:
            criteria = (
                await session.scalars(
                    select(ScoringCriterion).where(ScoringCriterion.dimension_id == dimension.dimension_id)
                )
            ).all()
        terms = _keyword_terms(dimension, list(criteria))
    # 补充 query 词窗：无维度/语义降级时关键词路仍能工作
    terms += [t for t in re.findall(r"[一-鿿]{2,4}", query) if t not in _STOPWORDS]
    hits_k: list[tuple[str, float]] = _score_keywords(all_chunks, terms) if terms else []

    # 路3：结构化精确查
    hits_s = await _structured_match(query, bid_id)

    # RRF 融合（向量 + 关键词两路）
    routes = {"vector": hits_v}
    if hits_k:
        routes["keyword"] = hits_k
    fused = _rrf_fuse(routes, k=k_rrf, top_n=top_k)

    chunk_info = {c["chunk_id"]: c for c in all_chunks}
    results: list[RetrievalResult] = []
    for cid, score in fused:
        info = chunk_info.get(cid)
        if info is None:
            continue
        results.append(
            RetrievalResult(
                chunk_id=cid,
                bid_id=bid_id,
                lot_id=lot_id,
                content=info["content"],
                chapter_title=info.get("chapter_title") or "",
                page_no=info.get("page_no") or 0,
                score=score,
                source="vector" if cid in {c[0] for c in hits_v} else "keyword",
            )
        )
    # 结构化结果附加尾部（证据增强，非语义 chunk）
    for marker, s in hits_s:
        results.append(
            RetrievalResult(
                chunk_id=marker,
                bid_id=bid_id,
                lot_id=lot_id,
                content=f"[结构化数据] {marker}",
                chapter_title="structured_data",
                page_no=0,
                score=s,
                source="structured",
            )
        )
    max_score = hits_v[0][1] if hits_v and semantic_ok else None
    logger.debug("retriever.done", query=query, lot_id=lot_id, bid_id=bid_id,
                 results=len(results), max_score=max_score, semantic_ok=semantic_ok)
    return results, max_score, semantic_ok


def _confidence_band(max_score: float | None) -> str:
    """检索置信档三档：none（无分/低于拒答阈值视为无关）｜low（[LOW, HIGH) 相关性存疑）｜high。

    仅反映路1 向量置信度（max_score 为路1 最高 IP），阈值集中定义在 degradation.py。
    """
    if max_score is None or max_score < CONFIDENCE_LOW_THRESHOLD:
        return "none"
    if max_score < CONFIDENCE_HIGH_THRESHOLD:
        return "low"
    return "high"


async def retrieve(
    query: str,
    *,
    lot_id: str,
    bid_id: str,
    dimension: ScoringDimension | None = None,
    top_k: int = 8,
    k_rrf: int = 60,
) -> list[RetrievalResult]:
    """多路召回 + RRF 融合 → Top-K（纯结果，P2.2 验收/通用检索用）。"""
    results, _max, _ok = await _retrieve_internal(
        query, lot_id=lot_id, bid_id=bid_id, dimension=dimension, top_k=top_k, k_rrf=k_rrf
    )
    return results


async def retrieve_with_meta(
    query: str,
    *,
    lot_id: str,
    bid_id: str,
    dimension: ScoringDimension | None = None,
    top_k: int = 8,
    k_rrf: int = 60,
    bid_parsed: bool = True,
    return_meta: bool = False,
) -> tuple[list[RetrievalResult], str | None] | tuple[list[RetrievalResult], str | None, dict]:
    """带降级判定的检索入口（评审链路/API 用）。返回 (结果, 提示文案)。

    提示文案由 degradation.classify_retrieval 判定：语义超时降级 / 标书
    未解析（PARSING）/ 全低分拒答（NO_EVIDENCE）；正常返回 None。

    return_meta=True 时追加返回 meta 字典（max_score/semantic_ok/source_count/
    confidence_band），供调用方透出 tool_call 事件与进 prompt 的置信度声明
    （结构见 RETRIEVE_RESULT_SCHEMA）。默认 False 返回二元组，旧调用点零改动。
    """
    results, max_score, semantic_ok = await _retrieve_internal(
        query, lot_id=lot_id, bid_id=bid_id, dimension=dimension, top_k=top_k, k_rrf=k_rrf
    )
    hint = classify_retrieval(max_score, bid_parsed=bid_parsed, semantic_ok=semantic_ok)
    if not return_meta:
        return results, hint
    meta = {
        "max_score": max_score,
        "semantic_ok": semantic_ok,
        "source_count": len(results),
        "confidence_band": _confidence_band(max_score),
    }
    return results, hint, meta


async def compare_across_bids(
    query: str,
    *,
    lot_id: str,
    bid_ids: list[str],
    dimension: ScoringDimension | None = None,
    top_k: int = 5,
) -> list[dict]:
    """跨标书对比检索（P2.5，评后汇总用）。

    同一维度 query 对每份标书独立检索，结果标注 supplier_id 来源，
    供"同 query 多标书对照"（评审汇总/围串标排查）使用。
    返回 [{bid_id, supplier_id, hint, results}]。
    """
    supplier_map: dict[str, str] = {}
    if bid_ids:
        async with session_factory() as session:
            rows = await session.execute(
                select(BidDocument.bid_id, BidDocument.supplier_id).where(
                    BidDocument.bid_id.in_(bid_ids)
                )
            )
            supplier_map = {b: s for b, s in rows.all()}
    out: list[dict] = []
    for bid_id in bid_ids:
        results, hint = await retrieve_with_meta(
            query, lot_id=lot_id, bid_id=bid_id, dimension=dimension, top_k=top_k
        )
        out.append(
            {
                "bid_id": bid_id,
                "supplier_id": supplier_map.get(bid_id),
                "hint": hint,
                "results": results,
            }
        )
    logger.debug("retriever.compare", query=query, lot_id=lot_id, bids=len(bid_ids))
    return out
