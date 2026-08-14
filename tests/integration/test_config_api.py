"""P6.2 系统配置 API 集成测试。

覆盖：GET /config 权限与返回结构、PUT /config 落库+即时生效、非法 value / 未知 key 422。
"""

from __future__ import annotations

import pytest

from app.core.database import session_factory
from app.models.system_config import SystemConfig
from app.services import config_service


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """清理内存缓存，避免跨用例残留（DB 侧由 conftest _reset_state TRUNCATE）。"""
    config_service._cache.clear()
    config_service._last_full_load = 0.0
    yield
    config_service._cache.clear()
    config_service._last_full_load = 0.0


async def test_list_config_admin(client, admin_headers):
    resp = await client.get("/api/v1/config", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 11
    keys = {i["key"] for i in items}
    assert "fraud.critical_threshold" in keys
    assert "conflict.employment_years" in keys  # 孤儿项也要返回（页面标注未接入）
    item = next(i for i in items if i["key"] == "fraud.critical_threshold")
    assert item["default_value"] == "75"
    assert item["wired"] is True


async def test_list_config_forbidden_for_pm(client, pm_headers):
    """仅 ADMIN 可读配置。"""
    resp = await client.get("/api/v1/config", headers=pm_headers)
    assert resp.status_code == 403


async def test_update_config_persists_and_effective(client, admin_headers):
    body = {"items": [{"key": "fraud.critical_threshold", "value": 80}]}
    resp = await client.put("/api/v1/config", headers=admin_headers, json=body)
    assert resp.status_code == 200

    # 落库
    async with session_factory() as s:
        row = await s.get(SystemConfig, "fraud.critical_threshold")
    assert row is not None and row.config_value == "80"

    # 内存缓存即时生效（业务侧 get_sync 零 DB 查询）
    assert config_service.get_sync("fraud.critical_threshold") == "80"

    # GET 反映新值
    resp = await client.get("/api/v1/config", headers=admin_headers)
    item = next(i for i in resp.json()["items"] if i["key"] == "fraud.critical_threshold")
    assert item["value"] == "80"


async def test_update_config_rejects_out_of_range(client, admin_headers):
    resp = await client.put(
        "/api/v1/config",
        headers=admin_headers,
        json={"items": [{"key": "fraud.weight_text", "value": 1.5}]},
    )
    assert resp.status_code == 422


async def test_update_config_rejects_unknown_key(client, admin_headers):
    resp = await client.put(
        "/api/v1/config",
        headers=admin_headers,
        json={"items": [{"key": "foo.bar", "value": 1}]},
    )
    assert resp.status_code == 422
