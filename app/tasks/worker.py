"""arq 后台 Worker 配置与任务注册。

P1.6 落地 outbox 消费：
- consume_outbox：每 5s 目标→arq cron 最小粒度是分钟，实际每分钟 + 启动即跑
  一次（run_at_startup）；验收/紧急触发走 `enqueue_job("consume_outbox")`。
  消费逻辑见 services/outbox_consumer.py（FOR UPDATE SKIP LOCKED → Neo4j 幂等同步）。
- reconcile_outbox：每小时扫描 FAILED 事件重放（MERGE 语义幂等）。

P2.1 补充标书解析：
- document_ingest：由上传/retry-parse 经 dispatch.enqueue_document_ingest 触发
  （非 cron，job 内自管重试），7 步 checkpoint 见 tasks/document_ingest.py。
- scan_zombie_parsing：每分钟扫描 PARSING 悬挂（parsing_step>0 且超时）→ PARSE_FAILED。
"""

from __future__ import annotations

import structlog
from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.services import outbox_consumer
from app.tasks import archive, document_ingest

logger = structlog.get_logger(__name__)


async def health_check(ctx: dict) -> str:
    """占位任务：验证 worker 心跳。"""
    return "ok"


async def consume_outbox(ctx: dict) -> int:
    """消费一批 PENDING outbox 事件（同步 Neo4j）。"""
    processed = await outbox_consumer.consume_pending_once()
    logger.info("outbox.consume_end", processed=processed)
    return processed


async def reconcile_outbox(ctx: dict) -> int:
    """Reconciliation：重放 FAILED outbox 事件。"""
    replayed = await outbox_consumer.reconcile_failed()
    logger.info("outbox.reconcile_end", replayed=replayed)
    return replayed


async def startup(ctx: dict) -> None:
    """worker 启动钩子。"""


async def shutdown(ctx: dict) -> None:
    """worker 关闭钩子。"""


class WorkerSettings:
    """arq Worker 配置。函数名即任务名（enqueue_job 用），cron_jobs 定时触发。"""

    functions = [
        health_check,
        consume_outbox,
        reconcile_outbox,
        document_ingest.document_ingest,
        document_ingest.scan_zombie_parsing,
        archive.archive_project,
    ]

    # 队列轮询间隔：默认 0.5s，收小加速 enqueue 任务 pickup（本地/验收友好）
    poll_delay = 0.2

    cron_jobs = [
        # arq cron 最小粒度分钟：minute=None 即每分钟，启动即消费一次；
        # 验收/紧急触发用 enqueue_job("consume_outbox") 即时执行。
        cron(consume_outbox, run_at_startup=True),
        # Reconciliation 每小时扫描 FAILED 重放（task.md P1.6）
        cron(reconcile_outbox, minute=0, run_at_startup=True),
        # 僵尸解析扫描：每分钟，启动即扫一次（task.md P2.1）
        cron(document_ingest.scan_zombie_parsing, run_at_startup=True),
    ]

    on_startup = startup
    on_shutdown = shutdown

    # Redis 连接配置从环境变量 / .env 加载
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
