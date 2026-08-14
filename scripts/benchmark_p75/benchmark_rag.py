"""P7.5 RAG 检索基准（真实深度语料 + 真实 BGE-M3 检索）。

- Recall@5 / MRR：30 条 query（5 标书 × 6 子主题），GT = 子主题 chunk_id
  （每子主题 1 chunk，chunk_id = f"{bid_id}-{seq:04d}"，见 bench_data）
- 拒答：10 条不相关 query → retrieve_with_meta → DegradationHint.NO_EVIDENCE
- 维度感知：10 条需求式 query（每维度 2 条）带/不带 dimension 对比
  "维度相关子主题命中率"（该维度章节 3 个子主题任一进 top-5 即命中）

验收标准（task.md P7.5 / memory）：Recall@5 ≥0.85，MRR ≥0.75，
拒答 ≥95%，维度感知带维度 ≥ 无维度且提升 ≥10%。不达标记录 issue。

用法: poetry run python scripts/benchmark_p75/benchmark_rag.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app.ai.rag.degradation import DegradationHint  # noqa: E402
from app.ai.rag.retriever import retrieve, retrieve_with_meta  # noqa: E402
from app.core.database import session_factory  # noqa: E402
from app.models.project import ScoringDimension  # noqa: E402

import bench_data as B  # noqa: E402

TOP_K = 5
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def _load_dimensions() -> dict[str, ScoringDimension]:
    async with session_factory() as s:
        dims = (
            await s.scalars(
                select(ScoringDimension).where(ScoringDimension.lot_id == B.BENCH_LOT_ID)
            )
        ).all()
    return {d.dimension_id: d for d in dims}


def _hit(results, gt_chunk: str) -> bool:
    return any(r.chunk_id == gt_chunk for r in results[:TOP_K])


def _mrr(results, gt_chunk: str) -> float:
    for i, r in enumerate(results[:TOP_K], start=1):
        if r.chunk_id == gt_chunk:
            return 1.0 / i
    return 0.0


async def main() -> None:
    dims = await _load_dimensions()
    if len(dims) != len(B.DIM_ORDER):
        print(f"!! 未找到基准维度（当前 {len(dims)}，需 {len(B.DIM_ORDER)}），先跑 make_deep_bids.py")
        return

    # ---- 1) Recall@5 / MRR（30 条）----
    recall_total = mrr_total = 0
    n = 0
    misses: list[str] = []
    for item in B.rag_queries():
        results = await retrieve(
            item["query"], lot_id=B.BENCH_LOT_ID, bid_id=item["bid_id"], top_k=TOP_K
        )
        gt = B.chunk_id_of(item["bid_id"], item["sub_key"])
        hit = _hit(results, gt)
        recall_total += 1 if hit else 0
        mrr_total += _mrr(results, gt)
        n += 1
        if not hit:
            misses.append(f"{item['bid_id']}@{item['sub_key']}:{item['query'][:20]}")
    recall = recall_total / n
    mrr = mrr_total / n
    print(f"\n[1] Recall@5 = {recall:.3f}（{recall_total}/{n}）  MRR = {mrr:.3f}")
    check("Recall@5 ≥ 0.85", recall >= 0.85, f"recall={recall:.3f}")
    check("MRR ≥ 0.75", mrr >= 0.75, f"mrr={mrr:.3f}")
    for m in misses[:8]:
        print(f"    miss: {m}")

    # ---- 2) 拒答（10 条）----
    refused = 0
    for q in B.REFUSAL_QUERIES:
        _results, hint = await retrieve_with_meta(
            q, lot_id=B.BENCH_LOT_ID, bid_id=B.BENCH_BID_IDS[0], top_k=TOP_K
        )
        if hint == DegradationHint.NO_EVIDENCE:
            refused += 1
        else:
            print(f"    未拒答: {q[:24]} hint={hint}")
    refusal = refused / len(B.REFUSAL_QUERIES)
    print(f"\n[2] 拒答 = {refusal:.0%}（{refused}/{len(B.REFUSAL_QUERIES)}）")
    check("拒答 ≥ 95%", refusal >= 0.95, f"refusal={refusal:.2f}")

    # ---- 3) 维度感知（10 条 × 带/不带 dimension）----
    dim_results = {"with": {"hit": 0, "n": 0}, "without": {"hit": 0, "n": 0}}
    for dim_key in B.DIM_ORDER:
        dim_id = f"DIM-BENCH-{dim_key}"
        dim = dims[dim_id]
        chapter = B.DIM_TO_CHAPTER[dim_key]
        # 该维度章节的 3 个子主题（任一命中 top-5 即算维度相关召回）
        sub_keys = [s["key"] for s in B.SUBTOPICS if s["chapter"] == chapter]
        for q in B.DIMENSION_QUERIES[dim_key]:
            for mode in ("without", "with"):
                kw = {} if mode == "without" else {"dimension": dim}
                results = await retrieve(
                    q, lot_id=B.BENCH_LOT_ID, bid_id=B.BENCH_BID_IDS[0], top_k=TOP_K, **kw
                )
                gts = [B.chunk_id_of(B.BENCH_BID_IDS[0], sk) for sk in sub_keys]
                hit = any(_hit(results, gt) for gt in gts)
                dim_results[mode]["hit"] += 1 if hit else 0
                dim_results[mode]["n"] += 1
    w = dim_results["with"]
    wo = dim_results["without"]
    rw = w["hit"] / w["n"]
    rwo = wo["hit"] / wo["n"]
    lift = (rw - rwo) / rwo if rwo > 0 else (1.0 if rw > 0 else 0.0)
    print(
        f"\n[3] 维度感知 带维度={rw:.2%} 无维度={rwo:.2%} "
        f"提升={lift:+.0%}（{w['hit']}/{w['n']} vs {wo['hit']}/{wo['n']}）"
    )
    check("带维度不劣于无维度", rw >= rwo, f"{rw:.2%} < {rwo:.2%}")
    check("维度感知提升 ≥ 10%", lift >= 0.10, f"lift={lift:.2%}")

    print(f"\n========== RAG 基准: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


asyncio.run(main())
