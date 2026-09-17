"""HTTP 中间件（P3.6 + §11.3 观测接入）。

- RequestIDMiddleware：X-Request-ID 全链路追踪。请求无该头时生成并绑定到
  structlog contextvars（后续所有日志自动携带 request_id），响应头回传。
  下游调用（DeepSeek/MinIO/Neo4j）如需透传，从 contextvars 取 request_id 注入。
- 观测 request 出入口（§11.3 sp #2）并入同一中间件：rid 与观测 trace_id 同源
  （日志 request_id == 观测 trace_id），SSE 在 body 迭代完成后收口。

说明：UUID7 在 Python 3.11 无内置，用 uuid4 hex 作 request_id（32 位，
跨服务唯一即可；如需时间序可后续换 uuid7 实现）。
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.obs import (
    OBS_EXEMPT_PATHS,
    begin_request as obs_begin,
    end_request as obs_end,
    llm_health_var,
)

logger = structlog.get_logger(__name__)


def _obs_finish(response, aborted: bool = False, health: dict | None = None,
                obs_input: dict | None = None) -> None:
    """request 出口统一收口：断连 > LLM 硬失败 > HTTP 状态码 > ok（仿 cs _obs_end）。

    SSE 客户端中途断开（body 迭代抛 BaseException）→ CLIENT_DISCONNECT；非 2xx →
    HTTP_{code}；其余 ok。end_request 自判 status 合法性/补 duration，此处不重复。

    **LLM 硬失败插在状态码之前**：SSE 恒返 200，LLM 异常被业务吞成 error 帧后状态码判不出，
    只能靠业务侧置的 health 标记；不这么记则平台按「故障已被业务吸收」切掉回流候选。

    health 由 dispatch 以**形参/闭包**传入，不在此处 `llm_health_var.get()` 重取——body 迭代
    发生在 dispatch 返回之后，重取拿不到请求上下文（gq 侧同一理由）。
    """
    if aborted:
        obs_end("error", error_type="CLIENT_DISCONNECT", error_msg="客户端连接中断",
                obs_input=obs_input)
        return
    if health and health["hard_fail"]:
        obs_end("error", error_type=health["error_type"],
                error_msg="LLM 调用失败，用户本轮未拿到正常回答", obs_input=obs_input)
        return
    code = response.status_code
    if code >= 400:
        obs_end("error", error_type=f"HTTP_{code}", obs_input=obs_input)
        return
    obs_end("ok", obs_input=obs_input)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求分配/透传 X-Request-ID，并绑定 structlog contextvars；观测出入口同源。"""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=rid)
        # 请求级 LLM 健康标记（在 obs_begin 之前置入，下游业务才取得到）。
        # 豁免路径同样置入：否则业务侧置位会打「上下文缺失」ERROR，噪声盖住真信号。
        health: dict = {"hard_fail": False, "error_type": None}
        llm_health_var.set(health)
        response = None
        try:
            # 豁免路径（健康探针/登录面）：不建观测 span，其余行为不变
            exempt = request.url.path in OBS_EXEMPT_PATHS
            if not exempt:
                # trace_id 沿用本请求 rid：观测 trace_id 与日志 request_id 同源
                obs_begin(method=request.method, path=request.url.path, trace_id=rid)
            try:
                response = await call_next(request)
            except Exception:
                if not exempt:
                    obs_end("error", error_type="UNHANDLED_EXCEPTION",
                            error_msg="请求处理抛异常未达响应")
                raise
            # 入参现场由路由在请求处理期写入 request.state（与路由同 scope）。取在此处：
            # call_next 返回时下游已执行完，而 SSE 路由是在返回 StreamingResponse 前写入的
            # （gen 惰性，写在其内则取不到），故此刻必已就绪。
            obs_input = getattr(request.state, "obs_input", None)
            if not exempt:
                # 流式响应：body 迭代完成后才收口——SSE 的 LLM 调用发生在 response body
                # 发送期（call_next 返回时尚未开始），若即时 end 则 request duration≈0 且
                # 锚点(seq=0)晚于 llm_call 子节点。包一层透传迭代器，真实流式不缓冲。
                body_iter = getattr(response, "body_iterator", None)
                if body_iter is None:
                    # 非流式响应体已整体生成：直接收口（status_code 即可判定）
                    _obs_finish(response, health=health, obs_input=obs_input)
                else:
                    async def _body_with_obs():
                        try:
                            async for chunk in body_iter:
                                yield chunk
                        except BaseException:
                            # 客户端断连（CancelledError/httpx 断开等）：trace 如实反映未拿完整响应
                            _obs_finish(response, aborted=True, health=health,
                                        obs_input=obs_input)
                            raise
                        else:
                            _obs_finish(response, health=health, obs_input=obs_input)

                    response.body_iterator = _body_with_obs()
        finally:
            # 观测 span 状态由 obs_sdk 独立 contextvar 承载（不随 structlog.contextvars 清除），
            # _body_with_obs 在 dispatch 返回后的 body 迭代期仍能拿到并正常收口。
            structlog.contextvars.clear_contextvars()
        # 成功路径唯一出口（处理异常已在上方 raise 传播）：回传 rid + debug 日志
        response.headers["X-Request-ID"] = rid
        if settings.debug:
            logger.debug("http.request", method=request.method, path=request.url.path)
        return response