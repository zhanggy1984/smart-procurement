"""P6.2 专家/工商信息列表接口集成测试。

覆盖：GET /experts 分页 200、GET /pending-conflicts 分页 200、非 ADMIN 403。
"""

from __future__ import annotations


async def test_list_experts_admin(client, admin_headers):
    resp = await client.get("/api/v1/experts", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body and "items" in body
    assert isinstance(body["items"], list)
    # 合成数据含专家，字段齐全
    if body["items"]:
        item = body["items"][0]
        assert "name" in item and "tags" in item and "status" in item


async def test_list_experts_keyword(client, admin_headers):
    resp = await client.get("/api/v1/experts?keyword=张", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json()["items"], list)


async def test_list_experts_forbidden_for_pm(client, pm_headers):
    resp = await client.get("/api/v1/experts", headers=pm_headers)
    assert resp.status_code == 403


async def test_list_pending_conflicts_admin(client, admin_headers):
    resp = await client.get("/api/v1/pending-conflicts", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body and "items" in body


async def test_list_pending_conflicts_status_filter(client, admin_headers):
    resp = await client.get("/api/v1/pending-conflicts?status=PENDING", headers=admin_headers)
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["status"] == "PENDING"


async def test_list_pending_conflicts_forbidden_for_pm(client, pm_headers):
    resp = await client.get("/api/v1/pending-conflicts", headers=pm_headers)
    assert resp.status_code == 403
