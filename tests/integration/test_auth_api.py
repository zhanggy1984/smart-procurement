"""P7.3 认证 API 集成测试（task.md #1）+ 自查 #6 安全加固。

- 正确凭据 → 200 + JWT（access 30min / refresh 7d）
- 错误密码 → 401（防枚举，不区分账号不存在）
- 缺失 username / password → 422（pydantic 校验）
- refresh_token 换新 access_token → 200；无效 refresh → 401
- 无凭证访问受保护接口 → 401
- 自查 #6：连续失败限流锁定 → 429；refresh 轮换旧 token 二次使用 → 401；
  首登强改：must_change_password=True 业务 API 403、改密后恢复
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

LOGIN_URL = "/api/v1/auth/login"


async def _login(client, username: str, password: str):
    return await client.post(LOGIN_URL, json={"username": username, "password": password})


@pytest.fixture(autouse=True)
async def _flush_auth_redis_keys():
    """每个认证测试前清空限流/轮换 Redis 键。

    conftest 每个测试重置 Redis 单例，但 key 空间是共享的；不清会跨测试/
    跨会话累计失败计数导致偶发 429/401（自查 #6 引入限流与轮换后必需）。
    """
    from app.core.redis import flush_keys

    # 自查 #6 键空间：auth:ip:{ip}:req/cooldown（IP 限流）+ refresh:user:{uid}:{jti}
    # （轮换白名单，'1'/'USED'）+ refresh:revoke:{uid}（全量吊销标记）
    for prefix in ("auth:ip:", "refresh:user:", "refresh:revoke:"):
        try:
            await flush_keys(prefix)
        except Exception:  # noqa: BLE001  Redis 故障不阻断测试
            pass
    yield


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


# ==================== 自查 #6：登录限流 / refresh 轮换 / 首登强改 ====================


@pytest.mark.asyncio
async def test_login_ip_rate_limit_exceeds_429(client):
    """IP 级限流（S4）：同一 IP 60s 窗口内第 6 次尝试（即使密码正确）→ 429 + 冷却。

    5 次失败均为 401（未超阈值）；第 6 次无论密码对错 → 429。
    """
    for _ in range(5):
        r = await _login(client, "itest_lock", "Wrong@999")
        assert r.status_code == 401
    r6 = await _login(client, "itest_lock", "Smart@2026")
    assert r6.status_code == 429
    assert "次数过多" in r6.json()["detail"]
    # 冷却期内继续尝试（含正确密码）仍 429
    r7 = await _login(client, "itest_lock", "Smart@2026")
    assert r7.status_code == 429


@pytest.mark.asyncio
async def test_refresh_rotates_and_old_token_rejected(client):
    """refresh 轮换：首次换新返还新 refresh_token；旧 refresh 二次使用 → 401。"""
    login = await _login(client, "admin", "Smart@2026")
    refresh = login.json()["refresh_token"]
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r1.status_code == 200
    assert r1.json()["access_token"]
    assert r1.json().get("refresh_token")  # 轮换返还新 refresh_token
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 401
    assert "已失效" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_all_user_tokens(client):
    """S5 复用检测：旧 refresh 二次使用 → 该用户全部 refresh token 被吊销。

    轮换后同用户的新 refresh_token 本应有效；旧 token 复用触发全量吊销后，
    新 token 也必须 401（不是只作废旧 token 本身）。
    """
    login = await _login(client, "admin", "Smart@2026")
    refresh1 = login.json()["refresh_token"]
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh1})
    assert r1.status_code == 200
    refresh2 = r1.json()["refresh_token"]
    # 复用旧 refresh1 → 泄露信号 → 撤销 admin 全部 refresh token
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh1})
    assert r2.status_code == 401
    # refresh2（轮换新发的令牌）一并被吊销 → 401
    r3 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh2})
    assert r3.status_code == 401
    assert "已失效" in r3.json()["detail"]


@pytest.mark.asyncio
async def test_change_password_flow(client, exp_headers):
    """改密：旧密码错误 400；正确 → 200；新密码可登录。"""
    r_bad = await client.post(
        "/api/v1/auth/change-password", headers=exp_headers,
        json={"old_password": "Wrong@999", "new_password": "New@Pass123"},
    )
    assert r_bad.status_code == 400
    r_weak = await client.post(
        "/api/v1/auth/change-password", headers=exp_headers,
        json={"old_password": "Smart@2026", "new_password": "weak"},
    )
    assert r_weak.status_code == 400  # 复杂度不达标
    r_ok = await client.post(
        "/api/v1/auth/change-password", headers=exp_headers,
        json={"old_password": "Smart@2026", "new_password": "New@Pass123"},
    )
    assert r_ok.status_code == 200
    r_login = await _login(client, "exp1", "New@Pass123")
    assert r_login.status_code == 200


@pytest.mark.asyncio
async def test_must_change_password_blocks_business_api(client):
    """首登强改：must_change_password=True 账号登录成功但业务 API 403；改密后恢复。"""
    from app.core import security
    from app.core.database import session_factory
    from app.models.user import Role, User

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_factory() as s:
        s.add(User(
            user_id="ITEST-U-FORCED", username="itest_forced",
            password_hash=security.hash_password("Smart@2026"),
            role=Role.ADMIN, display_name="强制改密测试",
            email="forced@itest.local", is_active=True,
            must_change_password=True, created_at=now, updated_at=now,
        ))
        await s.commit()

    r_login = await _login(client, "itest_forced", "Smart@2026")
    assert r_login.status_code == 200
    token = r_login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 业务 API（任意登录端点）→ 403 首登强改拦截
    r_blocked = await client.get("/api/v1/projects/ITEST-NO-PROJECT", headers=headers)
    assert r_blocked.status_code == 403
    assert "修改初始密码" in r_blocked.json()["detail"]

    # 改密端点本身豁免（否则永远改不了密码）
    r_change = await client.post(
        "/api/v1/auth/change-password", headers=headers,
        json={"old_password": "Smart@2026", "new_password": "New@Pass123"},
    )
    assert r_change.status_code == 200

    # 改密后 403 解除：业务 API 进入正常流程（项目不存在 → 404 而非 403）
    r_ok = await client.get("/api/v1/projects/ITEST-NO-PROJECT", headers=headers)
    assert r_ok.status_code == 404
