"""P6.2 系统配置服务单元测试。

覆盖：get_sync 默认值兜底、set_configs 更新内存+DB、非法 key/value 拒绝、
load_all 全量加载覆盖缓存。不连真实 DB（session 用 AsyncMock）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services import config_service
from app.services.config_service import ConfigError, get_sync


@pytest.fixture(autouse=True)
def _reset_cache():
    """清理模块级内存缓存，避免用例间污染。"""
    saved = dict(config_service._cache)
    saved_full_load = config_service._last_full_load
    config_service._cache.clear()
    config_service._last_full_load = 0.0
    yield
    config_service._cache.clear()
    config_service._cache.update(saved)
    config_service._last_full_load = saved_full_load


def test_get_sync_returns_default_when_not_loaded():
    """缓存未加载时回退默认值（与 config.py 孤儿字段解耦后的单一默认源）。"""
    assert get_sync("fraud.critical_threshold") == "75"
    assert get_sync("llm.temperature") == "0.3"
    assert get_sync("fraud.similar_pair_threshold") == "7"


def test_validate_rejects_unknown_key():
    with pytest.raises(ConfigError):
        config_service._validate("not.a.key", 1)


def test_validate_rejects_out_of_range():
    with pytest.raises(ConfigError):
        config_service._validate("fraud.weight_text", 1.5)
    with pytest.raises(ConfigError):
        config_service._validate("llm.temperature", 3)


def test_validate_rejects_non_integer_for_int_key():
    with pytest.raises(ConfigError):
        config_service._validate("fraud.similar_pair_threshold", 3.5)


async def test_set_configs_updates_cache_and_commits_db():
    session = AsyncMock()
    items = [{"key": "fraud.critical_threshold", "value": 80}]
    result = await config_service.set_configs(session, items, operator_id="ADMIN-1")

    # 内存缓存即时生效（写时更新）
    assert get_sync("fraud.critical_threshold") == "80"
    # DB UPSERT 执行 + 提交
    assert session.execute.await_count == 1
    session.commit.assert_awaited_once()
    # 返回值含全部 11 项
    assert len(result) == 11
    item = next(i for i in result if i["key"] == "fraud.critical_threshold")
    assert item["value"] == "80"
    assert item["min"] == 1 and item["max"] == 100


async def test_set_configs_rejects_invalid_before_db_write():
    session = AsyncMock()
    with pytest.raises(ConfigError):
        await config_service.set_configs(
            session,
            [{"key": "fraud.weight_price", "value": 99}],
            operator_id="ADMIN-1",
        )
    # 校验失败不应落库/提交
    session.commit.assert_not_awaited()
    assert get_sync("fraud.weight_price") == "0.25"


async def test_load_all_fills_cache_from_db():
    from app.models.system_config import SystemConfig

    r1 = SystemConfig(config_key="fraud.critical_threshold", config_value="88")
    r2 = SystemConfig(config_key="llm.temperature", config_value="0.5")
    # scalars() 为 async，await 后返回同步 ScalarResult（.all() 普通方法）
    result = Mock()
    result.all.return_value = [r1, r2]
    mock_session = AsyncMock()
    mock_session.scalars.return_value = result

    with patch("app.services.config_service.session_factory") as mf:
        mf.return_value.__aenter__.return_value = mock_session
        await config_service.load_all()

    assert get_sync("fraud.critical_threshold") == "88"
    assert get_sync("llm.temperature") == "0.5"
    # 未落库的键仍回退默认
    assert get_sync("fraud.weight_text") == "0.40"
