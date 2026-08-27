"""agent 编排（function calling）：LLM 自主决定是否调内部工具（检索/评分标准/结构化数据）。

决策轮静默 + 作答轮流式三发（SSE 契约兼容）。对外入口 stream_agent（agent_loop.py）。
"""

from app.ai.agent.agent_loop import MAX_TOOL_ROUNDS, stream_agent
from app.ai.agent.tools import (
    CHAT_TOOLS,
    GET_BID_STRUCTURED_INFO_TOOL,
    GET_DIMENSION_RUBRIC_TOOL,
    RETRIEVE_KNOWLEDGE_TOOL,
    ToolContext,
)

__all__ = [
    "MAX_TOOL_ROUNDS",
    "stream_agent",
    "CHAT_TOOLS",
    "ToolContext",
    "RETRIEVE_KNOWLEDGE_TOOL",
    "GET_DIMENSION_RUBRIC_TOOL",
    "GET_BID_STRUCTURED_INFO_TOOL",
]
