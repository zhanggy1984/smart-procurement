"""SSE 事件序列化（P3.3）。

SSE 帧格式：`id: {seq}\\nevent: {event}\\ndata: {json}\\n\\n`。
seq 递增用于前端 Last-Event-ID 断流续推（P3.6）。data 统一 JSON（中文 ensure_ascii=False）。
data 内置 ts（unix ms，评测契约 §5.1 agent 侧生成）。
"""

from __future__ import annotations

import json
import time


def sse_event(event: str, data: dict, seq: int) -> str:
    """构造一个 SSE 帧。data 统一注入 ts（unix ms），供评测端阶段耗时分析。"""
    payload = {**data, "ts": int(time.time() * 1000)}
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
