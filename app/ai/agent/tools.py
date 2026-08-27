"""agent 工具定义与执行器（function calling，仅内部能力）。

good-question 二期 function calling 架构移植，smart-procurement 决策空间收敛为三个
内部能力（外部能力全由内部服务承载，无跨系统 API 调用）：
- retrieve_knowledge：问标书正文 → RAG 检索（唯一可能降级的外部依赖，error 兜底）
- get_dimension_rubric：问评分标准 → 查 ScoringCriterion（无 IO 风险，本地数据）
- get_bid_structured_info：问结构化数据（报价/资质/团队/工期）→ bid 字段 + structured_data

评分端点不 agent 化（必须检索 + 报价公式是确定性数学，无决策空间，方案评审结论）。
执行器返回 dict 直接作为 tool 消息 content（JSON 序列化，LLM 消费）；source_count 统一
作为"结果非空"判定字段（agent_loop._result_nonempty）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm.text_cleaner import clean_bid_text
from app.ai.rag.query_cleaner import clean_query
from app.ai.rag.retriever import retrieve_with_meta
from app.models.project import ScoringCriterion


@dataclass
class ToolContext:
    """工具执行上下文：agent 所需的会话级依赖（调用方 reviews.py 组装）。

    review 绑定 dimension + bid；bid 提供 lot_id/bid_id 与结构化字段。
    """

    session: AsyncSession
    review: object
    bid: object
    dimension: object | None = None
    # 最近 user 消息原文（倒序，最新在前）。回指意图归队用（_classify_intent 的
    # history 参数）：unknown 且命中回指词（"就这个/还有呢"）时延续上一轮意图。
    history: list[str] = field(default_factory=list)


# ──────────────────── 工具 schema（DeepSeek function calling 用） ────────────────────

RETRIEVE_KNOWLEDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "retrieve_knowledge",
        "description": (
            "在标书库中检索与用户问题相关的投标文件内容（技术方案、实施计划、"
            "保障措施等正文片段）。用户询问标书中的事实、细节、条款，或要求总结标书内容时，"
            "必须先调用本工具。工具返回 JSON：context（[来源N] 检索到的正文片段）、"
            "source_count（命中条数，0 表示未命中）、confidence_band（none/low/high 相关性置信度）、"
            "error（可选，检索服务不可用或降级时出现，含应对方式）。source_count=0 且问题与标书"
            "相关时如实告知用户未找到，不得编造；出现 error 字段时按 error 说明作答并注明可信度偏低，不得编造。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "用于检索的查询词。优先取用户问题的核心实体与关键限制条件"
                        "（技术方案、实施计划、保障措施等），去除寒暄客套，不要照抄整段对话，"
                        "通常 1-2 句。"
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

GET_DIMENSION_RUBRIC_TOOL = {
    "type": "function",
    "function": {
        "name": "get_dimension_rubric",
        "description": (
            "获取当前评审维度的评分标准（维度名、满分、评分项及分值、打分细则）。"
            "用户询问评审标准、评分规则、怎么打分、某评分项多少分、评分细则时调用。"
            "工具返回 JSON：dimension（维度名与满分）、criteria（评分项列表：name/max_score/rubric）、"
            "source_count（评分项条数）。当前评审维度已绑定，无需参数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

GET_BID_STRUCTURED_INFO_TOOL = {
    "type": "function",
    "function": {
        "name": "get_bid_structured_info",
        "description": (
            "获取投标文件的结构化信息（报价金额、工期、团队规模、资质证书等结构化字段）。"
            "用户询问价格、报价、金额、工期、团队、人员、资质、认证等数据型问题时调用。"
            "工具返回 JSON：fields（字段名→值，JSON 序列化）、source_count（字段条数）。"
            "field 缺省返回全部结构化字段。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": "可选。要查询的字段名（如 报价/工期/团队规模/资质）。缺省返回全部结构化字段。",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

# 供 chat_stream_agent 传入的完整 tools 列表（唯一事实源）
CHAT_TOOLS = [RETRIEVE_KNOWLEDGE_TOOL, GET_DIMENSION_RUBRIC_TOOL, GET_BID_STRUCTURED_INFO_TOOL]


# ──────────────────── 执行器 ────────────────────

async def execute_retrieve_knowledge(ctx: ToolContext, query: str) -> dict:
    """retrieve_knowledge：RAG 检索标书正文 → LLM 可消费 context + 检索质量元信息。

    与评分端点同一检索入口（retrieve_with_meta），dimension=None 走纯向量+关键词召回。
    区分两类失败（防误导，对齐 good-question error/source_count 语义）：
    - 检索正常但空 → source_count=0（三路兜底"未找到"）
    - 检索降级/异常（hint 非空）→ error 字段注入（LLM 兜底声明可信度偏低）
    """
    if ctx.bid is None:
        return {"context": "", "source_count": 0, "confidence_band": "none",
                "error": "标书不存在，无法检索"}
    results, hint, meta = await retrieve_with_meta(
        clean_query(query), lot_id=ctx.bid.lot_id, bid_id=ctx.bid.bid_id,
        dimension=None, top_k=8, return_meta=True,
    )
    chunks = [clean_bid_text(r.content) for r in results if r.source in ("vector", "keyword")]
    out: dict = {"source_count": len(chunks), "confidence_band": meta["confidence_band"],
                 "max_score": meta["max_score"], "context": ""}
    if not chunks:
        if hint is not None:
            out["error"] = hint  # 检索降级且无结果：走 LLM 兜底声明可信度偏低
        return out
    out["context"] = "\n\n".join(f"[来源{i}] {c}" for i, c in enumerate(chunks, 1))
    if hint is not None:
        out["hint"] = hint  # 低置信降级但有结果：正常作答，附降级提示让 LLM 如实说明
    return out


async def execute_get_dimension_rubric(ctx: ToolContext) -> dict:
    """get_dimension_rubric：当前评审维度评分标准（维度 + 评分项 + 细则）。"""
    dim = ctx.dimension
    if dim is None:
        return {"error": "当前评审无关联维度，无法获取评分标准"}
    criteria = (
        await ctx.session.scalars(
            select(ScoringCriterion).where(ScoringCriterion.dimension_id == dim.dimension_id)
        )
    ).all()
    return {
        "dimension": {"name": dim.name, "max_score": dim.max_score},
        "criteria": [
            {"name": c.name, "max_score": c.max_score,
             "rubric": c.scoring_rubric or c.description or ""}
            for c in criteria
        ],
        "source_count": len(criteria),
    }


def _match_field(fields: dict, field: str) -> Optional[str]:
    """结构化字段模糊匹配：精确命中优先，其次子串包含（"资质"→"资质证书"）。"""
    if field in fields:
        return field
    for k in fields:
        if field in k or k in field:
            return k
    return None


async def execute_get_bid_structured_info(ctx: ToolContext, field: str | None = None) -> dict:
    """get_bid_structured_info：标书结构化数据（报价/工期/团队 + structured_data 兜底）。"""
    bid = ctx.bid
    if bid is None:
        return {"error": "标书不存在"}
    fields: dict = {
        "报价金额": str(bid.bid_amount) if bid.bid_amount else None,
        "工期": bid.duration,
        "团队规模": bid.team_size,
    }
    if bid.structured_data:
        fields.update({str(k): v for k, v in bid.structured_data.items()})
    if field:
        key = _match_field(fields, field)
        if key:
            fields = {key: fields[key]}
        else:
            return {"fields": {}, "source_count": 0, "error": f"未找到字段：{field}"}
    return {"fields": fields, "source_count": len(fields)}
