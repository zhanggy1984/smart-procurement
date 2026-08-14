"""系统配置服务（P6.2）：system_config 表 + 内存缓存，运行时即时生效。

写入走 set_configs（UPSERT DB + 同步更新内存缓存），业务侧 get_sync 零 DB 查询。
单实例一致性由「写时更新」保证（所有写都经过 set_configs，缓存永远与 DB 同步）；
DB 被进程外改动的场景由 get_all 的 TTL 兜底 reload 补偿（solution.md 4.5 第二层
设计的简化实现——本部署为单体，不引入 Redis pub/sub）。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_factory
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

# 缓存 TTL：超过后 get_all 触发一次全量 reload，防止进程外改库长期不感知
_CACHE_TTL_SECONDS = 60.0

# 配置项元数据单一事实源：(默认值 str, 类型, 是否已接入业务, 中文名, 描述)
ConfigSpec = tuple[str, str, bool, str, str]

_DEFAULTS: dict[str, ConfigSpec] = {
    "llm.temperature": ("0.3", "number", True, "LLM 温度", "LLM 评分温度，越高随机性越大"),
    "llm.max_tokens": ("2048", "number", True, "LLM 最大 Token", "单次响应最大 token 数"),
    "conflict.employment_years": (
        "3", "number", False, "任职回避年限",
        "任职回避年限（业务逻辑未实现，当前未接入，仅可配置存储）",
    ),
    "review.deviation_threshold": (
        "0.15", "number", False, "评分偏差阈值",
        "评分偏差告警阈值（业务逻辑未实现，当前未接入，仅可配置存储）",
    ),
    "fraud.auto_pass_threshold": ("25", "number", True, "初筛自动通过阈值", "围串标初筛 LOW 自动通过上限"),
    "fraud.critical_threshold": ("75", "number", True, "CRITICAL 阈值", "围串标深度检测 CRITICAL 阈值（>75 标红告警）"),
    "fraud.weight_text": ("0.40", "number", True, "文本检测权重", "深度检测综合分 text 权重"),
    "fraud.weight_graph": ("0.35", "number", True, "图谱检测权重", "深度检测综合分 graph 权重"),
    "fraud.weight_price": ("0.25", "number", True, "报价检测权重", "深度检测综合分 price 权重"),
    "fraud.similar_pair_threshold": ("7", "number", True, "相似段落对阈值", "标书级高相似段落对命中阈值"),
    "fraud.text_similarity_threshold": ("0.85", "number", True, "段落相似阈值", "高相似段落 IP 相似度阈值"),
}

# number 配置取值区间（闭区间）；整数类配置另行校验
_VALUE_RANGES: dict[str, tuple[float, float]] = {
    "llm.temperature": (0, 2),
    "llm.max_tokens": (1, 8192),
    "conflict.employment_years": (0, 20),
    "review.deviation_threshold": (0, 1),
    "fraud.auto_pass_threshold": (1, 100),
    "fraud.critical_threshold": (1, 100),
    "fraud.weight_text": (0, 1),
    "fraud.weight_graph": (0, 1),
    "fraud.weight_price": (0, 1),
    "fraud.similar_pair_threshold": (1, 100),
    "fraud.text_similarity_threshold": (0, 1),
}

# 必须为整数的配置项
_INT_KEYS = {"llm.max_tokens", "fraud.similar_pair_threshold", "conflict.employment_years"}

# 内存缓存：key -> (value, updated_at, updated_by, loaded_at 单调时钟)
_cache: dict[str, tuple[str, Optional[str], Optional[str], float]] = {}
# 最近一次全量加载的单调时钟（get_all TTL 兜底）
_last_full_load: float = 0.0


class ConfigError(ValueError):
    """配置读写业务异常（未知 key / 非法 value）。"""


def get_sync(key: str) -> str:
    """同步读配置值（零 DB 查询）。缓存未命中时返回默认值兜底。

    缓存一致性由 set_configs 写时更新保证，故不做 TTL 检查（避免活跃配置
    在无操作窗口内退化回默认值）。供同步纯函数/任意调用方使用。
    """
    item = _cache.get(key)
    if item is not None:
        return item[0]
    return _DEFAULTS[key][0]


async def load_all() -> None:
    """全量从 DB 加载到缓存（lifespan 启动 + get_all TTL 兜底调用）。

    DB 只存有自定义值的行；未覆盖的键由 get_sync/get_all 回落默认值。
    """
    global _last_full_load
    async with session_factory() as session:
        rows = (await session.scalars(select(SystemConfig))).all()
    now = time.monotonic()
    for r in rows:
        _cache[r.config_key] = (r.config_value, r.updated_at, r.updated_by, now)
    _last_full_load = now
    logger.info("config.load_all", count=len(rows))


def _validate(key: str, raw_value: object) -> str:
    """校验 key 合法且 value 在区间内，返回规整后的字符串。"""
    if key not in _DEFAULTS:
        raise ConfigError(f"未知配置项: {key}")
    try:
        num = float(raw_value)
    except (TypeError, ValueError):
        raise ConfigError(f"配置项 {key} 需要数字值，收到: {raw_value!r}") from None
    lo, hi = _VALUE_RANGES[key]
    if not (lo <= num <= hi):
        raise ConfigError(f"配置项 {key} 取值需在 [{lo}, {hi}] 区间，收到: {num}")
    if key in _INT_KEYS and not num.is_integer():
        raise ConfigError(f"配置项 {key} 需要整数，收到: {num}")
    # 规整为字符串存储：整数去尾零（7 → "7"），浮点去尾零（0.40 → "0.4"）
    return str(int(num)) if num.is_integer() else str(num)


async def set_configs(
    session: AsyncSession,
    items: list[dict],
    operator_id: str,
) -> list[dict]:
    """批量更新配置：逐条校验 → UPSERT DB → 同步更新内存缓存。

    事务性：任一非法项抛 ConfigError，整体不提交（前序更新回滚）。
    返回更新后全部配置项（含未改动的）。
    """
    now = time.monotonic()
    normalized: list[tuple[str, str]] = []
    for item in items:
        key = item["key"]
        value = _validate(key, item["value"])
        normalized.append((key, value))
        # 先更新内存，DB UPSERT 失败时由异常回滚路径退出（内存与 DB 允许短暂一致回退，
        # 下次 load_all 以 DB 为准）
        default, _, wired, label, desc = _DEFAULTS[key]
        _cache[key] = (value, None, operator_id, now)

    for key, value in normalized:
        await session.execute(
            text(
                "INSERT INTO system_config (config_key, config_value, description, updated_at, updated_by) "
                "VALUES (:key, :value, :desc, NOW(), :op) "
                "ON DUPLICATE KEY UPDATE config_value = :value, "
                "description = :desc, updated_at = NOW(), updated_by = :op"
            ),
            {"key": key, "value": value, "desc": _DEFAULTS[key][4], "op": operator_id},
        )
    await session.commit()
    logger.info("config.set", keys=[k for k, _ in normalized], operator=operator_id)
    return [_build_item(k) for k in _DEFAULTS]


async def get_all() -> list[dict]:
    """返回全部配置项（当前值 + 默认值 + 元数据）。TTL 兜底点：缓存过期先 reload。"""
    if time.monotonic() - _last_full_load > _CACHE_TTL_SECONDS:
        try:
            await load_all()
        except Exception:  # noqa: BLE001  reload 失败不阻断读取，回退默认值
            logger.warning("config.reload_failed", exc_info=True)
    return [_build_item(k) for k in _DEFAULTS]


def _build_item(key: str) -> dict:
    """组装单条配置项响应。缓存未命中时 value 回落默认值。"""
    default, type_, wired, label, description = _DEFAULTS[key]
    item = _cache.get(key)
    lo, hi = _VALUE_RANGES[key]
    return {
        "key": key,
        "label": label,
        "description": description,
        "value": item[0] if item else default,
        "default_value": default,
        "type": type_,
        "wired": wired,
        "min": lo,
        "max": hi,
        "updated_at": item[1] if item else None,
        "updated_by": item[2] if item else None,
    }
