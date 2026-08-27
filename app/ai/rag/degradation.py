"""空结果与降级处理（P2.4）。

集中定义降级/拒答文案与判定，供 retriever、评审链路（P3.x）、API 层复用，
避免各层各自拼字符串。文案对齐 solution.md 5.7 节。

- 向量检索无结果且标书未解析完成 → "该标书正在解析中，请稍后再试"
- 全部 chunk 最高 IP < 0.5 → "未找到与该问题相关的依据"（拒答）
- Milvus 超时 → "语义检索暂不可用，以下分析仅基于结构化数据"（降级关键词+结构化）
- BGE-M3 不可用 → "AI 推理引擎暂不可用，已切换为人工评审模式"
- MySQL 超时 → 503 "核心数据暂不可用，请稍后重试"（事实数据不可缺失，必须报错）
"""

from __future__ import annotations

# 语义相似度拒答阈值（BGE-M3 归一化后 IP 等效余弦）
SIMILARITY_THRESHOLD = 0.5
# Milvus 语义检索超时（solution.md：asyncio.wait_for(10s)）
SEMANTIC_TIMEOUT_SECONDS = 10.0
# 置信档位阈值（retriever._confidence_band 用，集中管理避免散落）：
# 低于 LOW（=拒答阈值）判"无关"（none）；[LOW, HIGH) 相关性存疑（low）；>= HIGH 高相关（high）
CONFIDENCE_LOW_THRESHOLD = SIMILARITY_THRESHOLD
CONFIDENCE_HIGH_THRESHOLD = 0.65


class DegradationHint:
    """降级/拒答提示文案（前端 SSE 展示）。"""

    PARSING = "该标书正在解析中，请稍后再试"
    NO_EVIDENCE = "未找到与该问题相关的依据"
    SEMANTIC_DOWN = "语义检索暂不可用，以下分析仅基于结构化数据"
    LLM_DOWN = "AI 推理引擎暂不可用，已切换为人工评审模式"
    MYSQL_DOWN = "核心数据暂不可用，请稍后重试"
    # P8 LLM 输出校验：答非所问/空输出/分数越界 → 人工评分兜底
    NO_SCORE = "未识别到有效分数，请人工评分"
    SCORE_OUT_OF_RANGE = "LLM 输出分数超出范围，已忽略，请人工评分"
    EMPTY_OUTPUT = "AI 未返回有效内容，请人工评分"


def classify_retrieval(max_score: float | None, *, bid_parsed: bool, semantic_ok: bool) -> str | None:
    """根据检索结果状态返回降级/拒答提示，正常返回 None。

    判定顺序（solution.md 5.7）：
    1. 语义检索超时/不可用 → SEMANTIC_DOWN（调用方走关键词+结构化降级）
    2. 无结果且标书未解析完成 → PARSING
    3. 全部 chunk 低于阈值（最高 IP < 0.5）→ NO_EVIDENCE（拒答）
    """
    if not semantic_ok:
        return DegradationHint.SEMANTIC_DOWN
    if max_score is None:
        return DegradationHint.PARSING if not bid_parsed else DegradationHint.NO_EVIDENCE
    if max_score < SIMILARITY_THRESHOLD:
        return DegradationHint.NO_EVIDENCE
    return None
