"""P7.5 AI 评分基准（真实 DeepSeek，15 点 = 3 标书 × 5 维度）。

每点流程（与生产评审链路一致）：
  检索（retrieve，dimension 提供 rubric 术语注入）→ build_score_prompt
  → DeepSeek chat → 解析 `分数: X`。

指标：
- MAE：误差归一化为满分 10 制（|LLM - GT| / max_score × 10），15 点平均 ≤2.0
- Kendall tau：每维度 3 标书 LLM 排名 vs GT 排名，5 维度平均 ≥0.7
- 引用可验证：评分理由包含任一检索 chunk 的 anchor 词（维度章节子主题锚词）
  ≥80%

GT = QUALITY_FACTOR[标书维度档位] × max_score（合成标注，见 bench_data）。
验收不达标记录 issue，不改数据。

用法: poetry run python scripts/benchmark_p75/benchmark_ai.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app.ai.llm.deepseek_client import get_client  # noqa: E402
from app.ai.llm.prompts import build_score_prompt  # noqa: E402
from app.ai.rag.retriever import retrieve  # noqa: E402
from app.core.database import session_factory  # noqa: E402
from app.models.project import ScoringDimension  # noqa: E402

import bench_data as B  # noqa: E402

# 评分理由中提取总分的宽容正则（"分数: 25" / "总分：25 分" / "25分"）
_RE_SCORE = re.compile(r"(?:总分|分数)\s*[:：]\s*(\d+(?:\.\d+)?)")
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


def _rubric_text(dim_key: str) -> str:
    lines = [f"{B.DIMENSIONS[dim_key][0]}（满分 {B.DIMENSIONS[dim_key][1]}）评分标准："]
    for name, desc, rubric in B.DIM_CRITERIA[dim_key]:
        lines.append(f"- {name}（{desc}）：{rubric}")
    return "\n".join(lines)


def _dimension_anchors(dim_key: str) -> list[str]:
    """该维度章节 3 个子主题的 anchor 词（引用可验证的判定锚点）。"""
    chapter = B.DIM_TO_CHAPTER[dim_key]
    anchors = []
    for st in B.SUBTOPICS:
        if st["chapter"] == chapter:
            anchors.extend(st["anchor"])
    return [a for a in anchors if a]


def _kendall_tau(a: list[float], b: list[float]) -> float:
    """无并列 Kendall tau。n=3 时取值 ∈ {-1, -1/3, 1/3, 1}。"""
    n = len(a)
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            sign_a = (a[i] - a[j]) * (b[i] - b[j])
            if sign_a > 0:
                pairs += 1
            elif sign_a < 0:
                pairs -= 1
    return pairs / (n * (n - 1) / 2)


async def main() -> None:
    client = get_client()
    async with session_factory() as s:
        dims = {
            d.dimension_id: d
            for d in (
                await s.scalars(
                    select(ScoringDimension).where(ScoringDimension.lot_id == B.BENCH_LOT_ID)
                )
            ).all()
        }
    if len(dims) != len(B.DIM_ORDER):
        print("!! 未找到基准维度，先跑 make_deep_bids.py")
        return
    gt = B.ai_ground_truth()

    print("[AI 评分] 15 点（3 标书 × 5 维度，真实 DeepSeek）")
    scores: dict[str, dict[str, float]] = {bid: {} for bid in B.AI_SCORE_BIDS}
    verified = 0
    total = 0
    for bid in B.AI_SCORE_BIDS:
        for dim_key in B.DIM_ORDER:
            dim_id = f"DIM-BENCH-{dim_key}"
            dim = dims[dim_id]
            query = B.DIMENSION_QUERIES[dim_key][0]
            results = await retrieve(
                query, lot_id=B.BENCH_LOT_ID, bid_id=bid,
                dimension=dim, top_k=8,
            )
            chunks = [r.content for r in results]
            prompt = build_score_prompt(
                dimension_name=dim.name,
                max_score=dim.max_score,
                rubric=_rubric_text(dim_key),
                chunks=chunks,
            )
            text = await client.chat(prompt, max_tokens=2048)
            m = _RE_SCORE.search(text)
            if not m:
                print(f"  [未解析分数] {bid} {dim_key} raw={text[:80]!r}")
                scores[bid][dim_key] = 0.0
            else:
                scores[bid][dim_key] = float(m.group(1))
            total += 1
            # 引用可验证：评分理由包含任一维度章节 anchor 词
            anchors = _dimension_anchors(dim_key)
            cited = [a for a in anchors if a and a in text]
            ok = len(cited) > 0
            verified += 1 if ok else 0
            print(
                f"  {bid} {dim_key:<4} LLM={scores[bid][dim_key]:>5} "
                f"GT={gt[bid][dim_key]:>3}/{int(dim.max_score):>2} "
                f"cite={'✓' if ok else '✗'} anchors={len(anchors)}"
            )

    # ---- MAE（满分 10 制归一）----
    errs: list[float] = []
    for bid in B.AI_SCORE_BIDS:
        for dim_key in B.DIM_ORDER:
            _, max_score, _ = B.DIMENSIONS[dim_key]
            errs.append(abs(scores[bid][dim_key] - gt[bid][dim_key]) / max_score * 10)
    mae = sum(errs) / len(errs)
    print(f"\n[MAE] = {mae:.3f}（满分 10 制，{len(errs)} 点）")
    check("MAE ≤ 2.0", mae <= 2.0, f"mae={mae:.3f}")

    # ---- Kendall tau（每维度 3 标书排名）----
    taus = []
    for dim_key in B.DIM_ORDER:
        llm_rank = [scores[b][dim_key] for b in B.AI_SCORE_BIDS]
        gt_rank = [float(gt[b][dim_key]) for b in B.AI_SCORE_BIDS]
        tau = _kendall_tau(llm_rank, gt_rank)
        taus.append(tau)
        print(f"  {dim_key}: LLM={[round(scores[b][dim_key], 1) for b in B.AI_SCORE_BIDS]} "
              f"GT={[gt[b][dim_key] for b in B.AI_SCORE_BIDS]} tau={tau:+.2f}")
    avg_tau = sum(taus) / len(taus)
    print(f"[Kendall] 平均 = {avg_tau:.3f}")
    check("Kendall ≥ 0.7", avg_tau >= 0.7, f"tau={avg_tau:.3f}")

    # ---- 引用可验证 ----
    ratio = verified / total
    print(f"[引用可验证] = {ratio:.0%}（{verified}/{total}）")
    check("引用可验证 ≥ 80%", ratio >= 0.80, f"ratio={ratio:.2f}")

    print(f"\n========== AI 评分基准: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
