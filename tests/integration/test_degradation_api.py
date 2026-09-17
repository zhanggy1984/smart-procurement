"""P7.3 降级路径 API 集成测试（task.md 7 场景中 API 级可自动化的 4 项）。

- DeepSeek 断路器 OPEN → 503"AI 推理引擎暂不可用"（score 已测，此处 ai-status）
- ai-status：deepseek_enabled=false / 断路器 OPEN → unavailable（前端切纯人工）
- Milvus 不可用 → 标书正文降级为空 chunks（不阻断）
- （Neo4j/MySQL/BGE 超时、chunk IP<0.5 为 service 级；断路器状态机由单元测试
  test_degradation.py / test_deepseek_client.py 覆盖）
- **断路器到期自愈必须经过路由**（2026-09-17 补）：单测直接调 `acquire()`、
  旧集成用例用写死 `circuit_state` 的 MagicMock —— 两者都**绕过了路由那道
  `if circuit_state == "OPEN": 503` 的门**，而那正是让自愈失效的地方。
  见 `test_circuit_self_heals_after_window_through_route`。
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
async def test_circuit_self_heals_after_window_through_route(client, exp_headers):
    """**到期自愈必须经过路由**（2026-09-17 修复的回归用例）。

    上面两条用的是 `circuit_state` 写死的 MagicMock —— **它没有任何迁移行为**，
    所以旧用例无论断路器是否真的会自愈都照样绿。本条改用**真断路器**，只把它包一层
    转发 `circuit_state`，走真实路由读 `circuit_state`：到期前 OPEN（unavailable），
    窗口过后**同一个对象**必须自己翻成 HALF_OPEN（available）。
    修前此条必红（`state` 是纯读 ⇒ 恒 OPEN）；修后绿。
    """
    import asyncio

    from app.ai.llm.deepseek_client import _CircuitBreaker

    cb = _CircuitBreaker(threshold=1, open_seconds=0.05)
    await cb.record_failure()  # 阈值 1 ⇒ 立即 OPEN
    assert cb.state == "OPEN"

    class _Client:
        @property
        def circuit_state(self) -> str:
            return cb.state  # 走真属性，不写死

    with patch("app.api.v1.reviews.get_client", return_value=_Client()):
        before = await client.get("/api/v1/reviews/ai-status", headers=exp_headers)
        await asyncio.sleep(0.06)  # 越过 open_seconds
        after = await client.get("/api/v1/reviews/ai-status", headers=exp_headers)

    assert before.json()["circuit"] == "OPEN"
    assert before.json()["status"] == "unavailable"
    assert after.json()["circuit"] == "HALF_OPEN", "窗口过后路由读到的面必须自行迁移"
    assert after.json()["status"] == "available", "自愈未生效 ⇒ reviews 链路会被 503 闩死"


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


@pytest.mark.asyncio
async def test_bid_content_page_range_transparent(client, admin_headers, sup_headers, lot_factory):
    """get_bid_content 正常路径：chunks 透出 page_range（Milvus VARCHAR → list[int]）。"""
    lot = await lot_factory()
    up = await client.post(f"/api/v1/lots/{lot['lot_id']}/bids", headers=sup_headers,
                           files={"file": ("b.pdf", b"%PDF-1.4\n%%itest", "application/pdf")})
    assert up.status_code == 201
    bid_id = up.json()["bid_id"]

    fake = MagicMock()
    fake.query.return_value = [
        {"chunk_id": f"{bid_id}-0000", "content": "第一章内容", "chapter_title": "第一章",
         "chunk_index": 0, "page_range": "2-3"},
    ]
    with patch("app.core.milvus.get_collection", return_value=fake):
        resp = await client.get(f"/api/v1/bids/{bid_id}/content", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["chunks"][0]["page_range"] == [2, 3]
