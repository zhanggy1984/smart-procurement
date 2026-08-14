"""数据保护：身份证号加密/哈希 + 日志脱敏（P1.2）。

分层：
- Fernet（对称加密）：身份证号可逆加密，存 id_number_encrypted
- SHA-256：身份证号哈希，存 id_number_hash（去重匹配用，不可逆）
- redact()：日志入参脱敏的**唯一**入口（solution.md 日志规范：脱敏规则严禁散落各处）

Fernet key 说明：生产环境必须配置 FERNET_KEY（否则每次进程启动生成随机 key，
历史加密数据将无法解密）。开发环境（合成数据）允许留空自动生成。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from cryptography.fernet import Fernet

from app.core.config import settings

# 日志脱敏：对敏感字段做处理。身份证号脱敏保留前 3 后 4，其余字段统一 ***
_ID_MASK_CHARS = 4

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """懒加载 Fernet 实例。

    key 未配置或非法（非 32 url-safe base64）时降级为进程内随机 key（开发环境兜底）：
    生产未配置/配置错误属配置缺陷，warning 级提示（不阻断主链路，但重启后
    历史加密数据将不可解密）。
    """
    global _fernet
    if _fernet is None:
        try:
            _fernet = Fernet(settings.fernet_key.encode())
        except (ValueError, TypeError):
            import logging

            logging.getLogger(__name__).warning(
                "FERNET_KEY 未配置或非法（须为 32 url-safe base64），"
                "使用进程内随机 key，重启后历史加密数据不可解密"
            )
            _fernet = Fernet(Fernet.generate_key())
    return _fernet


def encrypt_id_number(id_number: str) -> str:
    """身份证号 Fernet 加密（UTF-8 → token 字符串）。"""
    return _get_fernet().encrypt(id_number.encode("utf-8")).decode()


def decrypt_id_number(encrypted: str) -> str:
    """解密身份证号（与 encrypt_id_number 配对）。"""
    return _get_fernet().decrypt(encrypted.encode("utf-8")).decode()


def hash_id_number(id_number: str) -> str:
    """身份证号 SHA-256 十六进制哈希（去重匹配，不可逆）。"""
    return hashlib.sha256(id_number.encode("utf-8")).hexdigest()


def redact(value: object) -> str:
    """日志脱敏入口。身份证号保留前 3 后 4，其余一律 `***`。

    调用方对 password / token / api_key 等字段直接 redact()；
    身份证号可用 redact()（自动识别 15-18 位数字形态）。
    """
    if value is None:
        return "null"
    text = str(value)
    # 15/18 位身份证号形态：保留前 3 + 后 4
    if text.isdigit() and len(text) in (15, 18):
        return f"{text[:3]}{'*' * (len(text) - _ID_MASK_CHARS - 3)}{text[-_ID_MASK_CHARS:]}"
    return "***"


def generate_id(prefix: str) -> str:
    """生成全局业务 ID：前缀 + 随机 12 位大写 hex（UUID4 截断，跨存储稳定）。"""
    return f"{prefix}-{secrets.token_hex(6).upper()}"
