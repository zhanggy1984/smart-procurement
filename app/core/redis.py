"""Redis 客户端（进程内单例）与通用缓存工具（ST1 评分缓存）。

幂等/SSE 断流续推/评分语义缓存共用同一个连接池（decode_responses=True），
避免各调用点各自 from_url 造成连接浪费。Redis 故障一律 fail-open：
调用方自行决定降级路径，这里只负责把单例与限频告警暴露出去。
"""

from __future__ import annotations

import time

import structlog

from app.core.config import settings

logger = structlog.get_logger(__name__)

_redis = None
_last_redis_warn = 0.0


def get_redis():
    """进程内单例。decode_responses=True：值按 str 返回（幂等/帧缓存均存 JSON 串）。"""
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def redis_warn_once(tag: str, error: str) -> None:
    """Redis 故障限频告警（30s 内只打一次，防 SSE 每帧/每条缓存都刷屏）。

    async 签名与历史调用点（reviews.py `await _redis_warn_once(...)`）对齐，无 I/O。
    """
    global _last_redis_warn
    now = time.monotonic()
    if now - _last_redis_warn > 30:
        _last_redis_warn = now
        logger.warning(tag, error=error)


async def flush_keys(prefix: str) -> int:
    """按前缀删除 key（scan_iter 游标遍历，适配大 key 空间）。返回删除条数。

    调用方负责 try/except（Redis 挂 → 抛错由调用方决定是否阻断）；
    本函数不吞异常，保持"缓存失效失败要可见"。
    """
    r = get_redis()
    deleted = 0
    async for key in r.scan_iter(match=f"{prefix}*"):
        deleted += await r.delete(key)
    if deleted:
        logger.info("redis.flush_keys", prefix=prefix, deleted=deleted)
    return deleted
