"""P7.2 ExpertMatchService 纯函数单元测试（task.md：5 用例）。

覆盖 _score_candidates 多维加权排序、_assign_dimensions 维度轮转与 min_per_dim
补足。冲突检测（_conflicts_with_status）依赖 Neo4j，P8 降级路径在此覆盖。
"""

from __future__ import annotations

import pytest

from app.services.expert_match_service import _assign_dimensions, _score_candidates


def _cand(eid: str, tags: list[str], exp: int, region: str) -> dict:
    return {"expert_id": eid, "tags": set(tags), "experience": exp, "region": region}


def test_score_candidates_ranking():
    """加权排序：标签命中、经验、同地区加分，spec 权重最高。"""
    cands = [
        _cand("E1", ["软件开发"], 10, "华东"),   # 命中标签
        _cand("E2", ["云计算"], 30, "华北"),    # 无标签命中但经验满
    ]
    scored = _score_candidates(cands, ["软件开发"], "华东",
                               {"specialization": 0.40, "experience": 0.30,
                                "review_quality": 0.20, "region": 0.10},
                               {"E1": 0.7, "E2": 0.7})
    # E1 标签命中(0.4) + 经验(10/30=0.33×0.3=0.1) + 同地区(0.1) > E2 经验满分(0.3)+非同区(0.05)
    assert scored[0][0]["expert_id"] == "E1"


def test_score_candidates_region_bonus():
    """同地区 1.0 vs 非本地区 0.5，region 权重只占 0.10。"""
    cands = [_cand("E1", [], 30, "华东"), _cand("E2", [], 30, "华北")]
    scored = _score_candidates(cands, [], "华东",
                               {"specialization": 0.40, "experience": 0.30,
                                "review_quality": 0.20, "region": 0.10},
                               {"E1": 0.7, "E2": 0.7})
    # 除 region 外其余一致 → E1 高 0.05
    diff = scored[0][1] - scored[1][1]
    assert abs(diff - 0.05) < 1e-6


def test_assign_dimensions_round_robin():
    """维度轮转：4 专家 × 2 维度，各专家唯一维度主责。"""
    dims = [MagicMock(dimension_id="D1"), MagicMock(dimension_id="D2")]
    chosen = [{"expert_id": f"E{i}"} for i in range(4)]
    ass = _assign_dimensions(chosen, dims, min_per_dim=1)
    # 每专家恰好 1 个维度
    assert all(len(v) == 1 for v in ass.values())
    # 两维度都有人
    assert sum(1 for v in ass.values() if "D1" in v) >= 1
    assert sum(1 for v in ass.values() if "D2" in v) >= 1


def test_assign_dimensions_min_per_dim():
    """min_per_dim=2：专家不足时每维度至少 2 人（重复指派补齐）。"""
    dims = [MagicMock(dimension_id="D1"), MagicMock(dimension_id="D2")]
    chosen = [{"expert_id": f"E{i}"} for i in range(3)]  # 3 专家 < 2×2=4 需求
    ass = _assign_dimensions(chosen, dims, min_per_dim=2)
    for d in ("D1", "D2"):
        assert sum(1 for v in ass.values() if d in v) >= 2


def test_assign_dimensions_empty():
    """无维度 → 空分配。"""
    from sqlalchemy import text  # noqa: F401

    assert _assign_dimensions([{"expert_id": "E1"}], [], min_per_dim=1) == {}


from unittest.mock import MagicMock


# ==================== P8 异常兜底：Neo4j 图检降级 ====================


@pytest.mark.asyncio
async def test_conflicts_with_status_returns_graph_error(monkeypatch):
    """Neo4j 挂 → _conflicts_with_status 返回 ({}, True)（图检跳过，匹配降级）。"""
    import app.services.expert_match_service as m

    def _boom(*a, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(m.neo4j, "get_driver", _boom)
    conflicts, graph_error = await m._conflicts_with_status(["E1", "E2"], ["S1"])
    assert conflicts == {}
    assert graph_error is True


@pytest.mark.asyncio
async def test_find_conflicts_degrades_on_neo4j_down(monkeypatch):
    """薄封装 _find_conflicts：图检降级时返回空 dict（调用方获兼容签名）。"""
    import app.services.expert_match_service as m

    async def _fail(eids, sids):
        return {}, True

    monkeypatch.setattr(m, "_conflicts_with_status", _fail)
    assert await m._find_conflicts(["E1"], ["S1"]) == {}


@pytest.mark.asyncio
async def test_conflicts_with_status_empty_experts():
    """空专家列表 → 不触 Neo4j，直接 ({}, False)。"""
    import app.services.expert_match_service as m

    conflicts, graph_error = await m._conflicts_with_status([], ["S1"])
    assert conflicts == {} and graph_error is False
