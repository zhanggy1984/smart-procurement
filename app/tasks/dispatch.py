"""标书解析 job 投递（P2.1）。

上传 / retry-parse 成功后向 arq 队列投递 `document_ingest`。Redis pool 进程内
缓存单例，避免每次请求重建连接（FastAPI/worker 进程各自持有）。

投递失败不抛出：上传已落库成功，解析可由 PM 手动 retry-parse 或僵尸扫描
兜底，不因投递失败回滚上传结果——fire-and-forget 语义。
"""

from __future__ import annotations

import structlog
from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings

logger = structlog.get_logger(__name__)

_pool = None  # arq RedisPool 单例


async def _get_pool():
    """延迟创建 arq 连接池。"""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def enqueue_document_ingest(bid_id: str) -> bool:
    """投递标书解析 job。返回是否投递成功（失败仅记日志，不抛出）。"""
    try:
        pool = await _get_pool()
        await pool.enqueue_job("document_ingest", bid_id)
        logger.info("bid.parse_enqueued", bid_id=bid_id)
        return True
    except Exception as e:  # noqa: BLE001  Redis 不可达等，不影响上传主链路
        logger.warning("bid.parse_enqueue_failed", bid_id=bid_id, error=str(e))
        return False


async def enqueue_archive(project_id: str) -> bool:
    """投递项目归档 job（P3.5 定标后触发）。失败仅记日志，不影响定标主链路。"""
    try:
        pool = await _get_pool()
        await pool.enqueue_job("archive_project", project_id)
        logger.info("project.archive_enqueued", project_id=project_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("project.archive_enqueue_failed", project_id=project_id, error=str(e))
        return False
