"""P5.3 综合深度检测验收脚本（本地 uvicorn :8001 + arq worker）。

覆盖 task.md P5.3 验收：
- 围串标组：2 份相似标书（shared_seed 同段落）+ 相同报价 + Neo4j SAME_CONTROLLER
  → deep_detection 综合风险 ≥ MEDIUM（>25）正确触发
- 正常组：2 份内容/报价各异、无关联 → LOW
- 单元：risk_level 四级、_deep_price_check 集中/陪标

前置：uvicorn :8001 + 本机 arq worker；bge-m3；MySQL/Neo4j/Milvus。
用法: poetry run python scripts/accept_p53_api.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time

import httpx
from neo4j import GraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402
from app.core.minio_client import get_minio_client, remove_prefix  # noqa: E402

BASE = "http://localhost:8001/api/v1"
PASS = 0
FAIL = 0

# 8 章结构（P5.3 回归：检测层命中对数阈值 7 后，围串标组需切出 ≥8 chunks 才达标；
# 正常组用各异正文，保持 0 误报）
_CN = "一二三四五六七八九十"
_CHAPTERS = ["公司概况", "项目理解", "技术方案", "实施计划", "项目管理",
             "售后服务", "质量保证", "商务承诺"]
NORMAL_C = [
    "我司成立于2010年，注册资本5000万元，专注于企业信息化系统建设与运维服务。",
    "本项目为政务云平台迁移，我方理解重点在于数据迁移的平滑性与业务连续性。",
    "采用传统瀑布式开发流程，重点关注成本控制与交付稳定性，技术栈以Java为主。",
    "项目周期较长，分三期交付，每期范围固定，不包含额外定制开发服务。",
    "团队成员以业务分析为主，技术实现由外部专业厂商协同承担。",
    "我司设有专职售后团队，工作日在线值守，问题48小时内反馈处理。",
    "按照ISO9001流程管理，关键交付节点安排内部评审与质量抽查。",
    "报价保守，不含额外定制服务费，付款按里程碑分期支付。",
]
NORMAL_D = [
    "我司成立于2015年，核心团队来自金融行业，长期专注国产化软件替代。",
    "本项目强调自主可控与信创适配，需适配主流国产服务器、数据库与中间件。",
    "采用纯国产数据库方案，物理机集群部署，不依赖容器与微服务架构。",
    "一次性整体交付，上线前完成充分的功能、性能与安全回归测试。",
    "项目通过多项资质认证，实施团队经验丰富，安排驻场服务三个月。",
    "提供7×24小时远程支持与应急响应机制，质保期长达36个月。",
    "产品通过CMMI3认证，测试与评审流程按既定规范执行，文档齐全。",
    "报价包含三年免费升级与驻场支持服务，总价包干不再追加费用。",
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_pdf(company: str, amount: int, seed: int | None = None,
             body: list[str] | None = None) -> bytes:
    """8 章结构：body 提供→正常组各异内容；否则→seed 模板（围串标组共用 → 高相似）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 10)
    lines = [f"供应商：{company}", f"投标总报价：{amount:,}元"]
    for i, ch in enumerate(_CHAPTERS):
        lines.append(f"第{_CN[i]}章 {ch}")
        lines.append(body[i] if body else f"我方针对{ch}维度提供完整响应，采用统一模板描述。段落{seed}-{i}")
    y = 760
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 18
    c.save()
    return buf.getvalue()


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")


async def login() -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
        assert r.status_code == 200
        return r.json()["access_token"]


async def upload_parse(token: str, lot: str, sup: str, company: str, amount: int, seed: int,
                       body: list[str] | None = None) -> str:
    files = {"file": (f"{company}_标书.pdf", make_pdf(company, amount, seed, body), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/lots/{lot}/bids", headers={"Authorization": f"Bearer {token}"},
                              files=files, data={"supplier_id": sup})
    bid_id = r.json()["bid_id"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{BASE}/bids/{bid_id}/status", headers={"Authorization": f"Bearer {token}"})
        if r.json()["status"] == "PARSED":
            return bid_id
        await asyncio.sleep(5)
    return bid_id


async def cleanup(engine, driver, lots) -> None:
    async with engine.begin() as conn:
        for lot, sup in lots:
            await conn.execute(text(
                "DELETE FROM bid_document WHERE lot_id=:l AND supplier_id=:s AND file_url LIKE 'bids/%'"),
                {"l": lot, "s": sup})
    for lot, _ in lots:
        remove_prefix(get_minio_client(), f"bids/{lot}/")
    with driver.session() as s:
        s.run("MATCH (a:Supplier {supplierId:'SUP-001'})-[r:SAME_CONTROLLER]->(b:Supplier {supplierId:'SUP-002'}) DELETE r")


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    lots = [("LOT-003", "SUP-001"), ("LOT-003", "SUP-002"), ("LOT-004", "SUP-001"), ("LOT-005", "SUP-001")]
    await cleanup(engine, driver, lots)
    print("[cleanup] 验收前残留已清理")

    # 单元断言
    from app.services.fraud_detection_service import _deep_price_check, risk_level

    check("risk_level 四级", risk_level(10) == "LOW" and risk_level(30) == "MEDIUM"
          and risk_level(60) == "HIGH" and risk_level(80) == "CRITICAL")
    pscore, pev = _deep_price_check([3280000, 3280000])
    check("报价集中（相同价）→ +40", pscore >= 40, str(pev))
    pscore2, _ = _deep_price_check([3200000, 3280000, 3400000])
    check("报价分散（价差>1% 且无陪标）→ 0", pscore2 == 0, f"score={pscore2}")
    pscore3, _ = _deep_price_check([1500000, 3280000])
    check("陪标（最低显著低）→ +40", pscore3 >= 40, f"score={pscore3}")

    # 围串标组：LOT-003 SUP-001/002 相似 + 同价 + SAME_CONTROLLER
    token = await login()
    print("\n[围串标组] 相似+同价+同控制人")
    bid_a = await upload_parse(token, "LOT-003", "SUP-001", "围甲", 3280000, 42)
    bid_b = await upload_parse(token, "LOT-003", "SUP-002", "围乙", 3280000, 42)
    with driver.session() as s:
        s.run("MATCH (a:Supplier {supplierId:'SUP-001'}), (b:Supplier {supplierId:'SUP-002'}) "
              "MERGE (a)-[:SAME_CONTROLLER]->(b)")

    from app.services.fraud_detection_service import deep_detection

    r1 = await deep_detection("LOT-003", [bid_a, bid_b])
    check("围串标组风险 ≥ MEDIUM", r1["risk"] in ("MEDIUM", "HIGH", "CRITICAL"),
          f"risk={r1['risk']} total={r1['total_score']}")
    check("围串标组综合分 >25", r1["total_score"] > 25, f"score={r1['total_score']}")
    check("图检命中 SAME_CONTROLLER", r1["scores"]["graph"] >= 40, str(r1["scores"]))
    check("报价检命中（集中）", r1["scores"]["price"] >= 40, str(r1["scores"]))
    check("文本检命中（相似）", r1["scores"]["text"] > 0, str(r1["scores"]))

    # 正常组：LOT-004/005 内容报价各异、无关联
    print("\n[正常组] 各异 + 无关联")
    bid_c = await upload_parse(token, "LOT-004", "SUP-001", "正常丙", 3280000, 7, body=NORMAL_C)
    bid_d = await upload_parse(token, "LOT-005", "SUP-001", "正常丁", 4500000, 99, body=NORMAL_D)
    r2 = await deep_detection("LOT-004", [bid_c, bid_d])
    check("正常组风险 LOW", r2["risk"] == "LOW", f"risk={r2['risk']} total={r2['total_score']}")

    # 清理
    await cleanup(engine, driver, lots)
    print("\n[cleanup] 验收数据已清理（含测试 SAME_CONTROLLER 关系）")
    driver.close()
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
