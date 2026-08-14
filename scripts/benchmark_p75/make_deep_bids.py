"""P7.5 深度基准语料生成（幂等可重跑）。

5 份深度标书（BID-BENCH-01..05，LOT-BENCH），每份 24 子主题（8 章 × 3 子主题）
渲染全文 → SmartDocumentChunker 分块 → BGE-M3 向量化 → Milvus 先删后插。
同时向 MySQL 写入 LOT-BENCH 的 5 个评分维度 + 评分标准（供 RAG 维度感知 /
AI 评分基准以真实 ORM 对象检索）。

不写 bid_document 表：基准标书仅用于检索/评分基准，不参与评审/围串标/供应商
链路，天然与演示数据隔离；retriever 路3 结构化匹配查不到 bid → 返回空，无副作用。

依赖：bge-m3 容器、sp-mysql、sp-milvus 在线。
用法: poetry run python scripts/benchmark_p75/make_deep_bids.py
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 本包

from sqlalchemy import delete, select  # noqa: E402

from app.ai.rag.chunker import SmartDocumentChunker  # noqa: E402
from app.ai.rag.embedder import get_embedder  # noqa: E402
from app.core.database import session_factory  # noqa: E402
from app.models.project import ScoringCriterion, ScoringDimension  # noqa: E402
from app.tasks.document_ingest import _insert_milvus  # noqa: E402

import bench_data as B  # noqa: E402


async def _upsert_dimensions() -> None:
    """向 MySQL 写 LOT-BENCH 的 5 维度 + 各 3 条评分标准（先删后插幂等）。"""
    async with session_factory() as s:
        dim_ids = [f"DIM-BENCH-{k}" for k in B.DIM_ORDER]
        await s.execute(delete(ScoringCriterion).where(ScoringCriterion.dimension_id.in_(dim_ids)))
        await s.execute(delete(ScoringDimension).where(ScoringDimension.lot_id == B.BENCH_LOT_ID))
        for order, dim in enumerate(B.DIM_ORDER, start=1):
            name, max_score, weight = B.DIMENSIONS[dim]
            dim_id = f"DIM-BENCH-{dim}"
            s.add(ScoringDimension(
                dimension_id=dim_id,
                lot_id=B.BENCH_LOT_ID,
                name=name,
                max_score=Decimal(str(max_score)),
                weight=Decimal(str(weight)),
                sort_order=order,
            ))
            for c_order, (cname, cdesc, crubric) in enumerate(B.DIM_CRITERIA[dim], start=1):
                s.add(ScoringCriterion(
                    criterion_id=f"CRI-{dim_id}-{c_order:02d}",
                    dimension_id=dim_id,
                    name=cname,
                    description=cdesc,
                    scoring_rubric=crubric,
                    max_score=Decimal(str(max_score)),
                    sort_order=c_order,
                ))
        await s.commit()
    print(f"[维度] LOT-BENCH 5 维度 + {sum(len(v) for v in B.DIM_CRITERIA.values())} 条标准已写入")


async def main() -> None:
    chunker = SmartDocumentChunker()
    embedder = get_embedder()
    await _upsert_dimensions()

    for bid_id in B.BENCH_BID_IDS:
        text = B.render_bid_text(bid_id)
        chunks = chunker.chunk(
            text, bid_id=bid_id, lot_id=B.BENCH_LOT_ID, source_file="benchmark"
        )
        if len(chunks) != len(B.SUBTOPICS):
            raise RuntimeError(
                f"{bid_id} 分块数 {len(chunks)} != 子主题数 {len(B.SUBTOPICS)}"
                f"（某子主题正文可能超 max_tokens 被滑窗切碎），停止入库防错位"
            )
        vectors = await embedder.embed([c.content for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(f"{bid_id} 向量数 {len(vectors)} != chunks {len(chunks)}")
        await asyncio.to_thread(_insert_milvus, chunks, vectors, bid_id)
        chapters = {c.chapter_title for c in chunks}
        print(
            f"✓ {bid_id} 字数={len(text)} chunks={len(chunks)} "
            f"章数={len(chapters)} 首chunk={chunks[0].chunk_id} 末chunk={chunks[-1].chunk_id}"
        )
    print("完成：5 份深度标书已入库 Milvus（bid_documents）")


asyncio.run(main())
