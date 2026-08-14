"""P7.6 核心链路 SLA 压测（task.md P7.6，8 路径）。

每路径多次采样 → P50/P95（P50=排序中位；n≤5 时 P95 取最大值近似），
对照 SLA 表逐项 PASS/FAIL。超标只记录原因与优化方向，不改实现（P7.6 只测不调）。

数据隔离：临时数据 SLA- 前缀，压测后清理（幂等）；AI 评分/深度检测/登录复用
现有数据（LOT-007 三家、BID-BENCH-01、admin 账号）。API 直连容器 :8000。

前置：docker compose 全部 healthy（/health/ready 全 ok）；合成数据已导入；
压测前建议先跑 scripts/_clean_p76_data.py 清历史残留。

用法: poetry run python scripts/sla_p76.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark_p75"))  # bench_data

from app.ai.llm.deepseek_client import get_client  # noqa: E402
from app.ai.llm.prompts import build_score_prompt  # noqa: E402
from app.ai.rag.retriever import retrieve  # noqa: E402
from app.core import neo4j  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import session_factory  # noqa: E402
from app.core.milvus import get_collection  # noqa: E402
from app.core.minio_client import get_minio_client, remove_prefix  # noqa: E402
from app.models.lot_expert_assignment import LotExpertAssignment  # noqa: E402
from app.schemas.project import LotCreate  # noqa: E402
from app.services.expert_declaration_service import declare  # noqa: E402
from app.services.expert_match_service import match_experts  # noqa: E402
from app.services.fraud_detection_service import deep_detection  # noqa: E402
from app.services.project_service import create_lot, create_project  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

import bench_data as B  # noqa: E402

BASE = "http://localhost:8000/api/v1"

# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "Smart@2026")

# ==================== SLA 表（task.md P7.6）====================
SLA = {
    "① 标书解析": {"n": 3, "p50": 60.0, "p95": 180.0, "unit": "s"},
    "② 专家匹配": {"n": 5, "p50": 1.0, "p95": 3.0, "unit": "s"},
    "③ 回避申报": {"n": 5, "p50": 0.5, "p95": 2.0, "unit": "s"},
    "④ AI评分首token": {"n": 5, "p50": 3.0, "p95": 8.0, "unit": "s"},
    "⑤ AI评分完整流": {"n": 5, "p50": 12.0, "p95": 20.0, "unit": "s"},
    "⑥ 围串标深度检测": {"n": 5, "p50": 8.0, "p95": 30.0, "unit": "s"},
    "⑦ 登录→JWT": {"n": 5, "p50": 0.2, "p95": 0.5, "unit": "s"},
    "⑧ Outbox同步延迟": {"n": 3, "p50": 1.0, "p95": 5.0, "unit": "s"},
}

# 路径① 上传组合：LOT-010（BIDDING），3 个未投标且非黑名单供应商（重复投标 409 限制）
PARSE_LOT = "LOT-010"
PARSE_SUPS = ["SUP-004", "SUP-009", "SUP-010"]

# 路径② 候选：华中 ACTIVE 专家标签并集（6 候选，合成数据该 region 上限）
HUAZHONG_TAGS = [
    "安防监控", "电子政务", "教育信息化", "大数据", "智慧城市",
    "医疗信息化", "软件开发", "网络安全", "物联网", "系统集成",
]

TOTAL = {"pass": 0, "fail": 0}


def _percentile(sorted_samples: list[float], q: float) -> float:
    """线性插值百分位（sorted）。n 小时 q=0.95 用最大值近似由调用方决定。"""
    if not sorted_samples:
        return 0.0
    n = len(sorted_samples)
    if q >= 0.95 and n <= 5:
        return sorted_samples[-1]  # n≤5 时 P95 以最大值近似
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * (pos - lo)


def report(name: str, samples: list[float], p50_sla: float, p95_sla: float, unit: str,
           note: str = "") -> None:
    """统计 P50/P95 并判定 SLA。"""
    s = sorted(samples)
    p50 = _percentile(s, 0.5)
    p95 = _percentile(s, 0.95)
    ok = p50 <= p50_sla and p95 <= p95_sla
    if ok:
        TOTAL["pass"] += 1
        mark = "PASS"
    else:
        TOTAL["fail"] += 1
        mark = "FAIL"
    print(
        f"[{mark}] {name} n={len(samples)} P50={p50:.2f}{unit} P95={p95:.2f}{unit} "
        f"(SLA ≤{p50_sla:g}{unit}/≤{p95_sla:g}{unit}) raw={[round(x, 2) for x in s]}"
        + (f" {note}" if note else "")
    )


# ==================== 标书解析（路径①）====================
def make_bid_pdf() -> bytes:
    """reportlab 生成 8 章 ~3000 字合法中文 PDF（pdfplumber 可提取）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    chapters = [
        "第一章 公司概况",
        "第二章 技术方案",
        "第三章 安全方案",
        "第四章 实施计划",
        "第五章 项目团队",
        "第六章 质量与售后",
        "第七章 商务承诺",
        "第八章 项目管理",
    ]
    # 每章 3 个专业长句（~120 字/句），拼出 ~3000 字
    body = {
        "第一章 公司概况": (
            "我方成立于2008年，注册资本5000万元，是一家专业从事信息系统集成与软件研发的高新技术企业。"
            "公司拥有员工200余人，其中技术研发人员占比超过六成，设有独立的研发中心和测试实验室。"
            "近三年累计承接同类信息化项目40余个，覆盖政务、教育、医疗等多个行业，交付质量与客户口碑良好。",
        ),
        "第二章 技术方案": (
            "本方案基于微服务架构与容器化部署，采用前后端分离设计，具备高可用、高并发与弹性扩展能力。"
            "核心业务模块涵盖数据采集、智能分析与流程审批，系统通过API网关统一鉴权与限流，保障接口安全。"
            "技术选型上使用主流开源中间件，支持国产化硬件与操作系统适配，满足信创要求并预留平滑升级路径。",
        ),
        "第三章 安全方案": (
            "安全设计遵循等级保护三级要求，从网络安全、主机安全、应用安全与数据安全四个层面整体防护。"
            "网络层部署防火墙与入侵检测，主机层加固操作系统并配置防病毒与补丁管理，应用层实施最小权限与审计。"
            "数据层对敏感信息加密存储，建立备份与容灾机制，关键数据每日全量备份、实时增量同步。",
        ),
        "第四章 实施计划": (
            "项目实施划分为需求调研、系统设计、开发实现、测试验收与上线试运行五个阶段，总工期180天。"
            "首月完成现场调研与需求确认，第二至四个月完成核心模块开发，第五个月进入联调测试，末月试运行验收。"
            "我方派驻专职项目经理统筹进度，每周向甲方汇报里程碑与风险，确保按时保质交付。",
        ),
        "第五章 项目团队": (
            "本项目配置专职团队35人，其中项目经理1名（PMP认证，十年以上经验），技术架构师2名，开发人员18名。"
            "测试团队6人独立于开发，实施与运维人员8名，关键岗位均有同类项目交付经验。"
            "团队成员全员签订保密协议，项目期间保持稳定，如有人员变动提前报甲方审批并完成交接。",
        ),
        "第六章 质量与售后": (
            "我方已通过CMMI3质量管理体系认证，项目全过程按质量手册与作业指导书执行，分阶段质量评审。"
            "系统验收后提供36个月免费质保，质保期内免费修复缺陷并持续优化，响应时效为紧急故障2小时到场。"
            "质保期后按成本价提供运维服务，常设服务热线与远程支持，每年提供两次现场巡检与培训。",
        ),
        "第七章 商务承诺": (
            "本项目投标总报价：2,980,000元，价格包含全部软硬件、实施、培训与三年质保费用，无隐性收费。"
            "我方承诺严格履行合同约定的各项条款，如因我方原因造成项目延误，愿按合同约定承担违约责任。"
            "付款方式、发票开具与知识产权归属均按招标文件要求执行，确保双方权益清晰合规。",
        ),
        "第八章 项目管理": (
            "项目管理采用规范化流程，覆盖范围、进度、成本、质量、风险与沟通六大领域，使用项目管理工具实时跟踪。"
            "建立周例会、里程碑评审与变更控制机制，所有变更经双方确认后书面执行，防止范围蔓延。"
            "项目文档全程留痕，验收时提交完整交付物清单，包括源代码、设计文档、测试报告与操作手册。",
        ),
    }
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 11)
    y = 780
    for title in chapters:
        if y < 80:
            c.showPage()
            c.setFont("STSong-Light", 11)
            y = 780
        c.setFont("STSong-Light", 14)
        c.drawString(50, y, title)
        y -= 24
        c.setFont("STSong-Light", 11)
        for sent in body[title]:
            # 逐段换行（每行 ~38 字）
            cur = ""
            for ch in sent:
                cur += ch
                if len(cur) >= 38:
                    c.drawString(50, y, cur)
                    y -= 18
                    cur = ""
            if cur:
                c.drawString(50, y, cur)
                y -= 18
        y -= 8
    c.save()
    return buf.getvalue()


async def _api_parse(engine, bid_ids: list[str], supplier_id: str) -> float:
    """单次：上传 8 章 PDF → 轮询 PARSED，返回解析耗时（s）。上传成功即记录 bid_id。"""
    token = await _login("admin", TEST_PASSWORD)
    files = {"file": ("sla_bid.pdf", make_bid_pdf(), "application/pdf")}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/lots/{PARSE_LOT}/bids",
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data={"supplier_id": supplier_id},
        )
    if r.status_code != 201:
        raise RuntimeError(f"上传失败 {r.status_code}: {r.text[:200]}")
    bid_id = r.json()["bid_id"]
    bid_ids.append(bid_id)  # 先记录再计时：后续任何异常 finally 也能清理
    t0 = time.perf_counter()
    while True:
        async with httpx.AsyncClient(timeout=30.0) as client:
            st = (await client.get(
                f"{BASE}/bids/{bid_id}/status",
                headers={"Authorization": f"Bearer {token}"},
            )).json()
        status = st["status"]
        if status == "PARSED":
            break
        if status == "PARSE_FAILED":
            raise RuntimeError(f"解析失败: {bid_id} {st}")
        if time.perf_counter() - t0 > 240:
            raise TimeoutError(f"解析超时 240s: {bid_id} status={status}")
        await asyncio.sleep(3)
    return time.perf_counter() - t0


async def bench_parse() -> None:
    """路径①：标书解析。3 次采样。"""
    engine = await _db()
    samples: list[float] = []
    bid_ids: list[str] = []
    try:
        for i in range(SLA["① 标书解析"]["n"]):
            print(f"  [①] 第 {i + 1} 次上传解析（{PARSE_SUPS[i]}）...")
            samples.append(await _api_parse(engine, bid_ids, PARSE_SUPS[i]))
    finally:
        await _clean_parse(engine, bid_ids)
    report("① 标书解析", samples, 60.0, 180.0, "s",
           note=f"(上传后至 PARSED，n={SLA['① 标书解析']['n']}，lot={PARSE_LOT}+{PARSE_SUPS})")


async def _clean_parse(engine, bid_ids: list[str]) -> None:
    """路径① 清理：MySQL 行 + Milvus chunks + MinIO 对象。"""
    if bid_ids:
        async with engine.begin() as conn:
            for b in bid_ids:
                await conn.execute(text("DELETE FROM bid_document WHERE bid_id=:b"), {"b": b})

        def _del_milvus(ids: list[str]) -> None:
            collection = get_collection()
            for b in ids:
                try:
                    collection.delete(f'bid_id == "{b}"')
                except Exception:  # noqa: BLE001
                    pass
            collection.flush()

        await asyncio.to_thread(_del_milvus, bid_ids)
    client = get_minio_client()
    remove_prefix(client, f"bids/{PARSE_LOT}/")


# ==================== 专家匹配（路径②）====================
async def bench_match() -> None:
    """路径②：专家匹配（隔离项目+标段+3 标书）。5 次采样。"""
    engine = await _db()
    samples: list[float] = []
    try:
        for i in range(SLA["② 专家匹配"]["n"]):
            prj_code = f"SLA-PRJ-M{i + 1}"
            lot_code = f"SLA-LOT-M{i + 1}"
            # bid_id 带运行随机后缀，彻底规避任何历史残留主键冲突（前缀 SLA-BID- 保留供清理）
            run_salt = uuid4().hex[:8]
            bid_ids = [f"SLA-BID-{run_salt}-{j}" for j in (1, 2, 3)]
            try:
                async with session_factory() as s:
                    project = await create_project(
                        s,
                        data=_project_create(prj_code),
                        operator_id="sla-p76",
                    )
                    lot = await create_lot(
                        s, project.project_id,
                        data=LotCreate(
                            lot_code=lot_code, name="SLA 压测标段", budget=Decimal("1000000"),
                        ),
                    )
                    # 用 ORM 改状态（text UPDATE 不刷 identity map，session.get 会拿缓存 BIDDING）
                    lot.status = "UNDER_REVIEW"
                    for j, b in enumerate(bid_ids, start=1):
                        await s.execute(
                            text("INSERT INTO bid_document (bid_id, lot_id, supplier_id, status, "
                                 "bid_amount, created_at, updated_at) "
                                 "VALUES (:bid, :lid, :sup, 'PARSED', :amt, NOW(), NOW())"),
                            {"bid": b, "lid": lot.lot_id, "sup": f"SLA-SUP-M{i + 1}-{j}",
                             "amt": Decimal("1000000") + Decimal(j) * Decimal("100000")},
                        )
                    await s.commit()
                    t0 = time.perf_counter()
                    res = await match_experts(
                        s, lot_id=lot.lot_id, tags=HUAZHONG_TAGS, operator_id="sla-p76",
                    )
                    samples.append(time.perf_counter() - t0)
                    candidates = len(res["assigned"])
                    print(f"  [②] 第 {i + 1} 次匹配 落库 {candidates} 位 "
                          f"(insufficient={res['insufficient']})")
                # 精确清理（project 级联删其下 lot/bids/assignment）
                await _clean_sla_project(engine, project.project_id, None)
            except Exception:  # noqa: BLE001  单次失败也清理再抛出，避免残留
                await _clean_sla_project(engine, project.project_id, None)
                raise
    finally:
        # 兜底清理：清掉该批 SLA- 前缀项目残留（异常中断时）
        await _clean_sla_all(engine)
    report("② 专家匹配", samples, 1.0, 3.0, "s",
           note="(候选=华中6专家，合成数据该region上限，'15候选'不可达)")


def _in_clause(ids: list[str]) -> str:
    """构造 SQL IN 子句（避免 text() expanding 参数在 asyncmy 下的不确定行为）。"""
    return ",".join(f"'{x}'" for x in ids)


async def _clean_sla_all(engine) -> None:
    """全量清理所有 SLA 压测数据（幂等）：按 bid_id/lot_code 前缀直接删，不依赖 project 存在。

    覆盖孤儿数据（project 已删但 lot/bid 残留的中间态）：lot_id 为随机 LOT-xxx，
    bid_id 为固定 SLA-BID-M1-1 格式，故 bid 按 bid_id 前缀删、lot 按 lot_code 前缀删。
    """
    async with engine.begin() as conn:
        prj_ids = [r[0] for r in (await conn.execute(
            text("SELECT project_id FROM project WHERE project_code LIKE :p"), {"p": "SLA-PRJ%"})
        ).all()]
        lot_ids = [r[0] for r in (await conn.execute(
            text("SELECT lot_id FROM lot WHERE lot_code LIKE :p"), {"p": "SLA-LOT%"})
        ).all()]
        await conn.execute(text("DELETE FROM lot_expert_assignment WHERE lot_id LIKE :p"),
                           {"p": "SLA-LOT%"})
        await conn.execute(text("DELETE FROM bid_document WHERE bid_id LIKE :p"),
                           {"p": "SLA-BID%"})
        if lot_ids:
            await conn.execute(
                text(f"DELETE FROM bid_document WHERE lot_id IN ({_in_clause(lot_ids)})"))
            await conn.execute(
                text(f"DELETE FROM lot_expert_assignment WHERE lot_id IN ({_in_clause(lot_ids)})"))
            await conn.execute(text(f"DELETE FROM lot WHERE lot_id IN ({_in_clause(lot_ids)})"))
            await conn.execute(
                text(f"DELETE FROM outbox_event WHERE aggregate_id IN ({_in_clause(lot_ids)})"))
        if prj_ids:
            await conn.execute(
                text(f"DELETE FROM project WHERE project_id IN ({_in_clause(prj_ids)})"))
            await conn.execute(
                text(f"DELETE FROM outbox_event WHERE aggregate_id IN ({_in_clause(prj_ids)})"))
    if lot_ids or prj_ids:
        driver = neo4j.get_driver()
        async with driver.session() as s:
            for lid in lot_ids:
                await s.run("MATCH (n:Lot {lotId:$id}) DETACH DELETE n", id=lid)
            for pid in prj_ids:
                await s.run(
                    "MATCH (n:ProcurementProject {projectId:$id}) DETACH DELETE n", id=pid)


async def _clean_sla_project(engine, project_id: str | None = None, lot_id: str | None = None,
                             prefix: str | None = None) -> None:
    """SLA 压测数据清理：子表→lot→project→outbox→Neo4j。

    project 关联的 lot（随机 LOT-xxx ID）一并连删；prefix 时按 project_code 前缀
    反查 project，再级联其下 lot。幂等可重跑。
    """
    prj_ids: list[str] = []
    async with engine.begin() as conn:
        if prefix:
            prj_ids = [r[0] for r in (await conn.execute(
                text("SELECT project_id FROM project WHERE project_code LIKE :p"),
                {"p": f"{prefix}PRJ%"},
            )).all()]
        elif project_id:
            prj_ids = [project_id]
        lot_ids: list[str] = []
        if prj_ids:
            for pid in prj_ids:
                lot_ids += [r[0] for r in (await conn.execute(
                    text("SELECT lot_id FROM lot WHERE project_id=:p"), {"p": pid}
                )).all()]
        if lot_id and lot_id not in lot_ids:
            lot_ids.append(lot_id)
        for lid in lot_ids:
            await conn.execute(
                text("DELETE FROM lot_expert_assignment WHERE lot_id=:lid"), {"lid": lid})
            await conn.execute(
                text("DELETE FROM bid_document WHERE lot_id=:lid"), {"lid": lid})
            await conn.execute(
                text("DELETE FROM lot WHERE lot_id=:lid"), {"lid": lid})
            await conn.execute(
                text("DELETE FROM outbox_event WHERE aggregate_id=:id"), {"id": lid})
        for pid in prj_ids:
            await conn.execute(
                text("DELETE FROM project WHERE project_id=:id"), {"id": pid})
            await conn.execute(
                text("DELETE FROM outbox_event WHERE aggregate_id=:id"), {"id": pid})
    if lot_ids or prj_ids:
        driver = neo4j.get_driver()
        async with driver.session() as s:
            for lid in lot_ids:
                await s.run("MATCH (n:Lot {lotId:$id}) DETACH DELETE n", id=lid)
            for pid in prj_ids:
                await s.run(
                    "MATCH (n:ProcurementProject {projectId:$id}) DETACH DELETE n", id=pid)


def _project_create(project_code: str):
    from app.schemas.project import ProjectCreate

    return ProjectCreate(
        project_code=project_code,
        name="SLA 压测项目",
        type="SERVICE",
        region="华中",
        budget=Decimal("1000000"),
    )


# ==================== 回避申报（路径③）====================
async def bench_declare() -> None:
    """路径③：回避申报（隔离 assignment，无冲突路径）。5 次采样。"""
    engine = await _db()
    samples: list[float] = []
    for i in range(SLA["③ 回避申报"]["n"]):
        lot_id = f"SLA-LOT-D{i + 1}"
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO lot_expert_assignment (lot_id, expert_id, dimension_ids, status, assigned_at) "
                     "VALUES (:lid, 'EXP-001', :dims, 'PENDING_DECLARATION', NOW())"),
                {"lid": lot_id, "dims": json.dumps(["DIM-TECH"])},
            )
        async with session_factory() as s:
            assignment = (await s.scalars(
                select(LotExpertAssignment).where(LotExpertAssignment.lot_id == lot_id)
            )).one()
            t0 = time.perf_counter()
            res = await declare(
                s,
                assignment_id=assignment.id,
                expert_id="EXP-001",
                confirmations=[{"supplier_id": "SLA-SUP-D", "has_conflict": False}],
            )
            samples.append(time.perf_counter() - t0)
        print(f"  [③] 第 {i + 1} 次申报 → {res['status']}")
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM lot_expert_assignment WHERE lot_id=:lid"), {"lid": lot_id})
            await conn.execute(
                text("DELETE FROM notification WHERE related_id=:lid"), {"lid": lot_id})
    report("③ 回避申报", samples, 0.5, 2.0, "s", note="(无冲突路径 IN_PROGRESS+通知)")


# ==================== AI 评分（路径④⑤）====================
async def bench_ai_stream() -> None:
    """路径④⑤：AI 评分首 token / 完整流（预置 chunks，排除检索变量）。5 次采样。"""
    client = get_client()
    # 预置检索一次（不计时），取 BID-BENCH-01 技术方案 top-8
    results = await retrieve(
        B.DIMENSION_QUERIES["TECH"][0], lot_id=B.BENCH_LOT_ID, bid_id=B.BENCH_BID_IDS[0], top_k=8
    )
    chunks = [r.content for r in results]
    rubric = "\n".join(
        f"- {n}（{d}）：{r}"
        for n, d, r in (B.DIM_CRITERIA["TECH"][:2])
    )
    prompt = build_score_prompt(
        dimension_name="技术方案", max_score=30.0, rubric=rubric, chunks=chunks,
    )
    print(f"  [④⑤] 预置 chunks={len(chunks)} 段（BID-BENCH-01 技术方案）")
    firsts: list[float] = []
    fulls: list[float] = []
    for i in range(SLA["④ AI评分首token"]["n"]):
        t0 = time.perf_counter()
        t_first: float | None = None
        t_full: float | None = None
        acc = []
        async for delta in client.chat_stream(prompt, max_tokens=2048):
            if t_first is None:
                t_first = time.perf_counter() - t0
            acc.append(delta)
        t_full = time.perf_counter() - t0
        firsts.append(t_first)
        fulls.append(t_full)
        print(f"  [④⑤] 第 {i + 1} 次 首token={t_first:.2f}s 完整={t_full:.2f}s 字数={sum(len(x) for x in acc)}")
    report("④ AI评分首token", firsts, 3.0, 8.0, "s")
    report("⑤ AI评分完整流", fulls, 12.0, 20.0, "s", note="(真实 DeepSeek, max_tokens=2048)")


# ==================== 围串标深度检测（路径⑥）====================
async def bench_fraud() -> None:
    """路径⑥：围串标深度检测（LOT-007 三家，纯函数无副作用）。5 次采样。"""
    engine = await _db()
    async with engine.connect() as conn:
        bid_ids = [r[0] for r in (await conn.execute(text(
            "SELECT bid_id FROM bid_document WHERE lot_id='LOT-007' "
            "AND status IN ('PARSED','FROZEN') ORDER BY bid_id"
        ))).all()]
    print(f"  [⑥] LOT-007 标书: {bid_ids}")
    samples: list[float] = []
    for i in range(SLA["⑥ 围串标深度检测"]["n"]):
        t0 = time.perf_counter()
        res = await deep_detection(lot_id="LOT-007", bid_ids=bid_ids)
        samples.append(time.perf_counter() - t0)
        print(f"  [⑥] 第 {i + 1} 次 risk={res.get('risk')} total={res.get('total_score')}")
    await engine.dispose()
    report("⑥ 围串标深度检测", samples, 8.0, 30.0, "s")


# ==================== 登录（路径⑦）====================
async def _login(username: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    if r.status_code != 200:
        raise RuntimeError(f"登录失败 {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


async def bench_login() -> None:
    """路径⑦：登录→JWT。5 次采样，复用同一 httpx 客户端（贴近真实浏览器复用连接）。"""
    samples: list[float] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i in range(SLA["⑦ 登录→JWT"]["n"]):
            t0 = time.perf_counter()
            r = await client.post(
                f"{BASE}/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
            if r.status_code != 200:
                raise RuntimeError(f"登录失败 {r.status_code}: {r.text[:200]}")
            samples.append(time.perf_counter() - t0)
    print(f"  [⑦] 登录 {len(samples)} 次 token 获取成功")
    report("⑦ 登录→JWT", samples, 0.2, 0.5, "s",
           note="(复用连接；bcrypt cost=10，P7.6 优化后)")


# ==================== Outbox 同步（路径⑧）====================
async def bench_outbox() -> None:
    """路径⑧：Outbox 同步延迟（create_project 含直同步 Neo4j，同步路径≈API 耗时）。3 次采样。"""
    engine = await _db()
    samples: list[float] = []
    try:
        for i in range(SLA["⑧ Outbox同步延迟"]["n"]):
            prj_code = f"SLA-PRJ-O{i + 1}"
            async with session_factory() as s:
                t0 = time.perf_counter()
                project = await create_project(
                    s, data=_project_create(prj_code), operator_id="sla-p76",
                )
                samples.append(time.perf_counter() - t0)
                print(f"  [⑧] 第 {i + 1} 次 create_project → {project.project_id}")
            await _clean_sla_project(engine, project.project_id, None)
    finally:
        await _clean_sla_all(engine)
    report("⑧ Outbox同步延迟", samples, 1.0, 5.0, "s",
           note="(MySQL 事务+outbox 写入+Neo4j 直同步)")


async def _db():
    return create_async_engine(settings.database_url)


async def _run_path(name: str, coro) -> None:
    """单路径隔离执行：异常记 FAIL 不中断其他路径。"""
    try:
        await coro
    except Exception as e:  # noqa: BLE001  压测脚本需展示全部路径结果
        TOTAL["fail"] += 1
        print(f"[ERROR] {name} 执行异常: {type(e).__name__}: {e}")


async def main() -> None:
    print("========== P7.6 核心链路 SLA 压测（容器 :8000，DEEPSEEK_ENABLED=true）==========")
    # 幂等：开头清上次压测 SLA- 前缀残留（项目/标段/标书/分配/outbox/Neo4j）
    engine = await _db()
    await _clean_sla_project(engine, None, None, prefix="SLA-")
    await engine.dispose()
    await _run_path("① 标书解析", bench_parse())
    print()
    await _run_path("② 专家匹配", bench_match())
    print()
    await _run_path("③ 回避申报", bench_declare())
    print()
    await _run_path("④⑤ AI 评分", bench_ai_stream())
    print()
    await _run_path("⑥ 围串标深度检测", bench_fraud())
    print()
    await _run_path("⑦ 登录→JWT", bench_login())
    print()
    await _run_path("⑧ Outbox 同步延迟", bench_outbox())
    print()
    print(f"========== P7.6 压测结果: PASS={TOTAL['pass']} FAIL={TOTAL['fail']} ==========")
    sys.exit(0 if TOTAL["fail"] == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
