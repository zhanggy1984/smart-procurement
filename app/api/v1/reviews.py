"""评审 API（P3.3）。

- POST /reviews            创建评审工作台（校验 bid=FROZEN，限 REVIEW_EXPERT）
- POST /reviews/{id}/score SSE 流式评分（X-Idempotency-Key 幂等，报价走 price_calc）
- POST /reviews/{id}/chat  SSE 流式对话（追加 conversation_message + 自动摘要）

SSE 帧：`id:{seq}\nevent:{event}\ndata:{json}\n\n`（core/sse.py）。
评分幂等：前端每次评分携带 X-Idempotency-Key（UUID v4），Redis nx 去重，重复 → 422。
"""

import re
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.config import settings
from app.core.database import get_db_session, session_factory
from app.models.expert import Expert
from app.models.user import Role, User
from app.schemas.review import ChatRequest, ReviewCreate, ReviewOut, SaveScoreRequest
from app.services import review_service as svc
from app.services import conversation_service as conversation
from app.ai.llm.deepseek_client import get_client
from app.ai.llm.prompts import build_chat_prompt
from app.ai.rag.retriever import retrieve_with_meta
from app.core.sse import sse_event
from app.models.bid_document import BidDocument

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["reviews"])

_idem_pool = None


async def _idem_redis():
    """Redis 连接池单例（幂等检查用）。"""
    import redis.asyncio as aioredis

    from app.core.config import settings

    global _idem_pool
    if _idem_pool is None:
        _idem_pool = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _idem_pool


async def _check_idempotency(key: str, review_id: str) -> None:
    """评分幂等：X-Idempotency-Key 去重（24h）。重复使用 → 422。"""
    r = await _idem_redis()
    ok = await r.set(f"idem:score:{key}", review_id, ex=86400, nx=True)
    if not ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="重复请求：该 Idempotency-Key 已使用过",
        )


# ==================== SSE 断流续推（P3.6） ====================

# SSE 帧缓存 TTL（断流重连窗口，超过后前端全量重拉）
_SSE_CACHE_TTL = 300


async def _cache_sse_frame(review_id: str, frame: str) -> None:
    """缓存已发 SSE 帧（Redis list，断流续推用）。"""
    r = await _idem_redis()
    key = f"sse:cache:{review_id}"
    await r.rpush(key, frame)
    await r.expire(key, _SSE_CACHE_TTL)


async def _load_sse_cache(review_id: str) -> list[str] | None:
    """读取已缓存 SSE 帧列表（无缓存返回 None）。"""
    r = await _idem_redis()
    frames = await r.lrange(f"sse:cache:{review_id}", 0, -1)
    return frames if frames else None


def _parse_sse_seq(frame: str) -> int:
    """从 SSE 帧首行 `id: N` 解析序号。"""
    m = re.match(r"^id: (\d+)", frame)
    return int(m.group(1)) if m else 0


async def _resolve_expert(session: AsyncSession, user: User) -> str:
    """评审专家归属：登录账号 display_name 反查专家实体 → expert_id。

    P1.4 导入专家时自动建登录账号（display_name=专家名），expert 表无 user_id
    列，通过 name 关联（重名时取第一个并告警）。
    """
    experts = (
        await session.scalars(select(Expert).where(Expert.name == user.display_name))
    ).all()
    if not experts:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="当前账号未关联评审专家档案")
    if len(experts) > 1:
        logger.warning("review.expert_name_dup", name=user.display_name, count=len(experts))
    return experts[0].expert_id


def _service_to_http(exc: Exception) -> HTTPException:
    mapping = {
        svc.ReviewNotFoundError: status.HTTP_404_NOT_FOUND,
        svc.BidNotFrozenError: status.HTTP_400_BAD_REQUEST,
        svc.DimensionMismatchError: status.HTTP_400_BAD_REQUEST,
        svc.ReviewAccessDeniedError: status.HTTP_403_FORBIDDEN,
        svc.ReviewLockedError: status.HTTP_400_BAD_REQUEST,
    }
    return HTTPException(mapping.get(type(exc), status.HTTP_422_UNPROCESSABLE_ENTITY), detail=str(exc))


@router.get("/reviews", summary="评审历史（本人已提交评审，分页）")
async def my_reviews(
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT)),
) -> dict:
    expert_id = await _resolve_expert(session, user)
    items, total = await svc.list_my_reviews(
        session, expert_id=expert_id, page=page, page_size=page_size
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/lots/{lot_id}/reviews", summary="评审进度一览（标书×维度矩阵）")
async def lot_review_progress(
    lot_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.PROJECT_MANAGER, Role.ADMIN, Role.REVIEW_EXPERT)),
) -> dict:
    logger.debug("review.progress_request", operator=user.user_id, lot_id=lot_id)
    try:
        return await svc.get_lot_review_progress(session, lot_id=lot_id)
    except svc.LotNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post(
    "/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建评审工作台",
)
async def create_review(
    body: ReviewCreate,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT, Role.ADMIN)),
) -> ReviewOut:
    logger.debug("review.create_request", operator=user.user_id, bid_id=body.bid_id, dimension_id=body.dimension_id)
    expert_id = await _resolve_expert(session, user)
    try:
        review = await svc.create_review(
            session, expert_id=expert_id, bid_id=body.bid_id, dimension_id=body.dimension_id
        )
    except (svc.ReviewNotFoundError, svc.BidNotFrozenError, svc.DimensionMismatchError) as e:
        raise _service_to_http(e)
    return ReviewOut.model_validate(review)


@router.get("/reviews/ai-status", summary="AI 可用状态探测（降级 UI，P6.6）")
async def ai_status(
    user: User = Depends(require_roles(Role.REVIEW_EXPERT, Role.ADMIN)),
) -> dict:
    """断路器 OPEN 或 DEEPSEEK_ENABLED=false → unavailable，前端切换纯人工评审。

    半开（HALF_OPEN）视为可用：放行一次探测，失败自然回落 OPEN 由前端 503 兜底。
    """
    circuit = get_client().circuit_state
    available = settings.deepseek_enabled and circuit != "OPEN"
    return {
        "status": "available" if available else "unavailable",
        "circuit": circuit,
        "enabled": settings.deepseek_enabled,
    }


@router.post("/reviews/{review_id}/score", summary="SSE 流式评分（报价走公式）")
async def stream_score(
    review_id: str,
    x_idempotency_key: Optional[str] = Header(None),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT, Role.ADMIN)),
) -> StreamingResponse:
    if x_idempotency_key:
        await _check_idempotency(x_idempotency_key, review_id)
    # 断路器 OPEN：AI 不可用直接 503（前端切纯人工评审，P3.6 降级）
    if get_client().circuit_state == "OPEN":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 推理引擎暂不可用（断路器 OPEN），请切换人工评审",
        )
    expert_id = await _resolve_expert(session, user)

    async def gen():
        # 断流续推：Last-Event-ID → 从缓存补发 seq 之后的帧；缓存过期 → event:reset
        if last_event_id is not None:
            cached = await _load_sse_cache(review_id)
            if not cached:
                yield sse_event("reset", {"review_id": review_id}, 0)
                return
            last = int(last_event_id)
            for fr in cached:
                if _parse_sse_seq(fr) > last:
                    yield fr
            return
        try:
            async with session_factory() as s:
                async for frame in svc.stream_score(
                    s, review_id=review_id, expert_id=expert_id
                ):
                    await _cache_sse_frame(review_id, frame)
                    yield frame
        except Exception as e:  # noqa: BLE001  流中断不吞，记录并结束
            logger.warning("review.stream_error", review_id=review_id, error=str(e))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/reviews/{review_id}/chat", summary="SSE 流式对话（多轮追问）")
async def stream_chat(
    review_id: str,
    body: ChatRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT, Role.ADMIN)),
) -> StreamingResponse:
    expert_id = await _resolve_expert(session, user)

    async def gen():
        async with session_factory() as s:
            review = await s.get(svc.ExpertReview, review_id)
            if review is None or review.expert_id != expert_id:
                yield sse_event("error", {"detail": "评审记录不存在或非本人任务"}, 0)
                return
            # 检索标书证据注入：chat 首问无历史上下文，缺依据 LLM 会编造通用方案
            # （评测 run 165 根因）；dimension=None 走纯向量召回，避免维度关键词偏置。
            bid = await s.get(BidDocument, review.bid_id)
            chunks: list[str] = []
            if bid is not None:
                results, _hint = await retrieve_with_meta(
                    body.question, lot_id=bid.lot_id, bid_id=bid.bid_id,
                    dimension=None, top_k=8,
                )
                chunks = [r.content for r in results if r.source in ("vector", "keyword")]
            await conversation.add_message(
                s, review_id=review_id, dimension_id=review.dimension_id,
                role="user", content=body.question,
            )
            context = await conversation.get_context(
                s, review_id=review_id, dimension_id=review.dimension_id
            )
            prompt = build_chat_prompt(
                role_context="你是标书评审专家助手，结合标书内容与当前评审上下文回答专家的追问。",
                context=context, history=[], question=body.question, chunks=chunks,
            )
            seq = 1
            # 契约 meta 首帧（评测 §5.1）
            yield sse_event("meta", {
                "agent": "smart-procurement",
                "model": settings.deepseek_model,
                "interface": "POST /reviews/{review_id}/chat",
                "contract_version": "1.0",
                "git_sha": "",
                "knowledge_version": "",
            }, seq := seq + 1)
            yield sse_event("thinking", {"stage": "CHAT"}, seq := seq + 1)
            full = ""
            # 7.4 cache 字段：DeepSeek 响应带 prompt_cache_hit/miss_tokens，累加透传评测平台按命中价计成本
            total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
            try:
                # 对话回答即答案正文：契约 reasoning/answer 同 delta 双发，thought 保留旧前端
                async for delta, u in get_client().chat_stream(prompt, max_tokens=1024):
                    if u:
                        for _k in total_usage:
                            total_usage[_k] += u.get(_k, 0) or 0
                    if not delta:
                        continue
                    full += delta
                    yield sse_event("reasoning", {"delta": delta}, seq := seq + 1)
                    yield sse_event("answer", {"delta": delta}, seq := seq + 1)
                    yield sse_event("thought", {"delta": delta}, seq := seq + 1)
            finally:
                if full.strip():
                    await conversation.add_message(
                        s, review_id=review_id, dimension_id=review.dimension_id,
                        role="assistant", content=full,
                    )
                    await conversation.maybe_summarize(
                        s, review_id=review_id, dimension_id=review.dimension_id
                    )
            yield sse_event("usage", total_usage, seq := seq + 1)
            yield sse_event("done", {"content": full}, seq := seq + 1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.put("/reviews/{review_id}/score", response_model=ReviewOut, summary="暂存维度评分（DRAFT）")
async def save_score(
    review_id: str,
    body: SaveScoreRequest,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT, Role.ADMIN)),
) -> ReviewOut:
    logger.debug("review.save_request", operator=user.user_id, review_id=review_id, score=body.score)
    expert_id = await _resolve_expert(session, user)
    try:
        review = await svc.save_score(
            session, review_id=review_id, expert_id=expert_id,
            score=body.score, comment=body.comment, ai_suggestion=body.ai_suggestion,
        )
    except (svc.ReviewNotFoundError, svc.ReviewAccessDeniedError, svc.ReviewLockedError) as e:
        raise _service_to_http(e)
    return ReviewOut.model_validate(review)


@router.post("/reviews/{review_id}/submit", response_model=ReviewOut, summary="提交评审并锁定")
async def submit_review(
    review_id: str,
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_roles(Role.REVIEW_EXPERT, Role.ADMIN)),
) -> ReviewOut:
    logger.debug("review.submit_request", operator=user.user_id, review_id=review_id)
    expert_id = await _resolve_expert(session, user)
    try:
        review = await svc.submit_review(session, review_id=review_id, expert_id=expert_id)
    except (svc.ReviewNotFoundError, svc.ReviewAccessDeniedError) as e:
        raise _service_to_http(e)
    return ReviewOut.model_validate(review)
