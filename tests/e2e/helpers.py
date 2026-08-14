"""P7.4 E2E 数据工厂与公共工具。

- 文件生成：专家/供应商 Excel、冲突 CSV、可解析中文标书 PDF（reportlab STSong，
  验证过 pdfplumber 提取 + RE_AMOUNT 正则命中）
- 导入工厂：API 上传 Excel/CSV（导入自动建登录账号，password=Smart@2026）
- 账号解析：专家按 expert.user_id join users；供应商按 users.display_name=supplier.name
- 项目工厂：API 建 项目→标段→5 维度（权重和 1.0）→专家遴选参数
- 投标工厂：API 上传可解析 PDF
"""

from __future__ import annotations

import io
import os
import re
import secrets
from pathlib import Path

import openpyxl

from conftest import PASSWORD, PREFIX, _sql, BASE_URL

EXPERT_HEADERS = ["编号", "姓名", "单位", "地区", "从业年限", "专业标签", "身份证号", "邮箱", "电话"]
SUPPLIER_HEADERS = ["编号", "企业名称", "统一社会信用代码", "法定代表人", "所属行业", "企业规模"]
CONFLICT_HEADERS = ["姓名", "企业名称", "统一社会信用代码", "关系类型", "职位", "持股比例"]

DIMS = [
    {"name": "报价", "max_score": "20", "weight": "0.2", "criteria": []},
    {"name": "技术", "max_score": "30", "weight": "0.3", "criteria": []},
    {"name": "商务", "max_score": "25", "weight": "0.25", "criteria": []},
    {"name": "服务", "max_score": "15", "weight": "0.15", "criteria": []},
    {"name": "资信", "max_score": "10", "weight": "0.1", "criteria": []},
]


# ==================== 文件生成 ====================


def _xlsx(headers: list[str], rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv(headers: list[str], rows: list[list]) -> bytes:
    import csv

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _id18(seq: int) -> str:
    """生成唯一 18 位身份证（避免残留 hash 去重导致第二次运行 skipped）。"""
    return f"3101011995010{seq:07d}"[:18]


def make_expert_excel(rows: list[dict]) -> bytes:
    """rows: [{expert_id, name, region, exp, tags(list), ...}] 身份证自动生成。"""
    data = []
    for i, r in enumerate(rows):
        data.append([
            r["expert_id"], r["name"], r.get("org", "E2E大学"), r["region"], r["exp"],
            ";".join(r["tags"]), _id18(1000 + i), r.get("email", f"e{i}@e2e.local"), "",
        ])
    return _xlsx(EXPERT_HEADERS, data)


def make_supplier_excel(rows: list[dict]) -> bytes:
    data = []
    for i, r in enumerate(rows):
        data.append([
            r["supplier_id"], r["name"], r["code"], r.get("legal", "E2E法人"),
            r.get("industry", "软件开发"), r.get("scale", "LARGE"),
        ])
    return _xlsx(SUPPLIER_HEADERS, data)


def make_conflict_csv(rows: list[dict]) -> bytes:
    data = []
    for i, r in enumerate(rows):
        data.append([r["expert_name"], r["supplier_name"], r["code"], r["rel_type"],
                     r.get("role", "技术总监"), r.get("share", "")])
    return _csv(CONFLICT_HEADERS, data)


def make_bid_pdf(*, amount: str = "1,234,567", amount_unit: str = "元", pages: int = 2,
                 seed_text: str = "E2E标书技术方案") -> bytes:
    """可解析中文标书 PDF：reportlab STSong-Light（已实测 pdfplumber 提取 + RE_AMOUNT 命中）。

    金额/工期/团队/质保/ISO 措辞对齐 document_ingest 正则，解析后结构化字段可命中。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    lines = [
        "投标总报价：" + amount + " " + amount_unit,
        "计划工期：120 日历天",
        "项目团队：45 人",
        "质保期：24 个月",
        "具备 ISO9001 认证",
        "",
        seed_text,
        "本标段技术方案：针对项目需求设计分层架构，包含安全防护、数据治理、",
        "容灾备份、运维服务等完整体系，采用成熟稳定技术栈，保障系统长期可靠运行。",
        "项目管理方面配备专职项目经理与实施团队，按里程碑交付并组织专项验收。",
    ]
    for page in range(pages):
        c.setFont("STSong-Light", 12)
        y = 800
        for ln in lines:
            c.drawString(60, y, ln)
            y -= 22
        if page < pages - 1:
            c.showPage()
    c.save()
    return buf.getvalue()


# ==================== 导入（API，自动建登录账号） ====================


def import_experts(api, rows: list[dict]) -> None:
    r = api.post("/experts/import", files={
        "file": ("experts.xlsx", make_expert_excel(rows),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 201, f"专家导入失败: {r.text}"
    assert r.json()["imported"] == len(rows), r.text


def import_suppliers(api, rows: list[dict]) -> None:
    r = api.post("/suppliers/import", files={
        "file": ("suppliers.xlsx", make_supplier_excel(rows),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 201, f"供应商导入失败: {r.text}"
    assert r.json()["imported"] == len(rows), r.text


def import_conflicts(api, rows: list[dict]) -> None:
    r = api.post("/conflicts/import", files={"file": ("conflicts.csv", make_conflict_csv(rows), "text/csv")})
    assert r.status_code == 201, f"冲突导入失败: {r.text}"
    return r.json()


# ==================== 账号解析（导入后查登录账号） ====================


def expert_username(expert_id: str) -> str:
    rows = _sql("SELECT u.username FROM users u JOIN expert e ON e.user_id = u.user_id "
                "WHERE e.expert_id = :id", {"id": expert_id})
    assert rows, f"专家账号未找到: {expert_id}"
    return rows[0][0]


def supplier_username(supplier_id: str) -> str:
    rows = _sql("SELECT u.username FROM users u JOIN supplier s ON s.name = u.display_name "
                "WHERE s.supplier_id = :id", {"id": supplier_id})
    assert rows, f"供应商账号未找到: {supplier_id}"
    return rows[0][0]


def expert_name(expert_id: str) -> str:
    rows = _sql("SELECT name FROM expert WHERE expert_id = :id", {"id": expert_id})
    return rows[0][0]


def supplier_name(supplier_id: str) -> str:
    rows = _sql("SELECT name FROM supplier WHERE supplier_id = :id", {"id": supplier_id})
    return rows[0][0]


# ==================== Neo4j 辅助（E2E 前置图数据） ====================


def neo4j_run(cypher: str, params: dict | None = None) -> None:
    """执行 Cypher（E2E 造 SAME_CONTROLLER 等无导入途径的图关系）。"""
    import asyncio

    from conftest import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

    async def _do():
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        try:
            async with driver.session() as s:
                await s.run(cypher, params or {})
        finally:
            await driver.close()

    import threading

    box: dict = {}

    def _worker():
        box["v"] = asyncio.run(_do())

    t = threading.Thread(target=_worker)
    t.start()
    t.join()


# ==================== 项目工厂（API） ====================


def create_project_full(pm_api, *, budget: int = 2_000_000, lot_budget: int = 500_000,
                        dims: list[dict] | None = None) -> tuple[str, str]:
    """项目→标段→5 维度→专家遴选参数。返回 (project_id, lot_id)。"""
    tag = secrets.token_hex(3)
    r = pm_api.post("/projects", json={
        "project_code": f"E2E-PJ-{tag}", "name": "E2E测试项目",
        # 地区默认「西北」：合成演示数据在西北无"软件开发"标签，隔离匹配池（见 E2E-1 同款注释）
        "type": "SERVICE", "region": "西北", "budget": budget})
    assert r.status_code == 201, r.text
    project_id = r.json()["project_id"]
    r = pm_api.post(f"/projects/{project_id}/lots", json={
        "lot_code": f"E2E-LT-{tag}", "name": "E2E测试标段", "budget": lot_budget})
    assert r.status_code == 201, r.text
    lot_id = r.json()["lot_id"]
    r = pm_api.post(f"/lots/{lot_id}/dimensions", json={"dimensions": dims or DIMS})
    assert r.status_code == 201, r.text
    r = pm_api.post(f"/lots/{lot_id}/expert-criteria", json={
        "expert_count": 5, "min_experts_per_dimension": 1,
        "weight_specialization": "0.4", "weight_experience": "0.3",
        "weight_review_quality": "0.2", "weight_region": "0.1", "min_experience": 3})
    assert r.status_code == 201, r.text
    return project_id, lot_id


def upload_bids(pm_api, lot_id: str, supplier_ids: list[str],
                amounts: list[str] | None = None) -> list[str]:
    """各供应商 API 上传可解析 PDF。返回 bid_ids。lot 需 BIDDING。"""
    from conftest import Api

    bid_ids = []
    for i, sid in enumerate(supplier_ids):
        sup = Api("E2E-DUMMY", supplier_username(sid))
        try:
            amount = amounts[i] if amounts else f"{100 + i * 20},000"
            pdf = make_bid_pdf(amount=amount)
            r = sup.post(f"/lots/{lot_id}/bids", files={"file": ("bid.pdf", pdf, "application/pdf")})
            assert r.status_code == 201, f"{sid} 投标失败: {r.text}"
            bid_ids.append(r.json()["bid_id"])
        finally:
            sup.close()
    return bid_ids


def wait_parsed(lot_id: str, bid_ids: list[str], timeout: int = 180) -> None:
    """等待 arq worker 真实解析完成（PARSED/PARSE_FAILED）。超时失败。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = _sql("SELECT bid_id, status FROM bid_document WHERE lot_id = :l", {"l": lot_id})
        by_id = {r[0]: r[1] for r in rows}
        if all(by_id.get(b) in ("PARSED", "PARSE_FAILED") for b in bid_ids):
            return
        time.sleep(3)
    statuses = {r[0]: r[1] for r in _sql("SELECT bid_id, status FROM bid_document WHERE lot_id = :l",
                                         {"l": lot_id})}
    raise TimeoutError(f"标书解析超时 {timeout}s: {statuses}")


# ==================== 数据库断言辅助 ====================


def lot_status(lot_id: str) -> str:
    rows = _sql("SELECT status FROM lot WHERE lot_id = :id", {"id": lot_id})
    return rows[0][0]


def bid_reviews(bid_id: str) -> list[tuple]:
    """该标书所有评审行 (dimension_name, score, status, expert_id)。"""
    return _sql(
        "SELECT d.name, r.score, r.status, r.expert_id FROM expert_review r "
        "JOIN scoring_dimension d ON d.dimension_id = r.dimension_id "
        "WHERE r.bid_id = :id", {"id": bid_id})


def award_bid(api, lot_id: str) -> str | None:
    """已定标标段 winner bid_id（评标汇总 rank=1；系统无独立 award_result 表）。"""
    s = api.get(f"/lots/{lot_id}/summary").json()
    return next((b["bid_id"] for b in s["bids"] if b["rank"] == 1), None)


def assignment_status(expert_id: str, lot_id: str) -> str:
    rows = _sql("SELECT status FROM lot_expert_assignment WHERE expert_id = :e AND lot_id = :l",
                {"e": expert_id, "l": lot_id})
    return rows[0][0] if rows else None


def review_of(expert_id: str, bid_id: str, dim_name: str) -> tuple:
    rows = _sql(
        "SELECT r.score, r.status FROM expert_review r "
        "JOIN scoring_dimension d ON d.dimension_id = r.dimension_id "
        "WHERE r.expert_id = :e AND r.bid_id = :b AND d.name = :n",
        {"e": expert_id, "b": bid_id, "n": dim_name})
    return rows[0] if rows else (None, None)
