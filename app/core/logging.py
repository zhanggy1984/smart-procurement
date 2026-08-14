"""structlog JSON 日志配置（P1.2 落地，solution.md 日志规范）。

规范要点（solution.md「日志与可观测性」）：
- 单行 JSON，含时间戳与日志级别
- 脱敏规则集中在 core/crypto.redact()，本模块不重复实现
- X-Request-ID 链路上下文由 P3.6 中间件注入 structlog.contextvars

用法：业务代码 `logger = structlog.get_logger(__name__)`，
接口层打 debug 级入参出参。
"""

from __future__ import annotations

import logging

import structlog


def setup_logging(level: str = "INFO") -> None:
    """初始化 structlog（应用启动时调用一次）。"""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric_level, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            # 单行 JSON；ensure_ascii=False 保留中文便于阅读
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
