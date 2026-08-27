"""P7.x LLM 输入侧清洗单元测试（text_cleaner.py）。

覆盖 clean_text 规范化（移植 good-question 关键用例）+ redact_pii 脱敏：
手机/邮箱/15 位身份证/18 位身份证掩码格式、USCC 防误伤（上下文占位 + 生日校验
跳过）、掩码长度与原值一致、边界防误伤、先规范化后脱敏的顺序。
"""

from __future__ import annotations

from app.ai.llm.text_cleaner import clean_bid_text, clean_text, redact_pii

# ==================== 规范化（移植 good-question 关键用例） ====================


def test_clean_text_removes_control_chars():
    assert clean_text("a\x00b") == "ab"


def test_clean_text_compress_blank_lines():
    assert clean_text("a\n\n\n\nb") == "a\n\nb"


def test_clean_text_fullwidth_to_halfwidth():
    assert clean_text("ＡＢＣ１２３＠") == "ABC123@"


def test_clean_text_keeps_chinese_punct():
    """中文标点不转半角（NFKC 跳过），保中文排版。"""
    assert clean_text("你好，世界。") == "你好，世界。"


def test_clean_text_unify_newlines():
    assert clean_text("a\r\nb\rc") == "a\nb\nc"


def test_clean_text_remove_bom_and_zerowidth():
    assert clean_text("﻿开头文本") == "开头文本"
    assert clean_text("工资​发放") == "工资发放"
    assert clean_text("a‍b‌c") == "abc"


def test_clean_text_nbsp_and_fullwidth_space():
    assert clean_text("a b") == "a b"
    assert clean_text("a　b") == "a b"


def test_clean_text_trailing_spaces():
    assert clean_text("hello   \nworld") == "hello\nworld"


def test_clean_text_strip():
    assert clean_text("  hello  ") == "hello"


def test_clean_text_empty():
    assert clean_text("") == ""


# ==================== PII 脱敏 ====================


def test_redact_mobile():
    assert redact_pii("联系人 13812345678") == "联系人 138****5678"


def test_redact_mobile_length_preserved():
    """掩码长度与原值一致（不破坏 LLM 对文本结构的理解）。"""
    masked = redact_pii("13812345678")
    assert masked == "138****5678"
    assert len(masked) == 11


def test_redact_email():
    assert redact_pii("联系邮箱 zhangsan@corp.com") == "联系邮箱 z***@corp.com"


def test_redact_email_with_digits_and_subdomain():
    assert redact_pii("邮箱 sup2024@example.com.cn") == "邮箱 s***@example.com.cn"


def test_redact_id18():
    """18 位身份证：第 7-14 位合法生日 → 保留前 3 后 4。"""
    assert redact_pii("身份证 110101199001012345") == "身份证 110***********2345"


def test_redact_id15():
    """15 位身份证：第 7-12 位合法生日 → 掩码。"""
    assert redact_pii("旧身份证 110101900101123") == "旧身份证 110********1123"


def test_bare_18_digits_not_birth_kept():
    """无信用代码上下文、18 位纯数字第 7-14 位非合法日期 → 视为普通编号保留（USCC 形态）。"""
    assert redact_pii("编号 913501001234567890") == "编号 913501001234567890"


def test_uscc_context_placeholder():
    """信用代码上下文 + 18 位代码 → 整体占位（不被身份证规则掩码）。"""
    assert redact_pii("统一社会信用代码 913501001234567890") == "【统一社会信用代码】"
    assert redact_pii("信用代码：91350100MA34XYZ123") == "【统一社会信用代码】"


def test_uscc_birth_like_still_placeholder():
    """即便 18 位代码第 7-14 位是合法日期（身份证形态），信用代码上下文优先 → 占位。"""
    assert redact_pii("统一社会信用代码 110101199001012345") == "【统一社会信用代码】"


def test_mobile_not_part_of_longer_digits():
    """超长数字串内部不命中手机号（13 位非 11 位）。"""
    assert redact_pii("大金额 1381234567890") == "大金额 1381234567890"


def test_landline_not_redacted():
    """带区号座机不误伤。"""
    assert redact_pii("座机 010-88886666") == "座机 010-88886666"


def test_no_pii_unchanged():
    assert redact_pii("我司具备 ISO9001 认证，核心团队 25 人，工期 180 天。") == (
        "我司具备 ISO9001 认证，核心团队 25 人，工期 180 天。"
    )


def test_redact_empty():
    assert redact_pii("") == ""


# ==================== 组合入口 clean_bid_text ====================


def test_clean_bid_text_fullwidth_phone():
    """先规范化（全角数字转半角）再脱敏的顺序验证。"""
    assert clean_bid_text("电话：１３８１２３４５６７８") == "电话：138****5678"


def test_clean_bid_text_clean_text_unchanged():
    """无 PII 的干净文本经过组合入口后原样返回。"""
    s = "我司具备 ISO9001 认证，核心团队 25 人，工期 180 天。"
    assert clean_bid_text(s) == s


def test_clean_bid_text_empty():
    assert clean_bid_text("") == ""
