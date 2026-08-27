"""app/main.py 启动安全校验单测（自查第 1 项：JWT 弱默认密钥 fail loud）。

_jwt_secret_secure：非空、非公开默认值（change-me-in-production）、≥32 随机字符
才通过。不满足 → 启动 exit(1)，防漏配 JWT_SECRET_KEY 时用公开已知密钥签 token。
"""

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
