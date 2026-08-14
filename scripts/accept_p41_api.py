"""P4.1 受控词表 + LLM 标签翻译验收脚本。

覆盖 task.md P4.1 验收：
- translate_tags 项目描述 → DeepSeek → 受控词表内专业标签（全部在词表内）
- LLM 不可用降级 → MANUAL_TAG_SELECTION
- parse_tags 单元（过滤词表外标签）

前置：DeepSeek key 已配。纯 service 调用。
用法: poetry run python scripts/accept_p41_api.py
"""

from __future__ import annotations

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.constants import EXPERT_TAGS  # noqa: E402
from app.services import tag_translation_service as svc  # noqa: E402

PASS = 0
FAIL = 0

CASES = [
    "某市教育局智慧校园平台采购项目",
    "省公安厅视频监控与网络安全系统建设",
    "市医院电子病历与医疗信息化升级",
    "市政务服务中心一网通办电子政务平台",
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

    # ==================== parse_tags 单元 ====================
    print("\n[单元] parse_tags")
    check("过滤词表外标签", svc.parse_tags("教育信息化、软件开发、智慧教育") == ["教育信息化", "软件开发"],
          str(svc.parse_tags("教育信息化、软件开发、智慧教育")))
    check("去重保序", svc.parse_tags("软件开发,软件开发,教育信息化") == ["软件开发", "教育信息化"],
          str(svc.parse_tags("软件开发,软件开发,教育信息化")))
    check("空输入 → []", svc.parse_tags("") == [])

    # ==================== LLM 翻译（真实 DeepSeek） ====================
    print("\n[翻译] 真实 DeepSeek → 词表内标签")
    tag_set = set(EXPERT_TAGS)
    all_ok = True
    for desc in CASES:
        tags, mode = await svc.translate_tags(desc)
        in_vocab = bool(tags) and all(t in tag_set for t in tags)
        all_ok = all_ok and in_vocab
        check(f"'{desc[:12]}...' → 词表内标签", in_vocab,
              f"tags={tags} mode={mode}（词表 {len(EXPERT_TAGS)} 项）")
        if tags:
            check(f"  模式 AUTO（{desc[:12]}）", mode == svc.MODE_AUTO, mode)
    check("全部翻译命中词表", all_ok)

    # ==================== LLM 不可用降级 ====================
    print("\n[降级] LLM 不可用 → MANUAL_TAG_SELECTION")
    from app.ai.llm.deepseek_client import CircuitOpenError, get_client

    orig = get_client().chat
    async def fake_fail(*a, **kw):
        raise CircuitOpenError("测试熔断")
    get_client().chat = fake_fail
    try:
        tags, mode = await svc.translate_tags("某市教育局智慧校园平台采购")
    finally:
        get_client().chat = orig
    check("LLM 熔断 → 空标签 + MANUAL_TAG_SELECTION",
          tags == [] and mode == svc.MODE_MANUAL, f"tags={tags} mode={mode}")

    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
