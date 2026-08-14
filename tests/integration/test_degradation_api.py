"""P7.3 降级路径 API 集成测试（task.md 7 场景中 API 级可自动化的 4 项）。

- DeepSeek 断路器 OPEN → 503"AI 推理引擎暂不可用"（score 已测，此处 ai-status）
- ai-status：deepseek_enabled=false / 断路器 OPEN → unavailable（前端切纯人工）
- Milvus 不可用 → 标书正文降级为空 chunks（不阻断）
- （Neo4j/MySQL/BGE 超时、chunk IP<0.5、断路器半开探测为 service 级/状态机，
  由单元测试 test_degradation.py / test_deepseek_client.py 覆盖）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_ai_status_unavailable_when_disabled(client, exp_headers):
    """DEEPSEEK_ENABLED=false → ai-status unavailable（评分降级纯人工）。"""
    from app.api.v1 import reviews as reviews_mod

    orig = reviews_mod.settings.deepseek_enabled
    reviews_mod.settings.deepseek_enabled = False
    try:
        resp = await client.get("/api/v1/reviews/ai-status", headers=exp_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "unavailable"
        assert resp.json()["enabled"] is False
    finally:
        reviews_mod.settings.deepseek_enabled = orig


@pytest.mark.asyncio
async def test_ai_status_unavailable_when_circuit_open(client, exp_headers):
    """断路器 OPEN → ai-status unavailable（前端切换纯人工评审）。"""
    fake = MagicMock()
    fake.circuit_state = "OPEN"
    with patch("app.api.v1.reviews.get_client", return_value=fake):
        resp = await client.get("/api/v1/reviews/ai-status", headers=exp_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"
    assert resp.json()["circuit"] == "OPEN"


@pytest.mark.asyncio
async def test_ai_status_available_when_closed(client, exp_headers):
    """正常状态 → available。"""
    fake = MagicMock()
    fake.circuit_state = "CLOSED"
    with patch("app.api.v1.reviews.get_client", return_value=fake):
        resp = await client.get("/api/v1/reviews/ai-status", headers=exp_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "available"


@pytest.mark.asyncio
async def test_bid_content_milvus_degraded_to_empty(client, admin_headers, sup_headers, lot_factory):
    """Milvus 不可用 → 标书正文 chunks 降级为空列表（200，不阻断）。"""
    lot = await lot_factory()
    up = await client.post(f"/api/v1/lots/{lot['lot_id']}/bids", headers=sup_headers,
                           files={"file": ("b.pdf", b"%PDF-1.4\n%%itest", "application/pdf")})
    assert up.status_code == 201
    bid_id = up.json()["bid_id"]
    with patch("app.core.milvus.get_collection", side_effect=RuntimeError("milvus down")):
        resp = await client.get(f"/api/v1/bids/{bid_id}/content", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["chunks"] == []
    assert resp.json()["bid_id"] == bid_id
