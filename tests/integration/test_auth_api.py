"""P7.3 认证 API 集成测试（task.md #1）。

- 正确凭据 → 200 + JWT（access 30min / refresh 7d）
- 错误密码 → 401（防枚举，不区分账号不存在）
- 缺失 username / password → 422（pydantic 校验）
- refresh_token 换新 access_token → 200；无效 refresh → 401
- 无凭证访问受保护接口 → 401
"""

from __future__ import annotations

import pytest

LOGIN_URL = "/api/v1/auth/login"


async def _login(client, username: str, password: str):
    return await client.post(LOGIN_URL, json={"username": username, "password": password})


@pytest.mark.asyncio
async def test_login_success_returns_jwt(client):
    """正确凭据 → 200 + access/refresh token + 用户信息。"""
    resp = await _login(client, "admin", "Smart@2026")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == "ADMIN"


@pytest.mark.asyncio
async def test_login_wrong_password_401(client):
    """密码错误 → 401（不区分账号不存在，防枚举）。"""
    resp = await _login(client, "admin", "Wrong@999")
    assert resp.status_code == 401
    assert "用户名或密码错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_login_unknown_user_401(client):
    """账号不存在 → 401（与密码错误同一文案）。"""
    resp = await _login(client, "no_such_user", "Smart@2026")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_missing_username_422(client):
    """缺失 username → 422。"""
    resp = await client.post(LOGIN_URL, json={"password": "Smart@2026"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_login_missing_password_422(client):
    """缺失 password → 422。"""
    resp = await client.post(LOGIN_URL, json={"username": "admin"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_refresh_returns_new_access(client):
    """refresh_token → 新 access_token。"""
    login = await _login(client, "admin", "Smart@2026")
    refresh = login.json()["refresh_token"]
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


@pytest.mark.asyncio
async def test_refresh_invalid_token_401(client):
    """无效 refresh_token → 401。"""
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_without_token_401(client):
    """无凭证访问受保护接口 → 401。"""
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_with_valid_token_ok(client, admin_headers):
    """带合法 token 访问 → 非 401。"""
    resp = await client.get("/api/v1/projects", headers=admin_headers)
    assert resp.status_code == 200
