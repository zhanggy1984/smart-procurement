"""agent 编排（function calling）：LLM 自主决定是否调内部工具（检索/评分标准/结构化数据）。

决策轮静默 + 作答轮流式三发（SSE 契约兼容）。对外入口：
- chat_agent（agent_loop.py）：控制层对话门面（C 档分层，接管对话状态管理）
- stream_agent（agent_loop.py）：纯编排生成器（决策+作答，不碰 DB）
"""

from app.ai.agent.agent_loop import MAX_TOOL_ROUNDS, chat_agent, stream_agent
from app.ai.agent.tools import (
    CHAT_TOOLS,
    GET_BID_STRUCTURED_INFO_TOOL,
    GET_DIMENSION_RUBRIC_TOOL,
    RETRIEVE_KNOWLEDGE_TOOL,
    ToolContext,
)

__all__ = [
    "MAX_TOOL_ROUNDS",
    "chat_agent",
    "stream_agent",
    "CHAT_TOOLS",
    "ToolContext",
    "RETRIEVE_KNOWLEDGE_TOOL",
    "GET_DIMENSION_RUBRIC_TOOL",
    "GET_BID_STRUCTURED_INFO_TOOL",
]
