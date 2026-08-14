"""P7.2 单元测试公共 fixture。

单元测试隔离真实基础设施：
- ai/llm：vcrpy 录制回放（tests/cassettes/*.yaml）减少 API 调用成本；断路器/重试
  用 monkeypatch 直测状态机
- ai/rag：chunker/retriever 纯函数直测，embedder 用 fake HTTP 响应
- services：AsyncSession 用 unittest.mock.AsyncMock 构造，不连真实 MySQL
"""

from __future__ import annotations

import pytest

# 关闭真实 LLM 调用（单元测试不发起网络请求；断路器/重试单独构造测试）
pytest_plugins = []
