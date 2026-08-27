"""P3.2 Prompt 模板 + 意图识别验收脚本。

覆盖 task.md P3.2 验收（意图识别部分已随 P6.4.5 收紧）：
- build_score_prompt：chunks 在 <bid_content> 隔离、System 含注入防御
- build_chat_prompt：含历史对话（P6.4.5 起不要求 LLM 输出 [INTENT:] 标记）
- parse_intent 单元：各格式解析

注：P3.2 原「30 条标注测试集 → parse_intent 准确率 ≥90%」验收已废弃——chat 端点
不再要求 LLM 输出 [INTENT: X] 前缀（对用户是噪音，P6.4.5），真实调用必然全判
GENERAL，该断言恒挂且烧 30 次真实 DeepSeek。parse_intent 解析能力由本脚本单测 +
tests/unit/test_prompts.py 覆盖。

前置：DeepSeek key 已配。纯 client 测试。
用法: poetry run python scripts/accept_p32_api.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.ai.llm import prompts  # noqa: E402

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


def main() -> None:
    global PASS, FAIL

    print("\n[单元] prompt 结构与 parse_intent")
    prompt = prompts.build_score_prompt(
        dimension_name="技术方案", max_score=30, rubric="架构合理性 10 分",
        chunks=["标书片段1 微服务架构", "标书片段2 安全设计"],
        structured_data={"quality_cert": "CMMI3"},
    )
    sys_text = prompt[0]["content"]
    user_text = prompt[1]["content"]
    check("chunks 在 <bid_content> 标签内", "<bid_content>" in user_text and "</bid_content>" in user_text)
    check("System 含注入防御声明", "忽略" in sys_text and "依据" in sys_text)
    check("System 含评分格式契约", "最后一行必须严格输出总分格式" in sys_text and "分数:" in sys_text)
    check("评分 prompt 含 rubric 与维度", "技术方案" in sys_text and "架构合理性" in sys_text)

    chat_prompt = prompts.build_chat_prompt(
        role_context="你是标书评审助手", context="历史摘要",
        history=[{"role": "user", "content": "上一轮问题"}, {"role": "assistant", "content": "上一轮回答"}],
        question="继续追问",
    )
    check("对话 prompt 含历史", len(chat_prompt) >= 4, f"len={len(chat_prompt)}")
    check("对话 prompt 含上下文", any("历史摘要" in m["content"] for m in chat_prompt))

    check("parse_intent 标准格式", prompts.parse_intent("[INTENT: SCORE_REQUEST]") == "SCORE_REQUEST")
    check("parse_intent 带正文", prompts.parse_intent("[INTENT: TECH_DETAIL]\n好的") == "TECH_DETAIL")
    check("parse_intent 全角冒号", prompts.parse_intent("[INTENT：GENERAL]") == "GENERAL")
    check("parse_intent 未命中 → GENERAL", prompts.parse_intent("我不太确定") == "GENERAL")

    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
