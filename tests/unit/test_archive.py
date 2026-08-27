"""P8 异常兜底：归档 job 的 Neo4j 写路径降级。

覆盖 _merge_bid_together：Neo4j 挂 → 跳过失败对、job 继续；部分失败 → 成功对仍计数。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import neo4j_sync
from app.tasks import archive


class _Ctx:
    """session_factory 的 fake async context manager。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_merge_bid_together_continues_on_neo4j_failure(monkeypatch):
    """Neo4j 挂 → _merge_bid_together 跳过失败对、job 继续（返回成功计数 0，不抛）。"""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [("LOT-1", "S1"), ("LOT-1", "S2"), ("LOT-1", "S3")]
    session.execute.return_value = result
    monkeypatch.setattr(archive, "session_factory", lambda: _Ctx(session))

    async def _boom(a, b):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(neo4j_sync, "upsert_bid_together", _boom)

    count = await archive._merge_bid_together("PROJ-1")
    assert count == 0  # 全部失败对不计入成功


@pytest.mark.asyncio
async def test_merge_bid_together_partial_success(monkeypatch):
    """部分对失败 → 返回成功计数（降级不丢已成功对）。"""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [("LOT-1", "S1"), ("LOT-1", "S2"), ("LOT-1", "S3")]
    session.execute.return_value = result
    monkeypatch.setattr(archive, "session_factory", lambda: _Ctx(session))

    async def _flaky(a, b):
        if a == "S1" and b == "S2":
            raise RuntimeError("neo4j down")
        return None

    monkeypatch.setattr(neo4j_sync, "upsert_bid_together", _flaky)

    count = await archive._merge_bid_together("PROJ-1")
    assert count == 2  # (S1,S3)、(S2,S3) 成功
