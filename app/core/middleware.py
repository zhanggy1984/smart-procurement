"""HTTP 中间件（P3.6）。

- RequestIDMiddleware：X-Request-ID 全链路追踪。请求无该头时生成并绑定到
  structlog contextvars（后续所有日志自动携带 request_id），响应头回传。
  下游调用（DeepSeek/MinIO/Neo4j）如需透传，从 contextvars 取 request_id 注入。

说明：UUID7 在 Python 3.11 无内置，用 uuid4 hex 作 request_id（32 位，
跨服务唯一即可；如需时间序可后续换 uuid7 实现）。
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings

logger = structlog.get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求分配/透传 X-Request-ID，并绑定 structlog contextvars。"""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = rid
        if settings.debug:
            logger.debug("http.request", method=request.method, path=request.url.path)
        return response
