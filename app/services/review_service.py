"""评审业务逻辑（P3.3 API / P3.4 编排）。

- create_review()：创建评审工作台（校验 bid=FROZEN + 维度归属标段）
- stream_score()：评分 SSE 事件流。报价维度走纯公式（event:price_calc，不走 AI）；
  其他维度走 AI（检索 → prompt → DeepSeek 流式 → thought 增量 → 解析分数）
- _calc_price_formula()：综合评分法纯公式（基准价=Σ报价/N，得分=maxScore×(1-偏差率)）

SSE 事件序（task.md P3.3）：thinking → source → thought(流式增量) → score → done。
seq 递增（P3.6 断流续推用 Last-Event-ID）。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm.deepseek_client import CircuitOpenError, get_client
from app.ai.llm.prompts import ThinkingAnswerSplitter, build_score_prompt
from app.ai.llm.text_cleaner import clean_bid_text, redact_pii
from app.ai.rag.degradation import DegradationHint
from app.ai.rag.retriever import retrieve_with_meta
from app.core.config import settings
from app.core.crypto import generate_id
from app.core.redis import flush_keys, get_redis, redis_warn_once
from app.core.sse import sse_event
from app.models.bid_document import BidDocument, BidStatus
from app.models.expert import Expert
from app.models.expert_review import ExpertReview, ReviewStatus
from app.models.lot_expert_assignment import LotExpertAssignment
from app.models.project import Lot, ScoringCriterion, ScoringDimension
from app.models.supplier import Supplier

logger = structlog.get_logger(__name__)

# 报价维度名（solution.md：报价评审从 AI 剥离，纯公式）
PRICE_DIMENSION_NAME = "报价"


def _clean_structured_data(data: dict | None) -> dict | None:
    """结构化数据值过 PII 脱敏（防御未来抽取字段扩展含联系方式）。

    quality_cert/warranty_months 当前为短字符串无 PII，此层是防御性兜底。
    """
    if not data:
        return data
    return {k: redact_pii(v) if isinstance(v, str) else v for k, v in data.items()}
# 评分输出解析：prompt 要求最后一行 `分数: X`
_RE_SCORE = __import__("re").compile(r"分数\s*[:：]\s*(\d+(?:\.\d+)?)")
# 兜底：LLM 未按 `分数: X` 输出时，抓 "X分 / Y分" 里的 X（如 "19.0分 / 30.0分"）
_RE_TOTAL_SCORE = __import__("re").compile(r"(\d+(?:\.\d+)?)\s*分\s*[/／]\s*\d+(?:\.\d+)?\s*分")


def _extract_score(full_text: str, max_score: float) -> tuple[float | None, str | None]:
    """从 LLM 输出提取最终分数并校验范围（P8 答非所问兜底）。

    取末行 `分数: X`（_RE_TOTAL_SCORE 兜底），校验 0<=v<=max_score。
    返回 (score, hint)：空输出/无分/越界 → score=None + 对应 DegradationHint；
    正常 → (v, None)。score=None 时前端应展示 hint 提示人工评分，而非把 null
    当有效分数落库。
    """
    if not (full_text or "").strip():
        return None, DegradationHint.EMPTY_OUTPUT
    nums = _RE_SCORE.findall(full_text) or _RE_TOTAL_SCORE.findall(full_text)
    if not nums:
        return None, DegradationHint.NO_SCORE
    v = float(nums[-1])  # 末行优先：修"取第一个 分数:" 的 bug（LLM 理由正文里可能先出现示例分）
    if v < 0 or v > max_score:
        return None, DegradationHint.SCORE_OUT_OF_RANGE
    return v, None


# ==================== ST1 评分语义缓存（同标段同维度评分结果缓存） ====================
#
# solution.md P2 技术债 ST1：同 bid×dim 的评分流重复触发（前端刷新重评/评测脚本
# 连跑）不再重复打 LLM，重放首次产出的 SSE 帧（命中返回首次结果，用户已拍板）。
# 缓存内容 = 已生成 SSE 帧列表（不含 meta 首帧），零重建逻辑、事件序零偏差、
# usage 透传首次真实值（评测断言 usage 字段齐全且为正）。Redis 挂 → fail-open：
# 读失败=miss 走真实流，写失败忽略，评分主链路不受阻。

# 缓存 key：score:scache:{bid_id}:{dimension_id}:{model}（同标段同维度同模型，跨 review 复用）。
# model 进 key：meta 帧每次重放重新生成（带当前 settings.deepseek_model），若 body 帧是旧模型
# 产物而 meta 报新模型，客户端会看到自相矛盾的流 → model 变更自然 miss 重新评分。
_SCORE_CACHE_PREFIX = "score:scache:"
_SCORE_CACHE_TTL_SECONDS = 86400  # 24h，一次评审会议窗口


def _score_cache_key(bid_id: str, dimension_id: str) -> str:
    return f"{_SCORE_CACHE_PREFIX}{bid_id}:{dimension_id}:{settings.deepseek_model}"


async def _load_score_frames(cache_key: str) -> list[str] | None:
    """读评分缓存帧列表。未命中/Redis 挂 → None（miss 走真实流）。"""
    try:
        frames = await get_redis().lrange(cache_key, 0, -1)
        return list(frames) if frames else None
    except Exception as e:  # noqa: BLE001  Redis 不可用 → miss，不阻断评分
        await redis_warn_once("review.score_cache_read_down", str(e))
        return None


async def _save_score_frames(cache_key: str, frames: list[str]) -> None:
    """写评分缓存帧列表（TTL 24h）。Redis 挂忽略（fail-open，仅限频告警）。

    MULTI/EXEC 原子写：delete+rpush+expire 一次提交。若分三步非原子，两个并发
    miss（同 bid×dim，各自真实调 LLM）会交错 append 两批帧污染缓存列表 → 重放
    畸形流（重复 done/重复 seq），破"事件序零偏差"契约。
    """
    if not frames:
        return
    try:
        r = get_redis()
        pipe = r.pipeline(transaction=True)
        pipe.delete(cache_key)  # 先删后推，保证幂等（同 key 重评刷新）
        pipe.rpush(cache_key, *frames)
        pipe.expire(cache_key, _SCORE_CACHE_TTL_SECONDS)
        await pipe.execute()
        logger.info("review.score_cache_set", key=cache_key, frames=len(frames))
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("review.score_cache_set_down", str(e))


async def flush_score_cache(bid_id: str | None = None) -> int:
    """失效评分缓存：指定标书按 bid 前缀删；None 全量删（Milvus 重建/llm.* 配置变更）。

    Redis 挂 → 告警并返回 0（缓存失效失败可见，TTL 24h 兜底兜底）。调用方无需 try/except。
    """
    try:
        if bid_id is not None:
            return await flush_keys(f"{_SCORE_CACHE_PREFIX}{bid_id}:")
        return await flush_keys(_SCORE_CACHE_PREFIX)
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("review.score_cache_flush_down", str(e))
        return 0


class LotNotFoundError(ValueError):
    """标段不存在 → 404。"""


class ReviewNotFoundError(ValueError):
    """评审记录不存在 → 404。"""


class BidNotFrozenError(ValueError):
    """标书未封存（仅 FROZEN 可评审）→ 400。"""


class DimensionMismatchError(ValueError):
    """维度不属于该标段 → 400。"""


class ReviewAccessDeniedError(ValueError):
    """非本人评审 → 403。"""


class ReviewLockedError(ValueError):
    """评审已提交锁定，不可回改 → 400。"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_review(
    session: AsyncSession,
    *,
    expert_id: str,
    bid_id: str,
    dimension_id: str,
) -> ExpertReview:
    """创建评审工作台。校验 bid=FROZEN + 维度归属标段 + 专家分配归属（task.md P3.3）。"""
    bid = await session.get(BidDocument, bid_id)
    if bid is None:
        raise ReviewNotFoundError(f"标书不存在: {bid_id}")
    if bid.status != BidStatus.FROZEN:
        raise BidNotFrozenError(f"标书未封存（当前 {bid.status}），仅 FROZEN 可发起评审")
    dim = await session.get(ScoringDimension, dimension_id)
    if dim is None:
        raise DimensionMismatchError(f"评分维度不存在: {dimension_id}")
    if dim.lot_id != bid.lot_id:
        raise DimensionMismatchError(f"维度 {dimension_id} 不属于标段 {bid.lot_id}")
    # 评审越权防护：专家须为标段被分配评审员，且维度在其负责维度内
    assignment = await session.scalar(
        select(LotExpertAssignment).where(
            LotExpertAssignment.expert_id == expert_id,
            LotExpertAssignment.lot_id == bid.lot_id,
        )
    )
    if assignment is None:
        raise ReviewAccessDeniedError(f"专家 {expert_id} 未被分配标段 {bid.lot_id} 评审")
    if assignment.dimension_ids and dimension_id not in assignment.dimension_ids:
        raise ReviewAccessDeniedError(f"维度 {dimension_id} 不在专家 {expert_id} 的分配维度内")

    now = _now()
    review = ExpertReview(
        review_id=generate_id("REV"),
        expert_id=expert_id,
        bid_id=bid_id,
        dimension_id=dimension_id,
        status=ReviewStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )
    session.add(review)
    await session.commit()
    await session.refresh(review)
    logger.info("review.created", review_id=review.review_id, expert_id=expert_id,
                bid_id=bid_id, dimension_id=dimension_id)
    return review


def _calc_price_formula(bid_amount: float, dim_max_score: float, lot_bids: list[float]) -> dict:
    """综合评分法纯公式（solution.md 5.3 报价剥离）。

    基准价 = Σ有效报价 / N；偏差率 = |报价 - 基准价| / 基准价；
    得分 = maxScore × (1 - 偏差率)，下限 0。返回公式与结果（可审计，不走 AI）。
    """
    amounts = [a for a in lot_bids if a]
    base = sum(amounts) / len(amounts) if amounts else 0
    deviation = abs(bid_amount - base) / base if base else 0
    score = max(0.0, round(float(dim_max_score) * (1 - deviation), 2))
    return {
        "dimension": PRICE_DIMENSION_NAME,
        "formula": (
            f"基准价=Σ报价/N={round(base, 2)}，"
            f"得分={dim_max_score}×(1-|报价-基准价|/基准价)={score}"
        ),
        "result": {
            "bidAmount": bid_amount,
            "basePrice": round(base, 2),
            "deviationPct": round(deviation * 100, 2),
            "calculatedScore": score,
            "maxScore": float(dim_max_score),
        },
    }


async def stream_score(
    session: AsyncSession,
    *,
    review_id: str,
    expert_id: str,
) -> AsyncIterator[str]:
    """评分 SSE 事件流（报价→price_calc；其他→AI 流式评分）。"""
    review = await session.get(ExpertReview, review_id)
    if review is None:
        raise ReviewNotFoundError(f"评审记录不存在: {review_id}")
    if review.expert_id != expert_id:
        raise ReviewAccessDeniedError("不能评审非本人分配的任务")

    dim = await session.get(ScoringDimension, review.dimension_id)
    bid = await session.get(BidDocument, review.bid_id)
    if dim is None or bid is None:
        raise ReviewNotFoundError(f"评审关联数据缺失: review={review_id}")

    seq = 0
    # 契约 meta 首帧（评测 §5.1）：每个 SSE 流统一透出 agent 元信息
    yield sse_event("meta", {
        "agent": "smart-procurement",
        "model": settings.deepseek_model,
        "interface": "POST /reviews/{review_id}/score",
        "contract_version": "1.0",
        "git_sha": "",
        "knowledge_version": "",
    }, seq := seq + 1)
    # 报价维度：纯公式（零延迟、可审计，不走 AI）
    if dim.name == PRICE_DIMENSION_NAME:
        lot_bids = (
            await session.scalars(
                select(BidDocument.bid_amount).where(
                    BidDocument.lot_id == bid.lot_id,
                    BidDocument.status == BidStatus.FROZEN,
                )
            )
        ).all()
        calc = _calc_price_formula(
            float(bid.bid_amount or 0), float(dim.max_score or 0), [float(b) for b in lot_bids if b]
        )
        yield sse_event("thinking", {"stage": "PRICE_CALC"}, seq := seq + 1)
        yield sse_event("price_calc", calc, seq := seq + 1)
        # done 带结构化分数（契约 §5.1 扩展：评分任务显式透出，评测端不依赖正则提取）
        yield sse_event("done", {
            "content": calc.get("formula", ""),
            "score": calc.get("result", {}).get("calculatedScore"),
        }, seq := seq + 1)
        return

    # ==================== AI 评分 ====================
    query = f"针对{dim.name}维度，依据评分标准评审该标书"
    # ST1 评分语义缓存：同标段同维度结果重放（命中 → 不重复打 LLM）。meta 帧
    # 已正常发出，缓存帧 seq 从 2 起衔接自洽；重放返回"首次结果"（用户已拍板），
    # usage 透传首次真实值（评测断言 usage 齐全且为正，不能发 0）。
    cache_key = _score_cache_key(bid.bid_id, dim.dimension_id)
    cached = await _load_score_frames(cache_key)
    if cached is not None:
        logger.info("review.cache_hit", bid_id=bid.bid_id, dimension_id=dim.dimension_id,
                    frames=len(cached))
        for fr in cached:
            yield fr
        return
    # 帧收集：AI 分支产出的帧全部进 frames，正常走完 → 写缓存；NO_EVIDENCE /
    # CircuitOpenError / 客户端中断（generator 未走完）提前 return，frames 不落缓存。
    frames: list[str] = []

    def emit(event: str, data: dict) -> str:
        """生成 SSE 帧（seq 衔接不变）并收集进评分缓存帧列表。"""
        nonlocal seq
        frame = sse_event(event, data, seq := seq + 1)
        frames.append(frame)
        return frame

    yield emit("thinking", {"stage": "RETRIEVING"})
    # return_meta：附带检索质量元信息（source_count/max_score/confidence_band），
    # 供 tool_call 事件透出 + prompt 置信度声明；默认二元组不受影响
    results, hint, meta = await retrieve_with_meta(
        query, lot_id=bid.lot_id, bid_id=bid.bid_id, dimension=dim, top_k=5,
        return_meta=True,
    )

    # 检索结果 → source 事件（证据溯源，旧前端）+ 契约 tool_call（评测端观测检索动作）
    # P8.2：透出 page_range 页码范围（溯源面板"第 N 页/第 A-B 页"），加字段 additive 契约兼容
    retrievals: list[dict] = []
    for r in results:
        if r.source in ("vector", "keyword"):
            retrievals.append({
                "chunk_id": r.chunk_id, "chapter_title": r.chapter_title,
                "page_range": r.page_range, "score": round(r.score, 4),
            })
            yield emit(
                "source",
                {"chunk_id": r.chunk_id, "content": r.content[:500],
                 "chapter_title": r.chapter_title, "page_range": r.page_range,
                 "score": round(r.score, 4)},
            )
    yield emit("tool_call", {
        "id": str(time.time_ns()),
        "name": "knowledge_retrieval",
        "args": {"query": query[:50]},
        "result": retrievals,
        "status": "success",
        # 检索质量元信息（对齐 RETRIEVE_RESULT_SCHEMA）：评测端观测检索动作
        # （命中条数/语义相似度/置信档位/降级状态），加字段不改事件序，契约 §5.1 兼容
        "source_count": meta["source_count"],
        "max_score": meta["max_score"],
        "confidence_band": meta["confidence_band"],
        "semantic_ok": meta["semantic_ok"],
        "hint": hint,
    })

    # 无可用依据 → 拒答事件（P2.4 降级提示），不调 LLM（零成本，不缓存）
    if hint is not None and not results:
        yield emit("thinking", {"stage": "NO_EVIDENCE", "hint": hint})
        yield emit("done", {})
        return

    # 组装 rubric + chunks → Prompt
    criteria = (
        await session.scalars(
            select(ScoringCriterion).where(ScoringCriterion.dimension_id == dim.dimension_id)
        )
    ).all()
    rubric_lines = [
        f"- {c.name}（{c.max_score} 分）：{c.scoring_rubric or c.description or ''}"
        for c in criteria
    ]
    rubric = "\n".join(rubric_lines) if rubric_lines else f"{dim.name} 满分 {dim.max_score} 分"
    # P7.x 输入侧清洗：检索结果进 <bid_content> 前规范化 + PII 脱敏（真实标书
    # 身份证/手机/邮箱明文进 prompt，LLM 会复述泄漏）；structured_data 值兜底脱敏
    prompt = build_score_prompt(
        dimension_name=dim.name,
        max_score=float(dim.max_score or 0),
        rubric=rubric,
        chunks=[clean_bid_text(r.content) for r in results if r.source in ("vector", "keyword")],
        structured_data=_clean_structured_data(bid.structured_data),
        # P7.x 置信度进 prompt：低置信/降级时注入 <retrieval_meta>，LLM 理由中如实指出依据缺口
        retrieval_meta=meta,
    )

    # LLM 流式评分。P7.x：prompt 契约要求先 <thinking> 推理、再 <answer> 结论；切分器
    # 把推理段单独透出 reasoning 事件，answer/thought 只发结论段（full_text 仅存结论，
    # 思考不进评论/历史）。无标签降级全文当 answer，契约不破。7.4 cache 字段：
    # DeepSeek 响应带 prompt_cache_hit/miss_tokens，累加透传评测平台按命中价计成本
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                   "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
    yield emit("thinking", {"stage": "REASONING"})
    full_text = ""
    splitter = ThinkingAnswerSplitter()
    try:
        async for delta, u in get_client().chat_stream(prompt, max_tokens=2048):
            if u:
                for _k in total_usage:
                    total_usage[_k] += u.get(_k, 0) or 0
            for kind, piece in splitter.feed(delta):
                if not piece:
                    continue
                if kind == "reasoning":
                    yield emit("reasoning", {"delta": piece})
                else:
                    full_text += piece
                    yield emit("answer", {"delta": piece})
                    yield emit("thought", {"delta": piece})
        for kind, piece in splitter.flush():
            if not piece:
                continue
            if kind == "reasoning":
                yield emit("reasoning", {"delta": piece})
            else:
                full_text += piece
                yield emit("answer", {"delta": piece})
                yield emit("thought", {"delta": piece})
    except CircuitOpenError:
        yield emit("thinking", {"stage": "LLM_DOWN"})
        yield emit("usage", total_usage)
        yield emit("done", {"content": full_text})
        return

    # 解析分数（prompt 要求末行 `分数: X`，_RE_TOTAL_SCORE 兜底）+ 范围校验 → score 事件
    # P8：空输出/无分/越界 → score=None + hint（人工评分兜底），杜绝越界分/负分落库
    score_val, score_hint = _extract_score(full_text, float(dim.max_score or 0))
    yield emit(
        "score",
        {"score": score_val, "comment": full_text[:2000], "hint": score_hint},
    )
    yield emit("usage", total_usage)
    # done 带结构化分数（§5.1 扩展：评测端直接取 score，不依赖正则；解析不到为 null）
    yield emit(
        "done", {"content": full_text, "score": score_val, "hint": score_hint}
    )
    # ST1：正常走完 AI 评分（已发 done）→ 写缓存，后续同 bid×dim 重复评分直接重放。
    # 注意：async for 消费到 StopIteration 才会执行到这（即 done 已逐帧发出后落缓存）。
    await _save_score_frames(cache_key, frames)


# ==================== P3.4：暂存 / 提交 / 锁定 ====================

async def save_score(
    session: AsyncSession,
    *,
    review_id: str,
    expert_id: str,
    score: float,
    comment: str,
    ai_suggestion: dict | None = None,
) -> ExpertReview:
    """维度评分暂存（DRAFT）。已提交锁定（CONFIRMED/MANUAL_ADJUSTED）不可回改 → 400。

    ai_suggestion 保留 AI 建议分（提交时判定 MANUAL_ADJUSTED 依据）。
    """
    review = await session.get(ExpertReview, review_id)
    if review is None:
        raise ReviewNotFoundError(f"评审记录不存在: {review_id}")
    if review.expert_id != expert_id:
        raise ReviewAccessDeniedError("不能修改非本人评审")
    if review.status in (ReviewStatus.CONFIRMED, ReviewStatus.MANUAL_ADJUSTED):
        raise ReviewLockedError(f"评审已提交锁定（{review.status}），不可回改")

    from decimal import Decimal

    review.score = Decimal(str(score))
    review.comment = comment
    review.ai_suggestion = ai_suggestion
    review.status = ReviewStatus.DRAFT
    review.updated_at = _now()
    await session.commit()
    await session.refresh(review)
    logger.info("review.saved", review_id=review_id, score=score, status=review.status)
    return review


async def submit_review(
    session: AsyncSession,
    *,
    review_id: str,
    expert_id: str,
) -> ExpertReview:
    """提交评审并锁定。DRAFT → CONFIRMED（采纳 AI 建议）/ MANUAL_ADJUSTED（手动改过）。

    提交后不可回改（save_score 拒绝）。已提交状态幂等返回（重复提交无害）。
    """
    review = await session.get(ExpertReview, review_id)
    if review is None:
        raise ReviewNotFoundError(f"评审记录不存在: {review_id}")
    if review.expert_id != expert_id:
        raise ReviewAccessDeniedError("不能提交非本人评审")
    if review.status in (ReviewStatus.CONFIRMED, ReviewStatus.MANUAL_ADJUSTED):
        return review  # 已提交，幂等

    # 手动调整判定：score 与 AI 建议分不一致（ai_suggestion.score）
    ai_score = None
    if review.ai_suggestion:
        ai_score = review.ai_suggestion.get("score") or review.ai_suggestion.get("suggestion_score")
    manual = ai_score is not None and review.score is not None and abs(float(review.score) - float(ai_score)) > 0.01
    review.status = ReviewStatus.MANUAL_ADJUSTED if manual else ReviewStatus.CONFIRMED
    review.updated_at = _now()
    await session.commit()
    await session.refresh(review)
    logger.info("review.submitted", review_id=review_id, status=review.status, manual=manual)
    return review


# ==================== P6.4：评审历史 ====================

async def list_my_reviews(
    session: AsyncSession, *, expert_id: str, page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    """专家本人的已提交评审历史（P6.4 评审历史页）。

    仅统计 CONFIRMED / MANUAL_ADJUSTED（已提交锁定）；带标段/标书/供应商/维度名。
    """
    base = select(ExpertReview).where(
        ExpertReview.expert_id == expert_id,
        ExpertReview.status.in_((ReviewStatus.CONFIRMED, ReviewStatus.MANUAL_ADJUSTED)),
    )
    total = len((await session.scalars(base)).all())
    reviews = (
        await session.scalars(
            base.order_by(ExpertReview.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()

    bid_ids = {r.bid_id for r in reviews}
    dim_ids = {r.dimension_id for r in reviews}

    # bid → (lot_id, supplier_name)
    bid_info = {}
    if bid_ids:
        bid_rows = (
            await session.execute(
                select(BidDocument, Supplier)
                .join(Supplier, BidDocument.supplier_id == Supplier.supplier_id)
                .where(BidDocument.bid_id.in_(bid_ids))
            )
        ).all()
        bid_info = {b.bid_id: (b.lot_id, s.name) for b, s in bid_rows}
    lot_map = {}
    lot_ids = {info[0] for info in bid_info.values() if info[0]}
    if lot_ids:
        lots = (
            await session.scalars(select(Lot).where(Lot.lot_id.in_(lot_ids)))
        ).all()
        lot_map = {l.lot_id: l.name for l in lots}
    dim_map = {}
    if dim_ids:
        dims = (
            await session.scalars(
                select(ScoringDimension).where(ScoringDimension.dimension_id.in_(dim_ids))
            )
        ).all()
        dim_map = {d.dimension_id: (d.name, float(d.max_score or 0)) for d in dims}

    items = []
    for r in reviews:
        dim_name, dim_max = dim_map.get(r.dimension_id, (r.dimension_id, None))
        lot_id, supplier_name = bid_info.get(r.bid_id, (None, ""))
        items.append({
            "review_id": r.review_id,
            "bid_id": r.bid_id,
            "supplier_name": supplier_name,
            "lot_id": lot_id,
            "lot_name": lot_map.get(lot_id, ""),
            "dimension_id": r.dimension_id,
            "dimension_name": dim_name,
            "max_score": dim_max,
            "score": float(r.score) if r.score is not None else None,
            "status": r.status,
            "submitted_at": r.updated_at.isoformat() if r.updated_at else None,
        })
    return items, total


# ==================== P6.3：评审进度一览 ====================

async def get_lot_review_progress(session: AsyncSession, *, lot_id: str) -> dict:
    """评审进度一览（P6.3 评审进度页）。

    按标书 × 维度展开格子（矩阵），每格显示评审专家、得分与状态：
    - CONFIRMED / MANUAL_ADJUSTED：已提交（计入 done）
    - DRAFT：进行中
    - 无评审记录：未分配
    进度 = done / (标书数 × 维度数)。同格多评审记录取最新提交的一条。
    """
    lot = await session.get(Lot, lot_id)
    if lot is None:
        raise LotNotFoundError(f"标段不存在: {lot_id}")

    dims = (
        await session.scalars(
            select(ScoringDimension)
            .where(ScoringDimension.lot_id == lot_id)
            .order_by(ScoringDimension.sort_order)
        )
    ).all()
    rows = (
        await session.execute(
            select(BidDocument, Supplier)
            .join(Supplier, BidDocument.supplier_id == Supplier.supplier_id)
            .where(BidDocument.lot_id == lot_id, BidDocument.status == BidStatus.FROZEN)
            .order_by(BidDocument.bid_amount)
        )
    ).all()
    bid_ids = [bid.bid_id for bid, _ in rows]
    reviews = (
        await session.scalars(select(ExpertReview).where(ExpertReview.bid_id.in_(bid_ids)))
    ).all() if bid_ids else []

    expert_ids = {r.expert_id for r in reviews}
    experts = {
        e.expert_id: e.name
        for e in (await session.scalars(select(Expert).where(Expert.expert_id.in_(expert_ids)))).all()
    } if expert_ids else {}

    def _fmt(v):
        return float(v) if v is not None else None

    done = 0
    bids = []
    for bid, supplier in rows:
        cells = []
        for d in dims:
            rs = [r for r in reviews if r.bid_id == bid.bid_id and r.dimension_id == d.dimension_id]
            # 同一维度多评审：取已提交优先，其次最新创建的
            rs.sort(key=lambda r: (r.status in (ReviewStatus.CONFIRMED, ReviewStatus.MANUAL_ADJUSTED), r.created_at or _now()))
            if rs and rs[-1].status in (ReviewStatus.CONFIRMED, ReviewStatus.MANUAL_ADJUSTED):
                done += 1
            r = rs[-1] if rs else None
            cells.append({
                "dimension_id": d.dimension_id,
                "dimension_name": d.name,
                "max_score": _fmt(d.max_score),
                "review_id": r.review_id if r else None,
                "expert_id": r.expert_id if r else None,
                "expert_name": experts.get(r.expert_id) if r else None,
                "score": _fmt(r.score) if r and r.score is not None else None,
                "review_status": r.status if r else None,
            })
        bids.append({
            "bid_id": bid.bid_id,
            "supplier_id": supplier.supplier_id,
            "supplier_name": supplier.name,
            "status": bid.status,
            "cells": cells,
        })

    total = len(bids) * len(dims)
    return {
        "lot": {
            "lot_id": lot.lot_id,
            "lot_code": lot.lot_code,
            "name": lot.name,
            "status": lot.status,
            "budget": _fmt(lot.budget),
        },
        "bids": bids,
        "progress": {
            "total": total,
            "done": done,
            "pending": total - done,
            "percent": round(done / total * 100, 1) if total else 0,
        },
    }
