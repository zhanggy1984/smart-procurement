"""app/main.py 启动安全校验单测（自查：JWT/FERNET 弱默认密钥 fail loud）。

- _jwt_secret_secure：非空、非公开默认值（change-me-in-production）、≥32 随机字符
  才通过。不满足 → 启动 exit(1)，防漏配 JWT_SECRET_KEY 时用公开已知密钥签 token。
- is_fernet_key_secure：合法 32 url-safe base64 或显式 auto 豁免，空/非法拒绝。
  不满足 → 启动 exit(1)，防随机 key 重启后历史加密数据不可解密。
"""

from app.core.crypto import is_fernet_key_secure
from app.main import _jwt_secret_secure


def test_jwt_secret_secure_rejects_default():
    """公开已知默认值必须拒绝（config.py 默认就是它）。"""
    assert _jwt_secret_secure("change-me-in-production") is False


def test_jwt_secret_secure_rejects_empty_and_none():
    """缺失（空/None）必须拒绝。"""
    assert _jwt_secret_secure("") is False
    assert _jwt_secret_secure(None) is False


def test_jwt_secret_secure_rejects_short():
    """<32 字符视为弱密钥拒绝（含 31 边界）。"""
    assert _jwt_secret_secure("short-key") is False
    assert _jwt_secret_secure("a" * 31) is False


def test_jwt_secret_secure_accepts_strong():
    """≥32 随机字符通过（32 边界 + 任意字符集）。"""
    assert _jwt_secret_secure("a" * 32) is True
    assert _jwt_secret_secure("k3y-9f2c!@#v$7x4p" * 2) is True


def test_fernet_key_secure_accepts_valid():
    """合法 32 url-safe base64 key 必须通过（Fernet 标准生成）。"""
    from cryptography.fernet import Fernet

    valid = Fernet.generate_key().decode()
    assert is_fernet_key_secure(valid) is True


def test_fernet_key_secure_rejects_empty_and_none():
    """缺失（空/None）必须拒绝——生产漏配启动即 exit。"""
    assert is_fernet_key_secure("") is False
    assert is_fernet_key_secure(None) is False


def test_fernet_key_secure_rejects_invalid():
    """非法格式（非 url-safe base64）必须拒绝。"""
    assert is_fernet_key_secure("short-key") is False
    assert is_fernet_key_secure("a" * 44) is False  # 长度对但非 base64


def test_fernet_key_secure_accepts_auto():
    """显式 auto = 开发/演示豁免（进程内随机 key），必须通过。"""
    assert is_fernet_key_secure("auto") is True
