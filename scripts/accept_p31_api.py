"""P3.1 DeepSeek Client + 容错验收脚本。

覆盖 task.md P3.1 验收：
- 正常流式：真实 DeepSeek chat_stream 逐段输出
- 断路器状态机：连续失败 → OPEN → acquire 抛 CircuitOpenError → 到期 HALF_OPEN → 成功 CLOSE
- 429 退避重试：mock 抛 429 两次后成功 → 共 3 次调用（1s/2s/4s 退避逻辑）
- 连续 503 → 熔断 OPEN → 再调用抛 CircuitOpenError

前置：DeepSeek key 已配；纯 client 测试，不需要 uvicorn/worker。
用法: poetry run python scripts/accept_p31_api.py
"""

from __future__ import annotations

import asyncio
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
import openai  # noqa: E402
import httpx  # noqa: E402
from app.ai.llm import deepseek_client as dsc  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _status_error(status: int, kind: str) -> Exception:
    req = httpx.Request("POST", "http://fake")
    resp = httpx.Response(status, request=req)
    if kind == "rate":
        return openai.RateLimitError(message=f"err {status}", response=resp, body=None)
    return openai.InternalServerError(message=f"err {status}", response=resp, body=None)


async def main() -> None:
    global PASS, FAIL

    # ==================== 断路器单元状态机 ====================
    print("\n[断路器] 状态机")
    cb = dsc._CircuitBreaker(threshold=3, open_seconds=1.0)
    for _ in range(3):
        await cb.record_failure()
    check("连续 3 次失败 → OPEN", cb.state == "OPEN", cb.state)
    try:
        await cb.acquire()
        check("OPEN 时 acquire 抛 CircuitOpenError", False)
    except dsc.CircuitOpenError:
        check("OPEN 时 acquire 抛 CircuitOpenError", True)
    # 模拟熔断到期 → HALF_OPEN → 探测成功 → CLOSED
    await asyncio.sleep(1.1)
    await cb.acquire()  # 到期转 HALF_OPEN
    check("熔断到期 → HALF_OPEN", cb.state == "HALF_OPEN", cb.state)
    await cb.record_success()
    check("探测成功 → CLOSED", cb.state == "CLOSED", cb.state)

    # ==================== 正常流式（真实 DeepSeek） ====================
    print("\n[流式] 真实 DeepSeek chat_stream")
    client = dsc.get_client()
    text = ""
    try:
        async for delta in client.chat_stream(
            [{"role": "user", "content": "用一句话介绍标书评审系统"}], max_tokens=100
        ):
            text += delta
        check("流式输出非空", bool(text.strip()), text[:80])
    except Exception as e:  # noqa: BLE001
        check("流式输出非空", False, f"err={type(e).__name__}: {e}")
    check("熔断状态 CLOSED（正常调用后）", client.circuit_state == "CLOSED", client.circuit_state)

    # ==================== 429 退避重试（mock） ====================
    print("\n[429] 退避重试")
    c1 = dsc.DeepSeekClient()
    calls = {"n": 0}

    async def fake429_create(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(429, "rate")
        from types import SimpleNamespace
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    c1._client.chat.completions.create = fake429_create
    out = await c1.chat([{"role": "user", "content": "x"}])
    check("429 重试后成功返回", out == "ok", out)
    check("429 退避 3 次尝试（2 次失败+1 次成功）", calls["n"] == 3, f"calls={calls['n']}")
    check("429 不熔断（限流非故障）", c1.circuit_state == "CLOSED", c1.circuit_state)

    # ==================== 连续 503 → 熔断 OPEN ====================
    print("\n[503] 连续失败 → 熔断 OPEN")
    c2 = dsc.DeepSeekClient()

    async def fake503_create(**kw):
        raise _status_error(503, "internal")

    c2._client.chat.completions.create = fake503_create
    for _ in range(5):
        try:
            await c2.chat([{"role": "user", "content": "x"}])
        except Exception:  # noqa: BLE001  每次失败（重试耗尽后抛）
            pass
    check("连续 503 → 熔断 OPEN", c2.circuit_state == "OPEN", c2.circuit_state)
    try:
        await c2.chat([{"role": "user", "content": "x"}])
        check("OPEN 后再调用抛 CircuitOpenError", False)
    except dsc.CircuitOpenError:
        check("OPEN 后再调用抛 CircuitOpenError", True)

    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
