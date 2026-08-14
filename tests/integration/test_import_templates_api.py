"""导入模板下载 API 集成测试（P6.2 补）。

覆盖：三种模板下载 200 且内容非空、CSV 含全部列头（BOM utf-8-sig）、非 ADMIN 403、
未知类型 404。
"""

from __future__ import annotations

from app.core import importer


async def test_download_expert_template(client, admin_headers):
    resp = await client.get("/api/v1/import-templates/expert", headers=admin_headers)
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    assert len(resp.content) > 100  # xlsx 文件非空


async def test_download_supplier_template(client, admin_headers):
    resp = await client.get("/api/v1/import-templates/supplier", headers=admin_headers)
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]
    assert len(resp.content) > 100


async def test_download_conflict_template_has_headers(client, admin_headers):
    resp = await client.get("/api/v1/import-templates/conflict", headers=admin_headers)
    assert resp.status_code == 200
    # utf-8-sig 解码（含 BOM），列头必须与 importer 唯一约定一致
    body = resp.content.decode("utf-8-sig")
    for h in importer.CONFLICT_CSV_HEADERS:
        assert h in body


async def test_download_template_forbidden_for_pm(client, pm_headers):
    resp = await client.get("/api/v1/import-templates/expert", headers=pm_headers)
    assert resp.status_code == 403


async def test_download_template_unknown_type(client, admin_headers):
    resp = await client.get("/api/v1/import-templates/foo", headers=admin_headers)
    assert resp.status_code == 404
