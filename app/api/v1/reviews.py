"""评审 API（P3.3）。

- POST /reviews            创建评审工作台（校验 bid=FROZEN，限 REVIEW_EXPERT）
- POST /reviews/{id}/score SSE 流式评分（X-Idempotency-Key 幂等，报价走 price_calc）
- POST /reviews/{id}/chat  SSE 流式对话（追加 conversation_message + 自动摘要）

SSE 帧：`id:{seq}\nevent:{event}\ndata:{json}\n\n`（core/sse.py）。
评分幂等：前端每次评分携带 X-Idempotency-Key（UUID v4），Redis nx 去重，重复 → 422。
"""

import re
import time
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.config import settings
from app.core.database import get_db_session, session_factory
from app.core.redis import get_redis, redis_warn_once
from app.models.expert import Expert
from app.models.user import Role, User
from app.schemas.review import ChatRequest, ReviewCreate, ReviewOut, SaveScoreRequest
from app.services import review_service as svc
from app.services import conversation_service as conversation
from app.ai.agent import CHAT_TOOLS, ToolContext, stream_agent
from app.ai.llm.deepseek_client import get_client
from app.ai.llm.prompts import ThinkingAnswerSplitter, build_chat_prompt
from app.core.sse import sse_event
from app.models.bid_document import BidDocument
from app.models.conversation import ConversationMessage
from app.models.project import ScoringDimension

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["reviews"])

async def _idem_redis():
    """Redis 连接池单例（幂等/断流续推/评分缓存共用 app.core.redis.get_redis）。"""
    return get_redis()


async def _check_idempotency(key: str, review_id: str) -> None:
    """评分幂等：X-Idempotency-Key 去重（24h）。重复使用 → 422。

    P8 异常兜底：Redis 不可用 → fail-open 放行（幂等是安全网非正确性保证——
    评分流无破坏性写入，真实保存走 PUT /reviews/{id}/score，重复流只多耗 LLM；
    若 503 会直接断评分主链路，违背项目降级哲学）。前端 :loading/disabled 已防双击。
    """
    try:
        r = await _idem_redis()
        ok = await r.set(f"idem:score:{key}", review_id, ex=86400, nx=True)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001  Redis 不可用 → 放行
        await redis_warn_once("review.idem_redis_down", str(e))
        return
    if not ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="重复请求：该 Idempotency-Key 已使用过",
        )


# ==================== SSE 断流续推（P3.6） ====================

# SSE 帧缓存 TTL（断流重连窗口，超过后前端全量重拉）
_SSE_CACHE_TTL = 300


async def _cache_sse_frame(review_id: str, frame: str) -> None:
    """缓存已发 SSE 帧（Redis list，断流续推用）。

    P8：Redis 挂 → 跳过缓存不阻断流（断流续推降级为不可用，前端可全量重拉）。
    """
    try:
        r = await _idem_redis()
        key = f"sse:cache:{review_id}"
        await r.rpush(key, frame)
        await r.expire(key, _SSE_CACHE_TTL)
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("review.sse_cache_down", str(e))


async def _load_sse_cache(review_id: str) -> list[str] | None:
    """读取已缓存 SSE 帧列表（无缓存返回 None）。

    P8：Redis 挂 → 返回 None（reconnect 自然走 event:reset 全量重拉路径）。
    """
    try:
        r = await _idem_redis()
        frames = await r.lrange(f"sse:cache:{review_id}", 0, -1)
        return frames if frames else None
    except Exception as e:  # noqa: BLE001
        await redis_warn_once("review.sse_cache_read_down", str(e))
        return None


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
    except (svc.ReviewNotFoundError, svc.BidNotFrozenError, svc.DimensionMismatchError,
            svc.ReviewAccessDeniedError) as e:
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
        seq = 0
        try:
            async with session_factory() as s:
                async for frame in svc.stream_score(
                    s, review_id=review_id, expert_id=expert_id
                ):
                    seq = max(seq, _parse_sse_seq(frame))
                    await _cache_sse_frame(review_id, frame)
                    yield frame
        except Exception as e:  # noqa: BLE001  已开流不吞：error 帧收尾，前端提示重试
            logger.warning("review.stream_error", review_id=review_id, error=str(e))
            yield sse_event("error", {"detail": "评分流中断，请重试"}, seq + 1)

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
    # P8 异常兜底：断路器 OPEN → 503（与评分流对齐），前端仅 503 才降级纯人工评审
    if get_client().circuit_state == "OPEN":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 推理引擎暂不可用（断路器 OPEN），请切换人工评审",
        )
    expert_id = await _resolve_expert(session, user)

    async def gen():
        seq = 1
        # 契约 meta 首帧提前到 gen 顶部：DB/检索失败时 meta 仍是首帧（评测 §5.1 meta-first）
        yield sse_event("meta", {
            "agent": "smart-procurement",
            "model": settings.deepseek_model,
            "interface": "POST /reviews/{review_id}/chat",
            "contract_version": "1.0",
            "git_sha": "",
            "knowledge_version": "",
        }, seq := seq + 1)
        full = ""
        # 7.4 cache 字段：DeepSeek 响应带 prompt_cache_hit/miss_tokens，累加透传评测平台按命中价计成本
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                       "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
        try:
            async with session_factory() as s:
                review = await s.get(svc.ExpertReview, review_id)
                if review is None or review.expert_id != expert_id:
                    yield sse_event("error", {"detail": "评审记录不存在或非本人任务"}, seq := seq + 1)
                    return
                # agent 模式（function calling）：检索由 LLM 自主决策（调 retrieve_knowledge 工具），
                # 不再固定预检索；bid/dimension 供工具执行器组装上下文（retrieve 需 lot_id/bid_id，
                # rubric 工具需 dimension）。决策轮事件静默（保三发对齐契约），作答轮统一走
                # splitter 三发，full 仅存 answer 段（思考不进对话历史，避免污染摘要）。
                bid = await s.get(BidDocument, review.bid_id)
                dimension = await s.get(ScoringDimension, review.dimension_id)
                # 回指意图归队依赖：最近 user 消息原文（倒序，此时尚未写入当前问题）。
                # _classify_intent 的 history 参数用它把"就这个/还有呢"延续到上一轮意图。
                recent_user = (
                    await s.scalars(
                        select(ConversationMessage)
                        .where(ConversationMessage.review_id == review_id,
                               ConversationMessage.dimension_id == review.dimension_id,
                               ConversationMessage.role == "user")
                        .order_by(ConversationMessage.dim_turn_number.desc())
                        .limit(3)
                    )
                ).all()
                ctx = ToolContext(session=s, review=review, bid=bid, dimension=dimension,
                                  history=[m.content for m in recent_user])
                await conversation.add_message(
                    s, review_id=review_id, dimension_id=review.dimension_id,
                    role="user", content=body.question,
                )
                context = await conversation.get_context(
                    s, review_id=review_id, dimension_id=review.dimension_id
                )
                # P7.x tools 声明：agent 决策轮 system 声明工具可用与调用约束，LLM 自主决定是否调用
                prompt = build_chat_prompt(
                    role_context="你是标书评审专家助手，结合标书内容与当前评审上下文回答专家的追问。",
                    context=context, history=[], question=body.question,
                    chunks=None, tools_declared=True,
                )
                yield sse_event("thinking", {"stage": "CHAT"}, seq := seq + 1)
                try:
                    # P7.x：agent 决策轮静默（工具决策不外发 reasoning/answer），作答轮 content
                    # 进 splitter 三发（reasoning/answer/thought 对齐）；三路兜底固定话术无
                    # <thinking> 标签 → 全文当 answer（降级契约不破）
                    async for ev in stream_agent(prompt, CHAT_TOOLS, ctx):
                        etype = ev["type"]
                        if etype == "tool_call":
                            # 契约 tool_call 事件：评测端观测 LLM 决策调用的内部工具 + 检索质量元信息
                            yield sse_event("tool_call", {
                                "id": f"tool-{time.time_ns()}",
                                "name": ev["name"],
                                "args": ev["args"],
                                "result": ev["result"],
                                "status": ev["status"],
                                "intent": ev["intent"],
                            }, seq := seq + 1)
                        elif etype == "answer":
                            total_usage = dict(ev["usage"])
                            splitter = ThinkingAnswerSplitter()
                            for kind, piece in splitter.feed(ev["text"]):
                                if not piece:
                                    continue
                                if kind == "reasoning":
                                    yield sse_event("reasoning", {"delta": piece}, seq := seq + 1)
                                else:
                                    full += piece
                                    yield sse_event("answer", {"delta": piece}, seq := seq + 1)
                                    yield sse_event("thought", {"delta": piece}, seq := seq + 1)
                            for kind, piece in splitter.flush():
                                if not piece:
                                    continue
                                if kind == "reasoning":
                                    yield sse_event("reasoning", {"delta": piece}, seq := seq + 1)
                                else:
                                    full += piece
                                    yield sse_event("answer", {"delta": piece}, seq := seq + 1)
                                    yield sse_event("thought", {"delta": piece}, seq := seq + 1)
                        elif etype == "error":
                            yield sse_event("error", {"detail": ev["message"]}, seq := seq + 1)
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
        except Exception as e:  # noqa: BLE001  已开流不吞：error 帧收尾，前端提示重试
            logger.warning("review.chat_stream_error", review_id=review_id, error=str(e))
            yield sse_event("error", {"detail": "AI 对话中断，请重试"}, seq := seq + 1)

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
