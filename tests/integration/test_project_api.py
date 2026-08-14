"""P7.3 项目/标段/维度/遴选 API 集成测试（task.md #2-#5）。

成功路径：创建项目/标段/维度/遴选 → 201 + 状态正确。
错误路径：未认证 401、角色不符 403、region/type 非法 422、code 重复 409、
标段超项目预算 422、项目不存在 404、权重和≠1.0 422、维度名为空 422、
遴选权重和错 422、expert_count < min 422。
"""

from __future__ import annotations

import secrets

import pytest


def _code(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


async def _mk_project(client, headers, **over):
    body = {
        "project_code": _code("ITEST-PJ"),
        "name": "集成测试项目",
        "type": "SERVICE",
        "region": "华东",
        "budget": "2000000",
    }
    body.update(over)
    return await client.post("/api/v1/projects", headers=headers, json=body)


async def _mk_lot(client, headers, project_id, **over):
    body = {"lot_code": _code("ITEST-LT"), "name": "集成测试标段", "budget": "500000"}
    body.update(over)
    return await client.post(f"/api/v1/projects/{project_id}/lots", headers=headers, json=body)


DIMS_OK = [
    {"name": "报价", "max_score": "20", "weight": "0.2", "criteria": []},
    {"name": "技术", "max_score": "30", "weight": "0.3", "criteria": []},
    {"name": "商务", "max_score": "25", "weight": "0.25", "criteria": []},
    {"name": "服务", "max_score": "15", "weight": "0.15", "criteria": []},
    {"name": "资信", "max_score": "10", "weight": "0.1", "criteria": []},
]


async def _mk_dims(client, headers, lot_id, dims):
    return await client.post(f"/api/v1/lots/{lot_id}/dimensions", headers=headers,
                             json={"dimensions": dims})


CRIT_OK = {
    "expert_count": 5, "min_experts_per_dimension": 1,
    "weight_specialization": "0.4", "weight_experience": "0.3",
    "weight_review_quality": "0.2", "weight_region": "0.1", "min_experience": 3,
}


async def _mk_crit(client, headers, lot_id, **over):
    body = dict(CRIT_OK)
    body.update(over)
    return await client.post(f"/api/v1/lots/{lot_id}/expert-criteria", headers=headers, json=body)


# ==================== 创建项目 ====================


@pytest.mark.asyncio
async def test_create_project_success(client, pm_headers):
    resp = await _mk_project(client, pm_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["type"] == "SERVICE"
    assert body["region"] == "华东"


@pytest.mark.asyncio
async def test_create_project_unauthorized_401(client):
    resp = await _mk_project(client, {})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_project_forbidden_supplier_403(client, sup_headers):
    resp = await _mk_project(client, sup_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_project_region_invalid_422(client, pm_headers):
    resp = await _mk_project(client, pm_headers, region="火星")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_project_type_invalid_422(client, pm_headers):
    resp = await _mk_project(client, pm_headers, type="UNKNOWN")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_project_code_duplicate_409(client, pm_headers):
    r1 = await _mk_project(client, pm_headers)
    assert r1.status_code == 201
    code = r1.json()["project_code"]
    r2 = await _mk_project(client, pm_headers, project_code=code)
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_get_project_detail(client, pm_headers):
    r = await _mk_project(client, pm_headers)
    project_id = r.json()["project_id"]
    resp = await client.get(f"/api/v1/projects/{project_id}", headers=pm_headers)
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project_id


@pytest.mark.asyncio
async def test_get_project_not_found_404(client, pm_headers):
    resp = await client.get("/api/v1/projects/ITEST-PJ-MISSING", headers=pm_headers)
    assert resp.status_code == 404


# ==================== 创建标段 ====================


@pytest.mark.asyncio
async def test_create_lot_success(client, pm_headers):
    r = await _mk_project(client, pm_headers)
    project_id = r.json()["project_id"]
    resp = await _mk_lot(client, pm_headers, project_id)
    assert resp.status_code == 201
    assert resp.json()["status"] == "BIDDING"


@pytest.mark.asyncio
async def test_create_lot_exceeds_project_budget_422(client, pm_headers):
    r = await _mk_project(client, pm_headers, budget="1000000")
    project_id = r.json()["project_id"]
    # 标段预算超过项目预算（100 万）→ 422
    resp = await _mk_lot(client, pm_headers, project_id, budget="1200000")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_lot_project_not_found_404(client, pm_headers):
    resp = await _mk_lot(client, pm_headers, "ITEST-PJ-MISSING")
    assert resp.status_code == 404


# ==================== 评分维度 ====================


@pytest.mark.asyncio
async def test_add_dimensions_success(client, pm_headers, lot_factory):
    lot = await lot_factory()
    resp = await _mk_dims(client, pm_headers, lot["lot_id"], DIMS_OK)
    assert resp.status_code == 201
    assert len(resp.json()) == 5


@pytest.mark.asyncio
async def test_add_dimensions_weight_sum_wrong_422(client, pm_headers, lot_factory):
    lot = await lot_factory()
    bad = [  # 权重和 0.6+0.6=1.2 ≠ 1.0
        {"name": "技术", "max_score": "50", "weight": "0.6", "criteria": []},
        {"name": "商务", "max_score": "50", "weight": "0.6", "criteria": []},
    ]
    resp = await _mk_dims(client, pm_headers, lot["lot_id"], bad)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_dimensions_empty_name_422(client, pm_headers, lot_factory):
    lot = await lot_factory()
    bad = [{"name": "", "max_score": "100", "weight": "1.0", "criteria": []}]
    resp = await _mk_dims(client, pm_headers, lot["lot_id"], bad)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_add_dimensions_lot_not_found_404(client, pm_headers):
    resp = await _mk_dims(client, pm_headers, "ITEST-LT-MISSING", DIMS_OK)
    assert resp.status_code == 404


# ==================== 专家遴选参数 ====================


@pytest.mark.asyncio
async def test_expert_criteria_success(client, pm_headers, lot_factory):
    lot = await lot_factory()
    resp = await _mk_crit(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 201
    assert resp.json()["expert_count"] == 5


@pytest.mark.asyncio
async def test_expert_criteria_weight_sum_wrong_422(client, pm_headers, lot_factory):
    lot = await lot_factory()
    resp = await _mk_crit(client, pm_headers, lot["lot_id"],
                          weight_specialization="0.5", weight_experience="0.3",
                          weight_review_quality="0.2", weight_region="0.1")  # 和=1.1
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_expert_criteria_count_lt_min_422(client, pm_headers, lot_factory):
    lot = await lot_factory()
    resp = await _mk_crit(client, pm_headers, lot["lot_id"],
                          expert_count=2, min_experts_per_dimension=5)
    assert resp.status_code == 422
