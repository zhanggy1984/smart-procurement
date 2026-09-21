"""app.prompts.load_prompt 的行为契约（提示词外置的加载侧）。

钉住两件事，防后人"顺手"改掉：

1. **模板缺失 → FileNotFoundError，不兜底**。提示词缺一段等于行为变了（评分/对话
   文案少一块），静默降级跑起来比启动失败危险得多；而且兜底要把文案再抄一份回代码，
   正是本次外置要消除的双副本。所以这里**没有** except，也不该有。
2. **只读一次后走缓存**。常量与 builder 必须同一种时机——否则会出现「哪些模板改了
   立刻生效、哪些要重启」的歧义，而消除这个歧义正是加缓存的原因。
"""
import pytest

from app.prompts import load_prompt

# 10 个模板全清单：少一个说明文件没随代码提交，多一个说明有意料之外的文件
EXPECTED_TEMPLATES = [
    "chat_system",
    "conversation_summary_system",
    "fraud_report_system",
    "not_found_answer",
    "override_context",
    "retrieval_unavailable_hint",
    "score_system",
    "tag_match_system",
    "tools_declaration",
    "unknown_answer",
]


def test_all_templates_loadable_and_non_empty() -> None:
    """清单里的模板都能读到且非空——空模板等于该段文案消失，不如下线时显式报错。"""
    for name in EXPECTED_TEMPLATES:
        text = load_prompt(name)
        assert text.strip(), f"模板 {name} 为空"


def test_missing_template_raises_file_not_found() -> None:
    """缺失即抛，不给默认值——fail-fast 是刻意的，见模块 docstring。"""
    with pytest.raises(FileNotFoundError):
        load_prompt("__no_such_template__")


def test_loaded_once_then_cached() -> None:
    """同一模板只读一次盘：第二次命中缓存（hits 增加）。"""
    load_prompt.cache_clear()
    load_prompt("score_system")
    after_first = load_prompt.cache_info()
    assert after_first.misses == 1 and after_first.hits == 0, after_first

    load_prompt("score_system")
    after_second = load_prompt.cache_info()
    assert after_second.misses == 1 and after_second.hits == 1, after_second
    load_prompt.cache_clear()
