"""标书异步解析流水线（P2.1）。

arq job：7 步 checkpoint 流水线，每步完成后 UPDATE `bid_document.parsing_step`
（进度观测 + 僵尸扫描 + 失败定位依据）。全部步骤幂等，重跑安全
（Milvus 先删后插 / Neo4j MERGE / MySQL 直接更新）。

步骤（task.md P2.1）：
  step1 parsing_step=1  提取全文（pdfplumber/python-docx，DOCX 压缩炸弹防御）
  step2 parsing_step=2  规则提取报价/工期/人员 → MySQL 结构化字段
  step3 parsing_step=3  SmartDocumentChunker 标题感知分块（500-1000 tokens, overlap 100）
  step4 parsing_step=4  BGE-M3 Embedding（asyncio.to_thread 卸载）
  step5 parsing_step=5  Milvus 批量入库（先删后插，幂等）
  step6 parsing_step=6  Neo4j 同步 BidDocument + BELONGS_TO/SUBMITTED_BY
  step7 parsing_step=NULL, status=PARSED

重试语义：job 内自管重试（首次 + doc_parse_max_retries 次，每次间隔
doc_parse_retry_delay_seconds），耗尽置 PARSE_FAILED——不依赖 arq 内部
retry 状态，PARSE_FAILED 标记确定发生在最后一次尝试后。

僵尸扫描（scan_zombie_parsing）：PARSING + parsing_step>0 + updated_at 超过
doc_zombie_timeout_minutes（默认 30）→ PARSE_FAILED。worker cron 每分钟兜底。
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from datetime import datetime, timezone

import structlog
from sqlalchemy import text, update

from app.core.database import session_factory
from app.core.minio_client import download_object, get_minio_client
from app.models.bid_document import BidDocument, BidStatus

logger = structlog.get_logger(__name__)

# ==================== 常量与配置 ====================

# DOCX 压缩炸弹防御（task.md P2.1：解压比≤100:1，解压上限 200MB）
DOCX_MAX_RATIO = 100
DOCX_MAX_UNCOMPRESSED = 200 * 1024 * 1024

# BGE-M3 固定维度（Milvus schema dim=1024）
EMBEDDING_DIM = 1024

# 步骤 → parsing_step 值（step7 完成时置 NULL）
STEP_EXTRACT = 1
STEP_STRUCTURE = 2
STEP_CHUNK = 3
STEP_EMBED = 4
STEP_MILVUS = 5
STEP_NEO4J = 6

# 结构化提取正则（对齐真实标书常见措辞；验收仿真标书内容同规则可命中）
_RE_AMOUNT = re.compile(r"(?:投标总报价|投标报价|报价|总价)\s*[:：]?\s*([0-9][0-9,，]*(?:\.\d+)?)\s*元")
_RE_DURATION = re.compile(r"(?:计划工期|工期|建设工期)\s*[:：]?\s*(\d{1,4})\s*(?:个)?(?:日历)?天")
_RE_TEAM = re.compile(r"(?:项目团队|项目组|拟投入人员|投入人员|项目人员|人员配置|团队)\s*(?:[:：]|共|为|约|配备)?\s*(\d{1,4})\s*人")
_RE_QUALITY_CERT = re.compile(r"(ISO9001|ISO27001|ISO20000|CMMI5|CMMI4|CMMI3|ISO14001|GB/T\s*19001)")
_RE_WARRANTY = re.compile(r"(?:质保期|保修期|售后服务期|免费服务期)\s*[:：]?\s*(\d{1,3})\s*(?:个)?月")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NonRetryableParseError(ValueError):
    """确定性解析错误：重试不会成功（文件非法/压缩炸弹/全文为空），直接标记失败。

    与之相对的可重试错误（Milvus/Neo4j/embedding 瞬时故障）走 job 内重试——
    根因不同，重试策略不同，避免对坏文件空耗 3 次 × 60s。
    """


def _detect_type(content: bytes) -> str:
    """按 magic bytes 识别 pdf/docx。未知抛 ValueError（上传层已校验，此处兜底）。"""
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"PK\x03\x04"):
        return "docx"
    raise ValueError("仅支持 PDF/DOCX（magic bytes 校验失败）")


# ==================== Step 1：提取全文 ====================

def _check_docx_bomb(content: bytes) -> None:
    """DOCX 压缩炸弹防御：逐 entry 校验解压比与总解压大小（python-docx 解压前调用）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            total = 0
            for info in zf.infolist():
                if info.file_size > DOCX_MAX_UNCOMPRESSED:
                    raise ValueError(f"DOCX 单文件解压超限 {info.file_size} > {DOCX_MAX_UNCOMPRESSED}")
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > DOCX_MAX_RATIO:
                    raise ValueError(f"DOCX 压缩比异常 {ratio:.0f}:1 超限 {DOCX_MAX_RATIO}:1（疑似压缩炸弹）")
                total += info.file_size
                if total > DOCX_MAX_UNCOMPRESSED:
                    raise ValueError(f"DOCX 总解压超限 {total} > {DOCX_MAX_UNCOMPRESSED}")
    except zipfile.BadZipFile as e:
        raise ValueError(f"DOCX 不是合法 zip: {e}") from e


def _extract_text(content: bytes, kind: str) -> str:
    """提取全文。PDF 按页拼合；DOCX 先过压缩炸弹防御再解析。"""
    if kind == "pdf":
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
    _check_docx_bomb(content)
    import docx as _docx

    document = _docx.Document(io.BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs).strip()


# ==================== Step 2：结构化字段提取 ====================

def _first_group(pat: re.Pattern[str], text: str):
    """返回正则首个匹配组（去逗号/空白归一），无匹配返回 None。"""
    m = pat.search(text)
    if not m:
        return None
    return m.group(1).replace(",", "").replace("，", "").strip()


def _extract_structured_fields(text: str) -> dict:
    """规则提取报价/工期/人员/质量认证/质保，附原文片段供溯源审计。

    返回 {bid_amount, duration, team_size, structured_data}。规则未命中字段
    置 None（真实标书措辞多变，缺失不阻断解析，留人工补录）。
    """
    structured: dict[str, object] = {}
    if cert := _RE_QUALITY_CERT.search(text):
        structured["quality_cert"] = cert.group(1).replace(" ", "")
    if warranty := _RE_WARRANTY.search(text):
        structured["warranty_months"] = int(warranty.group(1))

    def _as_int(v: str | None):
        if v is None:
            return None
        try:
            return int(v)
        except ValueError:
            return None

    return {
        "bid_amount": _as_int(_first_group(_RE_AMOUNT, text)),
        "duration": _as_int(_first_group(_RE_DURATION, text)),
        "team_size": _as_int(_first_group(_RE_TEAM, text)),
        "structured_data": structured,
    }


# ==================== Step 5：Milvus 入库 ====================

def _insert_milvus(chunks, embeddings: list[list[float]], bid_id: str) -> int:
    """批量入库。先按 bid_id 删旧 chunks（幂等）再插入，flush 后立即可检索。"""
    from app.core.milvus import get_collection

    collection = get_collection()
    # 清理历史（重试/重新解析时旧 chunks 仍存在，先删后插保证无残留）
    try:
        collection.delete(f'bid_id == "{bid_id}"')
    except Exception:  # noqa: BLE001  首次解析无旧数据，delete 空表达式部分版本抛错，容忍
        pass
    data = [
        [c.chunk_id for c in chunks],
        [c.bid_id for c in chunks],
        [c.lot_id for c in chunks],
        [c.content for c in chunks],
        embeddings,
        [c.chapter_title for c in chunks],
        [c.page_no for c in chunks],
        [c.chunk_index for c in chunks],
        [c.source_file for c in chunks],
    ]
    collection.insert(data)
    collection.flush()
    return len(chunks)


# ==================== 流水线主体 ====================

async def _set_step(bid_id: str, step: int | None) -> None:
    """独立事务 UPDATE parsing_step（checkpoint 持久化）。step=None 表示完成。"""
    async with session_factory() as session:
        await session.execute(
            update(BidDocument)
            .where(BidDocument.bid_id == bid_id)
            .values(parsing_step=step, updated_at=_now())
        )
        await session.commit()


async def _run_pipeline(bid_id: str) -> None:
    """7 步解析。任一步失败抛异常（document_ingest 上层重试）。"""
    # ---------- 读取 + 状态机 ----------
    async with session_factory() as session:
        bid = await session.get(BidDocument, bid_id)
        if bid is None:
            raise ValueError(f"标书不存在: {bid_id}")
        if bid.status in (BidStatus.FROZEN, BidStatus.PARSED, BidStatus.DISQUALIFIED):
            logger.info("bid.parse_skip", bid_id=bid_id, status=bid.status)
            return
        if bid.status == BidStatus.SUBMITTED:
            bid.status = BidStatus.PARSING
            bid.updated_at = _now()
            await session.commit()
        lot_id, supplier_id, file_url = bid.lot_id, bid.supplier_id, bid.file_url or ""
    if not file_url:
        raise ValueError(f"标书缺少文件对象: {bid_id}")

    # ---------- Step 1：提取全文 ----------
    try:
        content = await asyncio.to_thread(download_object, get_minio_client(), file_url)
        kind = _detect_type(content)
        text = await asyncio.to_thread(_extract_text, content, kind)
    except ValueError as e:
        # 非法文件/压缩炸弹等确定性错误：重试无意义，直接标记失败
        raise NonRetryableParseError(str(e)) from e
    if not text:
        raise NonRetryableParseError(f"提取全文为空（可能为扫描件）: {bid_id}")
    await _set_step(bid_id, STEP_EXTRACT)
    logger.info("bid.parse_step1", bid_id=bid_id, kind=kind, chars=len(text))

    # ---------- Step 2：结构化字段 → MySQL ----------
    fields = _extract_structured_fields(text)
    async with session_factory() as session:
        await session.execute(
            update(BidDocument)
            .where(BidDocument.bid_id == bid_id)
            .values(
                bid_amount=fields["bid_amount"],
                duration=fields["duration"],
                team_size=fields["team_size"],
                structured_data=fields["structured_data"],
                updated_at=_now(),
            )
        )
        await session.commit()
    await _set_step(bid_id, STEP_STRUCTURE)
    logger.info("bid.parse_step2", bid_id=bid_id, **fields)

    # ---------- Step 3：分块 ----------
    from app.ai.rag.chunker import SmartDocumentChunker

    chunks = SmartDocumentChunker().chunk(
        text, bid_id=bid_id, lot_id=lot_id, source_file=file_url
    )
    if not chunks:
        raise ValueError(f"分块结果为空: {bid_id}")
    await _set_step(bid_id, STEP_CHUNK)
    logger.info("bid.parse_step3", bid_id=bid_id, chunks=len(chunks))

    # ---------- Step 4：BGE-M3 Embedding ----------
    from app.ai.rag.embedder import get_embedder

    vectors = await get_embedder().embed([c.content for c in chunks])
    if len(vectors) != len(chunks):
        raise ValueError(f"向量数量不匹配: {len(vectors)} vs {len(chunks)}")
    if vectors and len(vectors[0]) != EMBEDDING_DIM:
        raise ValueError(f"向量维度异常: {len(vectors[0])} != {EMBEDDING_DIM}")
    await _set_step(bid_id, STEP_EMBED)

    # ---------- Step 5：Milvus 批量入库 ----------
    await asyncio.to_thread(_insert_milvus, chunks, vectors, bid_id)
    await _set_step(bid_id, STEP_MILVUS)
    logger.info("bid.parse_step5", bid_id=bid_id, chunks=len(chunks))

    # ---------- Step 6：Neo4j 节点 + 关系 ----------
    from app.services import neo4j_sync

    await neo4j_sync.upsert_bid(
        bid_id,
        lot_id,
        supplier_id,
        bid_amount=fields["bid_amount"],
        status=BidStatus.PARSING,
    )
    await _set_step(bid_id, STEP_NEO4J)

    # ---------- Step 7：收尾（PARSED + parsing_step=NULL） ----------
    async with session_factory() as session:
        await session.execute(
            update(BidDocument)
            .where(BidDocument.bid_id == bid_id)
            .values(status=BidStatus.PARSED, parsing_step=None, updated_at=_now())
        )
        await session.commit()
    logger.info("bid.parse_step7", bid_id=bid_id, status=BidStatus.PARSED)


async def _mark_parse_failed(bid_id: str) -> None:
    """重试耗尽 → PARSE_FAILED（仅 PARSING 状态标记，已成功的跳过）。"""
    async with session_factory() as session:
        bid = await session.get(BidDocument, bid_id)
        if bid is None or bid.status != BidStatus.PARSING:
            return
        bid.status = BidStatus.PARSE_FAILED
        bid.updated_at = _now()
        await session.commit()


async def document_ingest(ctx: dict, bid_id: str) -> str:
    """arq job：解析标书。首次 + doc_parse_max_retries 次重试，耗尽 PARSE_FAILED。

    调用方：上传/retry-parse 后 enqueue（app/tasks/dispatch.py）。
    返回终态：'PARSED' 或 'PARSE_FAILED'。
    """
    from app.core.config import settings

    max_retries = settings.doc_parse_max_retries
    retry_delay = settings.doc_parse_retry_delay_seconds
    for attempt in range(max_retries + 1):
        try:
            await _run_pipeline(bid_id)
            logger.info("bid.parse_done", bid_id=bid_id, attempts=attempt + 1)
            return "PARSED"
        except NonRetryableParseError as e:
            # 确定性错误：立即失败，不走重试
            await _mark_parse_failed(bid_id)
            logger.warning("bid.parse_rejected", bid_id=bid_id, error=str(e))
            return "PARSE_FAILED"
        except Exception as e:  # noqa: BLE001
            logger.warning("bid.parse_retry", bid_id=bid_id, attempt=attempt, error=str(e))
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
    await _mark_parse_failed(bid_id)
    logger.error("bid.parse_giveup", bid_id=bid_id, max_retries=max_retries)
    return "PARSE_FAILED"


# ==================== 僵尸扫描（worker cron） ====================

async def scan_zombie_parsing(ctx: dict) -> int:
    """PARSING + parsing_step>0 + updated_at 超时 → PARSE_FAILED。返回处理条数。

    worker cron 每分钟兜底：解析 job 崩溃/worker 重启等导致 PARSING 悬挂时，
    超过 doc_zombie_timeout_minutes（默认 30）自动标记失败，PM 可 retry-parse。
    """
    from app.core.config import settings

    timeout_min = settings.doc_zombie_timeout_minutes
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT bid_id FROM bid_document "
                    "WHERE status=:st AND parsing_step>0 "
                    "AND updated_at < NOW() - INTERVAL :mins MINUTE"
                ),
                {"st": BidStatus.PARSING, "mins": timeout_min},
            )
        ).all()
        for r in rows:
            await session.execute(
                update(BidDocument)
                .where(BidDocument.bid_id == r.bid_id)
                .values(status=BidStatus.PARSE_FAILED, updated_at=_now())
            )
        await session.commit()
    if rows:
        logger.info("bid.zombie_marked", count=len(rows), timeout_minutes=timeout_min)
    return len(rows)
