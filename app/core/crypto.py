"""数据保护：身份证号加密/哈希 + 日志脱敏（P1.2）。

分层：
- Fernet（对称加密）：身份证号可逆加密，存 id_number_encrypted
- SHA-256：身份证号哈希，存 id_number_hash（去重匹配用，不可逆）
- redact()：日志入参脱敏的**唯一**入口（solution.md 日志规范：脱敏规则严禁散落各处）

Fernet key 说明：
- 生产：必须配置合法 FERNET_KEY（32 url-safe base64），否则启动 fail loud（main.py
  lifespan 校验 exit(1)），杜绝"进程内随机 key → 重启后历史加密数据不可解密"。
- 开发/演示/CI：显式设 `FERNET_KEY=auto` 才允许进程内随机 key（可逆数据无所谓）。
  留空与 auto 等价，但生产漏配时留空会被启动校验拦截（auto 是显式声明）。
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


def is_fernet_key_secure(key: str) -> bool:
    """FERNET 密钥校验：合法 32 url-safe base64，或显式 `auto` 豁免。

    返回 True：配置可用（合法 key，或 auto=进程内随机兜底）。
    返回 False：空/非法 key——生产漏配/配错时启动拦截（fail loud），防历史
    加密数据（专家身份证）在重启后不可解密。
    """
    if not key:
        return False
    if key == "auto":
        return True
    try:
        Fernet(key.encode())
        return True
    except (ValueError, TypeError):
        return False


def _get_fernet() -> Fernet:
    """懒加载 Fernet 实例。

    key 未配置或显式 `auto`：进程内随机 key（开发/演示兜底，warning 提示重启后
    历史加密数据不可解密）。key 已配置但非法：抛 ValueError——不静默降级随机
    （生产配置错误属致命配置缺陷，main.py lifespan 已在启动时 fail loud 拦截）。
    """
    global _fernet
    if _fernet is None:
        key = settings.fernet_key
        if not key or key == "auto":
            import logging

            logging.getLogger(__name__).warning(
                "FERNET_KEY 未配置或显式 auto：使用进程内随机 key，"
                "重启后历史加密数据不可解密"
            )
            _fernet = Fernet(Fernet.generate_key())
        else:
            try:
                _fernet = Fernet(key.encode())
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"FERNET_KEY 非法（须为 32 url-safe base64），拒绝使用随机 key: {e}"
                ) from e
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
