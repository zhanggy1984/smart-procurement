"""P7.x 检索 query 清洗单元测试（query_cleaner.py，参考 good-question function calling 契约）。

覆盖确定性规则清洗：全角数字/字母转半角、去 emoji、剥客套前后缀、去首尾标点、
压空白、剥空回退原文、超长截断（句末标点优先 + 无标点硬截）、中文标点保留、
评审追问场景（不删实体）。
"""

from __future__ import annotations

from app.ai.rag.query_cleaner import clean_query


def test_fullwidth_digits_letters_to_halfwidth():
    """全角数字/字母转半角（仅 ０-９ Ａ-Ｚ ａ-ｚ）。"""
    assert clean_query("１３８ 服务部署") == "138 服务部署"
    assert clean_query("Ａ方案Ｂ") == "A方案B"


def test_remove_emoji():
    """emoji/杂项符号无检索价值，剔除。"""
    assert clean_query("这个方案怎么样👍") == "这个方案怎么样"
    assert clean_query("部署流程❤️确认") == "部署流程确认"


def test_casual_prefix_stripped():
    """客套前缀剥离（请问/麻烦/帮我看看…）。"""
    assert clean_query("请问这个标书的评审标准是什么") == "这个标书的评审标准是什么"
    assert clean_query("麻烦你帮我看看技术方案的完整性") == "技术方案的完整性"


def test_casual_suffix_stripped():
    """客套后缀剥离（谢谢/辛苦了…）。"""
    assert clean_query("工资发放流程谢谢") == "工资发放流程"
    assert clean_query("帮我查一下报价情况辛苦了") == "报价情况"


def test_edge_noise_stripped():
    """客套剥离后残留的首尾冗余标点剔除（含句末句号——与 good-question 一致，
    尾部标点对向量/关键词检索均无价值）。"""
    assert clean_query("、标书里技术方案。，") == "标书里技术方案"
    assert clean_query("评分标准是什么？，谢谢") == "评分标准是什么"


def test_collapse_whitespace():
    """连续空白压缩为单个空格。"""
    assert clean_query("技术  方案  部署") == "技术 方案 部署"


def test_keep_chinese_punct_in_middle():
    """中文标点在句中保留（不转半角、不剔除），仅剥首尾冗余。"""
    assert clean_query("标书里的『评分标准』是什么") == "标书里的『评分标准』是什么"


def test_review_question_cleaned():
    """评审追问场景：剥客套 + 去尾部标点，语义实体完整保留。"""
    assert clean_query("请问这个标书对技术方案的评审标准是什么？谢谢") == (
        "这个标书对技术方案的评审标准是什么"
    )


def test_entities_not_removed():
    """实体（认证/编号/术语）不被误删。"""
    assert clean_query("标书是否具备 CMMI3 认证") == "标书是否具备 CMMI3 认证"


def test_empty_or_blank_fallback():
    """空/纯空白输入回退原文（防空 query 语义漂移，保持 good-question 行为）。"""
    assert clean_query("") == ""
    assert clean_query("   ") == "   "


def test_no_noise_unchanged():
    """无噪音 query 原样返回。"""
    q = "技术方案的评审标准"
    assert clean_query(q) == q


def test_long_query_truncate_at_boundary():
    """超长 query 优先在最后一个句末标点处截断，不切断完整句子。"""
    q = "第一句。" + "第二句。" * 120  # 480 字 > 400 上限
    out = clean_query(q)
    assert len(out) <= 400
    # 截断点在句末标点之后（末段是完整句）
    assert out.endswith("。")


def test_long_query_no_boundary_hard_truncate():
    """超长 query 无句末标点时硬截保信息量（400 字上限）。"""
    q = "甲" * 450  # 无标点长串
    out = clean_query(q)
    assert len(out) == 400


def test_combo_noise():
    """组合：全角 + 客套 + emoji + 尾部标点一次清干净。"""
    assert clean_query("请问，１３８😊 服务部署方案怎样？谢谢") == "138 服务部署方案怎样"
