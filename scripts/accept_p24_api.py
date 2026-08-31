"""P2.4 空结果与降级处理验收脚本（本地 uvicorn :8001 + arq worker）。

覆盖 solution.md 5.7 / task.md P2.4 降级路径：
- 相关 query → 正常检索（hint=None）
- 无关 query（语义距离远）→ 全 chunk 最高 IP<0.5 → 拒答 "未找到与该问题相关的依据"
- 标书未解析完成（无 chunks + bid_parsed=False）→ "该标书正在解析中，请稍后再试"
- Milvus 语义检索超时（monkeypatch 10s→0.001s）→ 降级关键词+结构化，
  提示 "语义检索暂不可用"（结果仍含关键词路）
- classify_retrieval 单元断言各分支文案

前置：uvicorn :8001 + 本机 arq worker；bge-m3 容器；Milvus/MySQL。
用法: poetry run python scripts/accept_p24_api.py
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
from app.ai.rag.degradation import DegradationHint, classify_retrieval  # noqa: E402

BASE = "http://localhost:8001/api/v1"
PASS = 0
FAIL = 0

LOT_ID = "LOT-003"
SUP_ID = "SUP-001"
WORDS = ["微服务架构", "容器化部署", "灰度发布", "等保三级", "数据加密"]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_pdf(company: str) -> bytes:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 12)
    lines = [f"供应商：{company}",
             "第一章 技术方案",
             f"我方采用{'、'.join(WORDS)}等关键技术路线。",
             "第二章 安全方案",
             "系统满足等保三级要求，数据全程加密传输。"]
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


async def upload(token: str) -> str:
    files = {"file": ("标书.pdf", make_pdf("华远云控"), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/lots/{LOT_ID}/bids",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data={"supplier_id": SUP_ID},
        )
    assert r.status_code == 201, f"上传失败 {r.status_code} {r.text[:200]}"
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
            text("DELETE FROM bid_document WHERE lot_id=:lot AND supplier_id=:sup AND file_url LIKE 'bids/%'"),
            {"lot": LOT_ID, "sup": SUP_ID},
        )
    remove_prefix(get_minio_client(), f"bids/{LOT_ID}/")


async def main() -> None:
    global PASS, FAIL
    engine = create_async_engine(settings.database_url)
    await cleanup(engine)
    print("[cleanup] 验收前残留已清理")

    # ---- classify_retrieval 单元断言 ----
    print("\n[单元] classify_retrieval 各分支")
    check("语义超时 → SEMANTIC_DOWN",
          classify_retrieval(None, bid_parsed=True, semantic_ok=False) == DegradationHint.SEMANTIC_DOWN)
    check("无结果+未解析 → PARSING",
          classify_retrieval(None, bid_parsed=False, semantic_ok=True) == DegradationHint.PARSING)
    check("无结果+已解析 → NO_EVIDENCE",
          classify_retrieval(None, bid_parsed=True, semantic_ok=True) == DegradationHint.NO_EVIDENCE)
    check("全低分(<0.5) → NO_EVIDENCE",
          classify_retrieval(0.3, bid_parsed=True, semantic_ok=True) == DegradationHint.NO_EVIDENCE)
    check("高分(≥0.5) → None",
          classify_retrieval(0.7, bid_parsed=True, semantic_ok=True) is None)

    # ---- 集成：上传解析 1 份标书 ----
    token = await login()
    bid_id = await upload(token)
    st = await wait_parsed(token, bid_id)
    check("标书 PARSED", st == "PARSED", f"status={st}")

    from app.ai.rag import retriever as R

    # 相关 query → 正常（hint None）
    print("\n[正常路径] 相关 query")
    res, hint = await R.retrieve_with_meta(WORDS[0], lot_id=LOT_ID, bid_id=bid_id)
    check("相关 query hint=None", hint is None, f"hint={hint}")
    check("相关 query 有结果", len(res) > 0, f"results={len(res)}")

    # 无关 query → NO_EVIDENCE（最高 IP<0.5）
    print("\n[拒答] 无关 query（宇宙弦理论）")
    res2, hint2 = await R.retrieve_with_meta("宇宙弦理论与量子引力统一", lot_id=LOT_ID, bid_id=bid_id)
    check("无关 query → NO_EVIDENCE", hint2 == DegradationHint.NO_EVIDENCE, f"hint={hint2}")

    # 未解析（无 chunks + bid_parsed=False）→ PARSING
    print("\n[未解析] 构造无 chunks 标书")
    res3, hint3 = await R.retrieve_with_meta("技术方案", lot_id="LOT-010", bid_id="BID-NOT-PARSED",
                                             bid_parsed=False)
    check("未解析 → PARSING 提示", hint3 == DegradationHint.PARSING, f"hint={hint3}")

    # Milvus 超时 → 降级关键词+结构化（monkeypatch 超时 0.001s）
    print("\n[降级] Milvus 语义检索超时")
    old_timeout = R.SEMANTIC_TIMEOUT_SECONDS
    R.SEMANTIC_TIMEOUT_SECONDS = 0.001
    try:
        res4, hint4 = await R.retrieve_with_meta(WORDS[0], lot_id=LOT_ID, bid_id=bid_id)
    finally:
        R.SEMANTIC_TIMEOUT_SECONDS = old_timeout
    check("超时 → SEMANTIC_DOWN 提示", hint4 == DegradationHint.SEMANTIC_DOWN, f"hint={hint4}")
    check("超时降级仍有结果（关键词路）", len(res4) > 0, f"results={len(res4)}")
    check("降级结果含关键词路来源", any(r.source == "keyword" for r in res4),
          str([r.source for r in res4]))

    # 清理
    await cleanup(engine)
    print("\n[cleanup] 验收残留已清理")
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
