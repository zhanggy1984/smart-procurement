"""structlog JSON 日志配置（P1.2 落地，solution.md 日志规范）。

规范要点（solution.md「日志与可观测性」）：
- 单行 JSON，含时间戳与日志级别
- 脱敏规则集中在 core/crypto.redact()，本模块不重复实现
- X-Request-ID 链路上下文由 P3.6 中间件注入 structlog.contextvars

用法：业务代码 `logger = structlog.get_logger(__name__)`，
接口层打 debug 级入参出参。

观测（§11.3 sp 接入）：sp 用 PrintLoggerFactory 直出 stdout，不经 stdlib logging
→ obs_sdk 的 stdlib Handler 收不到。SDK 提供 structlog processor（结构上旁路转发
一份事件到观测通道，不改原 event_dict），启用且包存在时在 JSONRenderer 前插链；
未启用/未装包时链不带观测项，日志行为与既有完全一致（零侵入）。
"""

from __future__ import annotations

import logging

import structlog

from app.core.config import settings


def setup_logging(level: str = "INFO") -> None:
    """初始化 structlog（应用启动时调用一次）。"""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric_level, format="%(message)s")

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    # 观测 structlog 转发器（§11.3）：须在 JSONRenderer 前；观测关闭/包缺失 = 链不带观测项
    if settings.obs_ready:
        try:
            from obs_sdk import structlog_processor

            processors.append(structlog_processor)
        except ImportError:
            logging.getLogger("app.core.logging").warning(
                "[obs] obs_sdk 未安装，观测 structlog 转发未插链（OBS_ENABLED=true 但包缺失）"
            )
    # 单行 JSON；ensure_ascii=False 保留中文便于阅读
    processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
