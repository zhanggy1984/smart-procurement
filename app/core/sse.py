"""SSE 事件序列化（P3.3）。

SSE 帧格式：`id: {seq}\\nevent: {event}\\ndata: {json}\\n\\n`。
seq 递增用于前端 Last-Event-ID 断流续推（P3.6）。data 统一 JSON（中文 ensure_ascii=False）。
"""

from __future__ import annotations

import json


def sse_event(event: str, data: dict, seq: int) -> str:
    """构造一个 SSE 帧。"""
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
