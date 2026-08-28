"""P7.5 意图识别基准（真实 DeepSeek，30 prompt）。

与 P3.2 验收同测试集（SCORE_REQUEST 10 / TECH_DETAIL 10 / GENERAL 10），
当前 build_chat_prompt 已移除意图标记指令（P6.7 联调遗留），故本基准自包含
意图指令 prompt（INTENT_INSTRUCTION，与 prompts._intent_instruction 语义一致，
不依赖私有 API），调用 DeepSeek → parse_intent → 准确率 ≥90%。

验收不达标记录 issue，不改测试集。

用法: poetry run python scripts/benchmark_p75/benchmark_intent.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.ai.llm.deepseek_client import get_client  # noqa: E402
from app.ai.llm.prompts import parse_intent  # noqa: E402

import bench_data as B  # noqa: E402


async def main() -> None:
    client = get_client()
    correct = total = 0
    mis: list[tuple[str, str, str, str]] = []
    for want, questions in B.INTENT_CASES.items():
        for q in questions:
            prompt = [
                {"role": "system",
                 "content": f"你是标书评审助手，先判断用户意图再回答。\n{B.INTENT_INSTRUCTION}"},
                {"role": "user", "content": q},
            ]
            text = await client.chat(prompt, max_tokens=40)
            got = parse_intent(text)
            total += 1
            if got == want:
                correct += 1
            else:
                mis.append((want, got, q, text[:60].replace("\n", " ")))
    acc = correct / total
    print(f"[意图识别] 准确率 = {acc:.0%}（{correct}/{total}）")
    for want, got, q, raw in mis:
        print(f"  [误判] 期望={want} 实际={got} query={q} raw={raw!r}")

    ok = acc >= 0.90
    print("[✓] 意图 ≥90% PASS" if ok else f"[X] 意图 ≥90% FAIL（acc={acc:.2f}）")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
