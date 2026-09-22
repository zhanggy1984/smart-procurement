"""提示词模板集中存放与加载。

每个模板一个纯文本文件（.md），正文即全部内容——改文案不必动代码。

**不做任何插值**：load_prompt 只负责读取。带 {占位符} 的模板由调用方 .format()
填充，插值时机与参数留给调用方，不把各自的上下文知识塞进加载器。

**读取时机统一在导入期（lru_cache），不是每次调用读盘**：模板一经加载就冻结，
改 .md 必须重启进程才生效。三个理由——① 与外置前行为一致：那时所有提示词
都是模块级字面量/常量，本来就在导入期固化；② 免掉每请求的磁盘 I/O；
③ 文件层热更不是目标——若允许改文件即生效，会制造「哪些模板改了立刻生效、
哪些要重启」的歧义，而两种时机共存正是这里要消除的东西。**「重启才生效」
是刻意的设计，不是尚未做热更的将就。**
（lru_cache 不缓存异常，故 .md 缺失时每次调用都会重试读盘——只影响失败路径。）

放在 app/ 下而不是 app/ai/llm/ 下：后者已有模块 prompts.py，同名会形成
「包 vs 模块」冲突，且既有 `from app.ai.llm.prompts import ...` 的调用方会失联。
"""
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """读取提示词模板正文。

    Args:
        name: 模板名（不含 .md 后缀），如 "score_system"。

    Returns:
        模板正文，原样返回、不做插值；带占位符的模板由调用方自行 .format()。
    """
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")
