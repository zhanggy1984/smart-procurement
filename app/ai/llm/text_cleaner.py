"""LLM 输入侧数据清洗：文本规范化 + PII 脱敏（P7.x）。

为什么放在检索后/prompt 前（而非入库时）：
- 不动 document_ingest/Milvus/chunk 边界，评测 recall/评分零影响，存量真实标书同样覆盖；
- 真实标书正文的身份证/手机/邮箱明文进 <bid_content>，LLM 会在 <answer> 里复述
  （输出侧泄漏），本模块在组装 prompt 前洗掉。

机制参考 good-question 的清洗层（纯函数 + 保守策略 + 全量单测）：
- clean_text() 规范化直接移植其 text_cleaner；
- PII 脱敏为其无能力项，本模块自研（redact_pii）。

对外唯一入口 clean_bid_text()，先规范化再脱敏，幂等。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

# ==================== 文本规范化（移植 good-question text_cleaner） ====================

# 中文标点：NFKC 会把它们转半角破坏中文排版，规范化时跳过
_CHINESE_PUNCT = set("，。！？；：、（）《》「」『』【】“”‘’—…")


def clean_text(text: str) -> str:
    """文本规范化：去 BOM、统一换行、全角→半角（跳过中文标点）、去控制字符/零宽、
    压空行、去行尾空格。保持段落结构（\n）。

    不移植 good-question 的页眉/页脚去重（默认 OFF 的启发式，无本仓库评测基线
    支撑，拍脑袋优化会倒退）。无输入返回空串。
    """
    if not text:
        return ""
    # 1. 去行首 BOM
    text = text.lstrip("﻿")
    # 2. 统一换行符（\r\n / \r → \n）
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 3. 全角→半角（NFKC），跳过中文标点
    text = "".join(
        unicodedata.normalize("NFKC", ch) if ch not in _CHINESE_PUNCT else ch
        for ch in text
    )
    # 4. 去控制字符（保留换行/制表符）
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    # 5. 不可见噪声：零宽字符（U+200B/C/D）删除——不可见但打断 embedding tokenizer
    #    中文切分；行中 BOM 删除（行首第 1 步已 lstrip）；NBSP→空格；全角空格→半角
    text = text.replace("​", "").replace("‌", "").replace("‍", "")
    text = text.replace("﻿", "")
    text = text.replace(" ", " ")
    text = text.replace("　", " ")
    # 6. 压连续空行为最多两个换行（保留段落分隔）
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 7. 去行尾空白
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return text.strip()


# ==================== PII 脱敏（自研） ====================

# 手机号：11 位，1 开头第二位 3-9；前后非数字防超长数字串内部命中
_RE_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 邮箱：本地部分首字符保留，域名完整
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 身份证 18 位：17 数字 + 末位数字/X（前后非数字）。USCC（统一社会信用代码）也是
# 18 位纯数字（合成数据 generators.py 生成），用第 7-14 位出生日期校验区分——
# USCC 无日期特征，天然跳过。
_RE_ID18 = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 身份证 15 位：纯数字，第 7-12 位为出生日期（YYMMDD）校验兜底
_RE_ID15 = re.compile(r"(?<!\d)\d{15}(?!\d)")
# USCC 上下文保护：企业资质段落里的 18 位代码整体占位，先于身份证规则执行——
# 即便 18 位数字第 7-14 位碰巧是合法日期，也以"信用代码上下文"为准，避免误伤。
_RE_USCC = re.compile(r"(?:统一社会信用代码|信用代码)\s*[:：]?\s*[0-9A-Za-z]{18}")


def _mask(s: str) -> str:
    """掩码：保留前 3 后 4，中间 *，长度与原值一致（不破坏 LLM 对文本结构的理解）。"""
    if len(s) <= 7:
        return s[0] + "*" * (len(s) - 1)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def _is_birth18(s: str) -> bool:
    """18 位串第 7-14 位是合法出生日期（1900-2100）→ 判定身份证形态。"""
    try:
        d = datetime.strptime(s[6:14], "%Y%m%d")
    except ValueError:
        return False
    return 1900 <= d.year <= 2100


def _is_birth15(s: str) -> bool:
    """15 位串第 7-12 位是合法出生日期（YYMMDD）→ 判定身份证形态。"""
    try:
        datetime.strptime(s[6:12], "%y%m%d")
        return True
    except ValueError:
        return False


def _mask_email(m: re.Match) -> str:
    """邮箱掩码：本地部分首字符 + *** + @域名（域名完整，便于 LLM 识别机构）。"""
    addr = m.group(0)
    at = addr.rfind("@")
    local, domain = addr[:at], addr[at:]
    return (local[0] + "***" + domain) if local else "***" + domain


def redact_pii(text: str) -> str:
    """PII 脱敏：手机号/邮箱/身份证 → 掩码；USCC 上下文整体占位。

    顺序：先保护 USCC（企业代码优先于身份证识别），再脱手机/邮箱/身份证。
    无 PII 文本原样返回（幂等）。
    """
    if not text:
        return text
    text = _RE_USCC.sub("【统一社会信用代码】", text)
    text = _RE_MOBILE.sub(lambda m: _mask(m.group(0)), text)
    text = _RE_EMAIL.sub(_mask_email, text)
    text = _RE_ID18.sub(
        lambda m: _mask(m.group(0)) if _is_birth18(m.group(0)) else m.group(0), text
    )
    text = _RE_ID15.sub(
        lambda m: _mask(m.group(0)) if _is_birth15(m.group(0)) else m.group(0), text
    )
    return text


def clean_bid_text(text: str) -> str:
    """LLM 输入侧清洗唯一入口：先规范化（clean_text）再 PII 脱敏（redact_pii）。

    挂载点：检索结果组装进 <bid_content>/<structured_data> 前（reviews.py /
    review_service.py）。幂等，无 PII 的干净文本近乎原样返回。
    """
    return redact_pii(clean_text(text))
