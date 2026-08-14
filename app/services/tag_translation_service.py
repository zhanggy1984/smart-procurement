"""受控词表 + LLM 标签翻译（P4.1）。

translate_tags()：项目描述 → DeepSeek → 受控词表（constants.EXPERT_TAGS）内的
专业标签列表。LLM 输出严格过滤到词表内（词表外一律丢弃），保证 P4.2 匹配可用。

降级（task.md P4.1）：LLM 不可用（断路器 OPEN/异常）或输出无法匹配词表 →
返回空标签 + match_mode=MANUAL_TAG_SELECTION（PM 手动从多选下拉框选择）。
"""

from __future__ import annotations

import re

import structlog

from app.ai.llm.deepseek_client import CircuitOpenError, get_client
from app.core.constants import EXPERT_TAGS

logger = structlog.get_logger(__name__)

# 匹配模式（P4.2 结果标注）
MODE_AUTO = "AUTO"
MODE_MANUAL = "MANUAL_TAG_SELECTION"

# 词表 set（O(1) 校验）
_TAG_SET = set(EXPERT_TAGS)


def parse_tags(text: str) -> list[str]:
    """解析 LLM 输出，仅保留受控词表内标签（去重保序）。"""
    if not text:
        return []
    parts = re.split(r"[,，、;；\n]+", text)
    tags = [p.strip() for p in parts if p.strip() in _TAG_SET]
    return list(dict.fromkeys(tags))


def _build_prompt(description: str) -> str:
    return (
        f"从以下受控词表中选择与项目描述最匹配的 2-4 个专业标签。\n"
        f"受控词表：{'、'.join(EXPERT_TAGS)}\n"
        f"项目描述：{description}\n"
        "只输出选中的标签，用顿号分隔，不要任何解释或额外文字。"
    )


async def translate_tags(description: str) -> tuple[list[str], str]:
    """项目描述 → 词表内专业标签。返回 (tags, match_mode)。

    match_mode：AUTO（LLM 自动翻译成功且命中词表）/ MANUAL_TAG_SELECTION（降级）。
    """
    if not description or not description.strip():
        return [], MODE_MANUAL
    try:
        text = await get_client().chat(
            [
                {"role": "system", "content": "你是专业领域标签匹配助手，严格从给定词表选择。"},
                {"role": "user", "content": _build_prompt(description.strip())},
            ],
            temperature=0.3,
            max_tokens=100,
        )
    except CircuitOpenError as e:
        logger.warning("tag.llm_circuit_open", error=str(e))
        return [], MODE_MANUAL
    except Exception as e:  # noqa: BLE001  LLM 网络/其他异常
        logger.warning("tag.llm_failed", error=str(e))
        return [], MODE_MANUAL

    tags = parse_tags(text)
    if not tags:
        logger.info("tag.no_match", description=description[:50], llm_output=text[:100])
        return [], MODE_MANUAL
    logger.info("tag.translated", description=description[:50], tags=tags, mode=MODE_AUTO)
    return tags, MODE_AUTO
