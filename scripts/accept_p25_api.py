"""P2.5 跨标书对比检索验收脚本（本地 uvicorn :8001 + arq worker）。

覆盖 task.md P2.5 验收：
- 同一 lot 上传 2 份标书（不同 supplier）→ 解析
- compare_across_bids 同 query 检索 → 两份标书分别返回 chunks，标注 supplier_id 来源

前置：uvicorn :8001 + 本机 arq worker；bge-m3；Milvus/MySQL。
用法: poetry run python scripts/accept_p25_api.py
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

LOT_ID = "LOT-003"
BIDS = [("SUP-001", "华远云控"), ("SUP-002", "立诚科技")]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_pdf(company: str, words: list[str]) -> bytes:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 12)
    lines = [f"供应商：{company}", "第一章 技术方案", f"我方采用{'、'.join(words)}等关键技术路线。"]
    y = 780
    for ln in lines:
        c.drawString(50, y, ln)
        y -= 20
    c.save()
    return buf.getvalue()


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "123456")


async def login() -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
        assert r.status_code == 200
        return r.json()["access_token"]


async def upload(token: str, sup: str, company: str, words: list[str]) -> str:
    files = {"file": (f"{company}_标书.pdf", make_pdf(company, words), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/lots/{LOT_ID}/bids",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data={"supplier_id": sup},
        )
    assert r.status_code == 201, f"上传失败 {sup}: {r.status_code} {r.text[:200]}"
    return r.json()["bid_id"]


async def wait_parsed(token: str, bid_id: str, timeout: float = 300) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(f"{BASE}/bids/{bid_id}/status", headers={"Authorization": f"Bearer {token}"})
        st = r.json()["status"]
        if st in ("PARSED", "PARSE_FAILED"):
            return st
        await asyncio.sleep(5)
    return st


async def cleanup(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM bid_document WHERE lot_id=:lot AND file_url LIKE 'bids/%'"),
            {"lot": LOT_ID},
        )
    remove_prefix(get_minio_client(), f"bids/{LOT_ID}/")


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)
    await cleanup(engine)
    print("[cleanup] 验收前残留已清理")

    token = await login()
    # 2 份标书：同主题词（技术方案），不同公司
    words_a = ["微服务架构", "容器化部署", "灰度发布"]
    words_b = ["微服务架构", "服务网格", "多租户隔离"]
    bid_ids = []
    for sup, company, words in [(*BIDS[0], words_a), (*BIDS[1], words_b)]:
        bid_id = await upload(token, sup, company, words)
        st = await wait_parsed(token, bid_id)
        check(f"{company} 标书 PARSED", st == "PARSED", f"status={st}")
        bid_ids.append(bid_id)

    # 跨标书对比检索
    from app.ai.rag.retriever import compare_across_bids

    cmp = await compare_across_bids("微服务架构", lot_id=LOT_ID, bid_ids=bid_ids)
    check("返回 2 份标书对比结果", len(cmp) == 2, f"count={len(cmp)}")
    supplier_ids = {d["supplier_id"] for d in cmp}
    check("标注 supplier_id 来源", supplier_ids == {"SUP-001", "SUP-002"}, str(supplier_ids))
    for d in cmp:
        check(f"{d['bid_id']} 各自返回 chunks（supplier={d['supplier_id']}）",
              len(d["results"]) > 0, f"results={len(d['results'])}")
        check(f"{d['bid_id']} 检索正常无降级", d["hint"] is None, f"hint={d['hint']}")
        check(f"{d['bid_id']} chunk 来源标注",
              all(r.source in ("vector", "keyword") for r in d["results"]),
              str({r.source for r in d["results"]}))

    # 清理
    await cleanup(engine)
    print("\n[cleanup] 验收残留已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
