"""检索 query 规则化去噪（P7.x，参考 good-question function calling 契约）。

只消除对检索有害的确定性噪音，不改语义、不删实体——区别于 LLM 改写
（good-question 历史实测 LLM 改写收益趋零且每检索多 2-4s 延迟，已回滚，
保留确定性规则清洗）。

适用场景：chat 端点用户问题裸进检索前（reviews.py stream_chat）。客套/emoji/
全角词会稀释 BGE-M3 向量编码、污染路2 query 词窗。评分模式 query 是固定模板，
无需清洗。

清洗项（与 good-question _normalize_query 对齐）：
- 全角数字/字母 → 半角（仅 ０-９ Ａ-Ｚ ａ-ｚ，不转中文标点——chunk 入库保留
  全角标点，query 转半角标点在稀疏检索 token 错位丢匹配权重）
- 去 emoji / 杂项符号 / 变体选择符
- 剥口语客套前缀/后缀（请问/麻烦/帮我看看/谢谢/辛苦了…）
- 去首尾冗余标点、压连续空白
- 剥空回退原文（防空 query 拖垮召回）
- 超长截断：上限 400 字，优先在句末标点断（不切断完整句子），断点过靠前硬截保信息量
"""

from __future__ import annotations

import re

# 超长 query 截断上限（BGE 模型 512 token 上限内安全；给整段条款引用留足空间）
_QUERY_MAX_LEN = 400
# 截断时保留的最小长度：前缀内最后一个句末断点过靠前说明整段无标点/连写，
# 此时按断点截会丢信息，退化为硬截保信息量
_QUERY_MIN_KEEP = 64
# 句末标点（中英）与换行：超长 query 优先在此断，避免切断完整句子破坏检索语义
_QUERY_BOUNDARY_RE = re.compile(r"[。！？；.!?;\n]")


def _to_halfwidth(text: str) -> str:
    """全角数字/字母 → 半角。不转中文标点（见模块注释 token 错位原因）。"""
    out = []
    for ch in text:
        code = ord(ch)
        if (
            0xFF10 <= code <= 0xFF19  # ０-９
            or 0xFF21 <= code <= 0xFF3A  # Ａ-Ｚ
            or 0xFF41 <= code <= 0xFF5A  # ａ-ｚ
        ):
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


# emoji / 杂项符号 / 变体选择符：无检索价值，只增噪音
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 扩展象形文字/表情符号
    "\U00002600-\U000027BF"  # 杂项符号/装饰符号
    "\U0001F900-\U0001F9FF"  # 补充符号与象形文字扩展
    "\\uFE0F"  # 变体选择符
    "]+"
)

# 口语客套前缀/后缀（^/$ 锚定，完整词，不单删"请/帮"等可能为实义的单字）
# 按词长降序排列：正则 alternation 左优先，长词先匹配
_CASUAL_PREFIX_WORDS = (
    "麻烦你帮我看看", "麻烦您帮我看看", "麻烦帮我看看",
    "麻烦你帮我", "麻烦您帮我", "麻烦帮我", "麻烦问一下", "麻烦问下",
    "请问一下", "帮我查一下", "帮我看看", "帮忙查一下", "帮忙看看",
    "我想问一下", "我想问下", "想问一下", "想问下", "想咨询一下",
    "咨询一下", "帮忙查", "帮忙", "帮我查", "帮我", "请问", "麻烦",
    "我想问", "想问", "想咨询", "劳驾",
)
_CASUAL_SUFFIX_WORDS = (
    "谢谢啦", "谢谢你", "辛苦啦", "辛苦你了", "谢谢", "感谢",
    "多谢", "辛苦了", "麻烦你了", "拜托啦", "拜托了",
)
_CASUAL_PREFIX_RE = re.compile("^(?:" + "|".join(_CASUAL_PREFIX_WORDS) + ")")
_CASUAL_SUFFIX_RE = re.compile("(?:" + "|".join(_CASUAL_SUFFIX_WORDS) + ")$")

# 客套剥离后可能残留的首尾标点/空白（如"工资几号发，谢谢"剥"谢谢"后剩尾部逗号）
_EDGE_NOISE_RE = re.compile(r"^[，,。.、：:；;!！?？~·\s]+|[，,。.、：:；;!！?？~·\s]+$")
# 连续空白压缩（多个空格撑乱切词）
_COLLAPSE_WS_RE = re.compile(r"\s+")


def _normalize_query(query: str) -> str:
    """规则化去噪：全角数字/字母转半角、去 emoji/客套、压冗余标点。

    只做确定性清洗，不改语义、不删实体。剥离后为空时回退原文，
    保证检索 query 非空（空 query 直接拖垮召回）。
    """
    q = _to_halfwidth(query or "")
    q = _EMOJI_RE.sub("", q)
    q = _CASUAL_PREFIX_RE.sub("", q)
    q = _CASUAL_SUFFIX_RE.sub("", q)
    q = _EDGE_NOISE_RE.sub("", q)
    q = _COLLAPSE_WS_RE.sub(" ", q).strip()
    return q or query  # 剥空回退原文，防空 query 拖垮召回


def _truncate(query: str) -> str:
    """超长 query 截断：优先在最后一个句末标点处断，断点过靠前则硬截保信息量。"""
    if len(query) <= _QUERY_MAX_LEN:
        return query
    prefix = query[:_QUERY_MAX_LEN]
    matches = list(_QUERY_BOUNDARY_RE.finditer(prefix))
    if matches and matches[-1].end() >= _QUERY_MIN_KEEP:
        return prefix[: matches[-1].end()].strip()
    return prefix


def clean_query(query: str) -> str:
    """清洗检索 query（chat 端点用户问题进检索前调用）。

    确定性规则去噪（全半角/emoji/客套/冗余标点），空回退原文，超长截断。
    只去噪不改语义，评测 RAG Recall 不因清洗而回归（噪声少 → 召回更聚焦）。
    """
    q = (query or "").strip()
    if not q:
        return query or ""
    q = _normalize_query(q)
    return _truncate(q)
