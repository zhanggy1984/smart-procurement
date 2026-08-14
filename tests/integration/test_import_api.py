"""P7.3 专家/供应商/冲突批量导入 API 集成测试（task.md #15-#17）。

成功：合法行 → 201 + imported 计数 + Neo4j 同步（一致性场景）。
错误：空文件 400；格式错误（缺列/非法 Excel）422。
"""

from __future__ import annotations

import csv
import io
from io import BytesIO

import openpyxl
import pytest

# 列头与 app/core/importer.py 模板约定一致（ITEST 前缀避免与 seed 冲突）
EXPERT_HEADERS = ["编号", "姓名", "单位", "地区", "从业年限", "专业标签", "身份证号", "邮箱", "电话"]
SUPPLIER_HEADERS = ["编号", "企业名称", "统一社会信用代码", "法定代表人", "所属行业", "企业规模"]
CONFLICT_HEADERS = ["姓名", "企业名称", "统一社会信用代码", "关系类型", "职位", "持股比例"]

EXPERT_ID = "310101199001010011"  # 18 位身份证


def _excel(headers: list[str], rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_file(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ==================== 专家导入 ====================


@pytest.mark.asyncio
async def test_import_experts_success(client, admin_headers):
    """合法 1 行 → imported=1 + 自动建登录账号。"""
    xlsx = _excel(EXPERT_HEADERS, [
        ["", "ITEST导入专家一", "集成测试大学", "华东", 10, "软件开发", EXPERT_ID, "e1@x.com", "13800000001"],
    ])
    resp = await client.post("/api/v1/experts/import", headers=admin_headers,
                             files={"file": ("experts.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 201
    assert resp.json()["imported"] == 1


@pytest.mark.asyncio
async def test_import_experts_empty_400(client, admin_headers):
    """无数据行 → 400。"""
    xlsx = _excel(EXPERT_HEADERS, [])
    resp = await client.post("/api/v1/experts/import", headers=admin_headers,
                             files={"file": ("experts.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_experts_format_error_422(client, admin_headers):
    """缺列 → 422。"""
    xlsx = _excel(["姓名", "单位"], [["张三", "x"]])
    resp = await client.post("/api/v1/experts/import", headers=admin_headers,
                             files={"file": ("experts.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_import_experts_validation_error_422(client, admin_headers):
    """身份证非法 → 行级校验 422（整批不导入）。"""
    xlsx = _excel(EXPERT_HEADERS, [
        ["", "ITEST导入专家二", "集成测试大学", "华东", 10, "软件开发", "123", "e2@x.com", "13800000002"],
    ])
    resp = await client.post("/api/v1/experts/import", headers=admin_headers,
                             files={"file": ("experts.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 422


# ==================== 供应商导入 ====================


@pytest.mark.asyncio
async def test_import_suppliers_success(client, admin_headers):
    xlsx = _excel(SUPPLIER_HEADERS, [
        ["", "ITEST导入供应商一", "913100001444444444", "法人A", "软件", "LARGE"],
    ])
    resp = await client.post("/api/v1/suppliers/import", headers=admin_headers,
                             files={"file": ("suppliers.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 201
    assert resp.json()["imported"] == 1


@pytest.mark.asyncio
async def test_import_suppliers_empty_400(client, admin_headers):
    xlsx = _excel(SUPPLIER_HEADERS, [])
    resp = await client.post("/api/v1/suppliers/import", headers=admin_headers,
                             files={"file": ("suppliers.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_suppliers_credit_code_invalid_422(client, admin_headers):
    xlsx = _excel(SUPPLIER_HEADERS, [
        ["", "ITEST导入供应商二", "123", "法人B", "软件", "MEDIUM"],
    ])
    resp = await client.post("/api/v1/suppliers/import", headers=admin_headers,
                             files={"file": ("suppliers.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert resp.status_code == 422


# ==================== 企查查冲突导入 ====================


@pytest.mark.asyncio
async def test_import_conflicts_matched_writes_neo4j(client, admin_headers):
    """专家甲↔供应商乙（任职）→ 双匹配 → 写入 Neo4j EMPLOYED_BY 关系。"""
    # seed 只写 MySQL；冲突关系需两端节点已存在（真实流程由专家/供应商导入同步），先建节点
    from app.services import neo4j_sync

    await neo4j_sync.upsert_expert("ITEST-E1", name="集成测试专家甲", organization="集成测试大学",
                                   region="华东", experience=15, status="ACTIVE")
    await neo4j_sync.upsert_supplier("ITEST-S2", name="集成测试供应商乙",
                                     uniform_credit_code="913100001222222222",
                                     legal_person="法人乙", industry="软件", scale="MEDIUM",
                                     blacklisted=False)
    csv_bytes = _csv_file(CONFLICT_HEADERS, [
        ["集成测试专家甲", "集成测试供应商乙", "913100001222222222", "任职", "技术总监", ""],
    ])
    resp = await client.post("/api/v1/conflicts/import", headers=admin_headers,
                             files={"file": ("conflicts.csv", csv_bytes, "text/csv")})
    assert resp.status_code == 201
    body = resp.json()
    assert body["total"] == 1
    assert body["matched"] == 1
    # 验证 Neo4j 关系（P1.4 commit 后直同步）
    from app.core import neo4j

    driver = neo4j.get_driver()
    async with driver.session() as s:
        rows = await (await s.run(
            "MATCH (e:Expert {expertId:'ITEST-E1'})-[r:EMPLOYED_BY]->(sup:Supplier {supplierId:'ITEST-S2'}) "
            "RETURN r.role")).data()
    assert rows, "Neo4j 应存在 EMPLOYED_BY 关系"
    assert rows[0]["r.role"] == "技术总监"


@pytest.mark.asyncio
async def test_import_conflicts_empty_400(client, admin_headers):
    csv_bytes = _csv_file(CONFLICT_HEADERS, [])
    resp = await client.post("/api/v1/conflicts/import", headers=admin_headers,
                             files={"file": ("conflicts.csv", csv_bytes, "text/csv")})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_conflicts_format_error_422(client, admin_headers):
    """缺列 → 422。"""
    csv_bytes = _csv_file(["姓名", "企业名称"], [["张三", "x"]])
    resp = await client.post("/api/v1/conflicts/import", headers=admin_headers,
                             files={"file": ("conflicts.csv", csv_bytes, "text/csv")})
    assert resp.status_code == 422
