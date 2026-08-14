"""P5.2 深度语义相似度验收脚本（本地 uvicorn :8001 + arq worker）。

覆盖 task.md P5.2 验收：
- 上传 4 份标书：2 份高相似（shared_seed 同段落，模拟围串标）+ 2 份正常
- deep_text_similarity（FAISS 批量 cosine）→ 相似组标书间高相似段落对命中
- 正常组 0 误报（无高相似对）

前置：uvicorn :8001 + 本机 arq worker；bge-m3；MySQL/Milvus。
用法: poetry run python scripts/accept_p52_api.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time

import httpx
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

# 8 章结构（P5.2 回归：检测层命中对数阈值 7 后，围串标组需切出 ≥8 chunks 才达标；
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

# 4 份标书组合（不同 lot + SUP-001）
DOCS = [
    ("LOT-003", "SUP-001", "相似甲", 42, None),
    ("LOT-004", "SUP-001", "相似乙", 42, None),
    ("LOT-005", "SUP-001", "正常丙", None, NORMAL_C),
    ("LOT-006", "SUP-001", "正常丁", None, NORMAL_D),
]
SIM_PAIR = (0, 1)  # 相似组下标
NORMAL_PAIR = (2, 3)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_pdf(company: str, seed: int | None = None, body: list[str] | None = None) -> bytes:
    """8 章结构：body 提供→正常组各异内容；否则→seed 模板（相似组共用 → 高相似）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 10)
    lines = [f"供应商：{company}"]
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


async def login(username: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": TEST_PASSWORD})
        assert r.status_code == 200
        return r.json()["access_token"]


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)

    # 清理该 4 lot 的验收标书
    async with engine.begin() as conn:
        for lot, sup, *_ in DOCS:
            await conn.execute(text(
                "DELETE FROM bid_document WHERE lot_id=:l AND supplier_id=:s AND file_url LIKE 'bids/%'"),
                {"l": lot, "s": sup})
    for lot, *_ in DOCS:
        remove_prefix(get_minio_client(), f"bids/{lot}/")
    print("[cleanup] 验收前残留已清理")

    token = await login("admin")
    headers = {"Authorization": f"Bearer {token}"}
    bid_ids: list[str] = []
    for lot, sup, company, seed, body in DOCS:
        files = {"file": (f"{company}_标书.pdf", make_pdf(company, seed, body), "application/pdf")}
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{BASE}/lots/{lot}/bids", headers=headers,
                                  files=files, data={"supplier_id": sup})
        bid_id = r.json()["bid_id"]
        bid_ids.append(bid_id)
        # 等解析
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(f"{BASE}/bids/{bid_id}/status", headers=headers)
            if r.json()["status"] == "PARSED":
                break
            await asyncio.sleep(5)
        check(f"{company} 标书 PARSED", r.json()["status"] == "PARSED", f"{company}")
    check("4 份标书解析完成", len(bid_ids) == 4, f"n={len(bid_ids)}")

    # ==================== FAISS 深度语义检测 ====================
    print("\n[FAISS] 深度语义相似度")
    from app.services.fraud_detection_service import deep_text_similarity, _faiss_similar_pairs

    result = await deep_text_similarity("LOT-DEEP-52", bid_ids)
    pair_hits = result["bid_similar_pairs"]
    sim_bids = {bid_ids[i] for i in SIM_PAIR}
    normal_bids = {bid_ids[i] for i in NORMAL_PAIR}

    check("高相似段落对 >0", result["high_similar_pairs"] > 0, f"pairs={result['high_similar_pairs']}")
    check("相似组标书命中（围串标 100% 召回）",
          any(set(p) == sim_bids for p in pair_hits), f"pairs={pair_hits}")
    check("正常组无高相似对（0 误报）",
          all(normal_bids != set(p) for p in pair_hits), f"pairs={pair_hits}")

    # FAISS 单测：同文本 → 高分对；无关文本 → 无
    check("FAISS 单元：相同 chunk 高分", len(_faiss_similar_pairs([
        {"chunk_id": "a", "bid_id": "B1", "embedding": [1.0, 0.0]},
        {"chunk_id": "b", "bid_id": "B1", "embedding": [1.0, 0.0]},
    ])) >= 1)
    check("FAISS 单元：正交向量无对", len(_faiss_similar_pairs([
        {"chunk_id": "a", "bid_id": "B1", "embedding": [1.0, 0.0]},
        {"chunk_id": "b", "bid_id": "B1", "embedding": [0.0, 1.0]},
    ])) == 0)

    # 清理
    async with engine.begin() as conn:
        for lot, sup, *_ in DOCS:
            await conn.execute(text(
                "DELETE FROM bid_document WHERE lot_id=:l AND supplier_id=:s AND file_url LIKE 'bids/%'"),
                {"l": lot, "s": sup})
    for lot, *_ in DOCS:
        remove_prefix(get_minio_client(), f"bids/{lot}/")
    print("\n[cleanup] 验收数据已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
