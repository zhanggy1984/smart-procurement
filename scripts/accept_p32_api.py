"""P3.2 Prompt 模板 + 意图识别验收脚本。

覆盖 task.md P3.2 验收：
- 30 条标注 prompt 测试集（SCORE_REQUEST 10 / TECH_DETAIL 10 / GENERAL 10）
  → 真实 DeepSeek 调用 → parse_intent 准确率 ≥90%
- build_score_prompt：chunks 在 <bid_content> 隔离、System 含注入防御
- build_chat_prompt：含历史对话
- parse_intent 单元：各格式解析

前置：DeepSeek key 已配。纯 client 测试。
用法: poetry run python scripts/accept_p32_api.py
"""

from __future__ import annotations

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.ai.llm import prompts  # noqa: E402
from app.ai.llm.deepseek_client import get_client  # noqa: E402

PASS = 0
FAIL = 0

TEST_CASES = [
    (prompts.INTENT_SCORE_REQUEST, [
        "请对技术方案维度评分",
        "按评分标准给这个标书打分",
        "请给出项目团队维度的分数",
        "根据评分标准评估并打分",
        "请评一下技术方案的得分",
        "给企业资质打多少分",
        "请按满分标准逐项打分",
        "请完成评分并说明理由",
        "技术方案应该得多少分",
        "请评分项目理解维度",
    ]),
    (prompts.INTENT_TECH_DETAIL, [
        "技术方案中微服务架构的具体实现细节",
        "请详细说明容器化部署的方案",
        "追问：第3点依据的原文是什么",
        "安全方案里数据加密是怎么实现的",
        "请展开讲讲项目团队的配置",
        "灰度发布的实现方式是什么",
        "售后服务具体包含哪些内容",
        "请引用标书中关于熔断降级的原文",
        "入侵检测系统如何部署",
        "请详细说明链路追踪的选型",
    ]),
    (prompts.INTENT_GENERAL, [
        "什么是标书评审",
        "介绍一下这个评审系统",
        "评审的一般流程是什么",
        "为什么需要专家评审",
        "评分标准是怎么制定的",
        "标书和招标文件的区别",
        "你们是怎么工作的",
        "评审结果的用途是什么",
        "什么是回避原则",
        "报告怎么写",
    ]),
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def main() -> None:
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
    check("System 含意图标记指令", "[INTENT:" in sys_text)
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

    print("\n[意图识别] 30 条标注测试集 → DeepSeek")
    client = get_client()
    correct = 0
    total = 0
    for want, questions in TEST_CASES:
        for q in questions:
            p = prompts.build_chat_prompt(
                role_context="你是标书评审助手，先判断用户意图再回答。",
                context="", history=[], question=q,
            )
            text = await client.chat(p, max_tokens=40)
            got = prompts.parse_intent(text)
            total += 1
            if got == want:
                correct += 1
            else:
                print(f"  [误判] 期望={want} 实际={got} query={q} raw={text[:60]!r}")
    acc = correct / total if total else 0
    check(f"意图识别准确率 ≥90%（实际 {acc:.0%}，{correct}/{total}）", acc >= 0.90, f"acc={acc:.2f}")

    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
