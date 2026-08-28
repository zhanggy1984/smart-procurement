"""P7.2 专家管理服务单元测试（task.md：P1.4 导入 + 状态）。

覆盖：
- _validate_id_number：15/18 位（末位 X）合法性
- import_experts：成功导入（建账号+outbox+Neo4j）；校验失败 ExpertImportError
  （姓名/身份证/地区/标签/年限）；库内身份证重复 → skipped 幂等
- update_status：三态 + 同步登录账号启用状态；非法状态 / 404
- delete_expert：逻辑删除 → INACTIVE
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.crypto import hash_id_number
from app.services.expert_service import (
    ExpertImportError,
    ExpertNotFoundError,
    ExpertStatus,
    InvalidExpertStatusError,
    _validate_id_number,
    delete_expert,
    import_experts,
    update_status,
)

_ID = "110101199001011234"  # 18 位合法


def test_validate_id_number():
    """身份证号：15 位纯数字、18 位（末位数字或 X）合法；其余非法。"""
    assert _validate_id_number("110101199001011234") is True
    assert _validate_id_number("11010119900101123X") is True  # 末位校验码 X
    assert _validate_id_number("11010119900101123x") is True  # 小写 x
    assert _validate_id_number("12345") is False
    assert _validate_id_number("11010119900101123Y") is False  # 末位非数字/X
    assert _validate_id_number("") is False


def _scalar_all(items):
    """session.scalars(...) 返回 ScalarResult，实现取 .all()（同步方法）。"""
    r = MagicMock()
    r.all.return_value = items
    return r


def _ok_row(**kw):
    row = {"姓名": "张三", "身份证号": _ID, "地区": "华中", "专业标签": "软件开发",
           "从业年限": "10", "单位": "某研究所", "邮箱": "a@b.com", "电话": "13800138000"}
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_import_experts_success():
    """合法行 → 建登录账号 + expert + 标签 + outbox + Neo4j。"""
    session = AsyncMock()
    session.scalars.side_effect = [_scalar_all([]), _scalar_all([])]  # 无库内 hash / 用户名
    with patch("app.services.expert_service.write_outbox_event", new=AsyncMock()) as outbox, \
         patch("app.services.expert_service.neo4j_sync.upsert_expert", new=AsyncMock()) as upsert:
        res = await import_experts(session, [_ok_row()], operator_id="U-1")
    assert res == {"imported": 1, "skipped": 0}
    session.commit.assert_awaited_once()
    outbox.assert_awaited_once()
    upsert.assert_awaited_once()
    # 登录用户名：expert_01（P6 登录对齐）；add_all 首次调用参数是 new_users 列表
    new_users = session.add_all.call_args_list[0].args[0]
    assert new_users[0].username == "expert_01"


@pytest.mark.asyncio
async def test_import_experts_skips_duplicate_id():
    """库内身份证已存在 → skipped（幂等重导不报错）。"""
    session = AsyncMock()
    session.scalars.side_effect = [_scalar_all([hash_id_number(_ID)]), _scalar_all([])]
    res = await import_experts(session, [_ok_row()], operator_id="U-1")
    assert res == {"imported": 0, "skipped": 1}
    # commit 无条件执行（空提交无副作用），幂等重导不报错即正确


@pytest.mark.asyncio
async def test_import_experts_validation_errors():
    """姓名/身份证/地区/标签/年限非法 → ExpertImportError 带行级 errors。"""
    session = AsyncMock()
    session.scalars.side_effect = [_scalar_all([]), _scalar_all([])]
    rows = [
        _ok_row(姓名=""),                                  # 姓名缺失
        _ok_row(身份证号="12345"),                          # 身份证非法
        _ok_row(地区="火星"),                              # 地区非受控
        _ok_row(专业标签="火星标签"),                       # 标签非受控
        _ok_row(从业年限="abc"),                           # 年限非整数
    ]
    with pytest.raises(ExpertImportError) as ei:
        await import_experts(session, rows, operator_id="U-1")
    fields = {e["field"] for e in ei.value.errors}
    assert {"姓名", "身份证号", "地区", "专业标签", "从业年限"} <= fields
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_status_blacklists_and_disables_login():
    """ACTIVE → BLACKLISTED：同步禁用登录账号（user.is_active=False）。"""
    session = AsyncMock()
    expert = MagicMock()
    expert.expert_id = "EXP-1"
    expert.name = "张三"
    expert.user_id = "U-1"
    user = MagicMock()
    user.is_active = True
    session.get.side_effect = [expert, user]
    with patch("app.services.expert_service.neo4j_sync.upsert_expert", new=AsyncMock()):
        await update_status(session, "EXP-1", "BLACKLISTED", operator_id="U-1")
    assert expert.status == "BLACKLISTED"
    assert user.is_active is False
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_status_invalid_status():
    """状态非法 → 422。"""
    session = AsyncMock()
    with pytest.raises(InvalidExpertStatusError):
        await update_status(session, "EXP-1", "FROZEN", operator_id="U-1")


@pytest.mark.asyncio
async def test_update_status_not_found():
    """专家不存在 → 404。"""
    session = AsyncMock()
    session.get.return_value = None
    with pytest.raises(ExpertNotFoundError):
        await update_status(session, "EXP-X", "ACTIVE", operator_id="U-1")


@pytest.mark.asyncio
async def test_delete_expert_logical_inactive():
    """逻辑删除 → INACTIVE（保留审计链路，不物理删除）。"""
    session = AsyncMock()
    expert = MagicMock()
    expert.expert_id = "EXP-1"
    expert.user_id = "U-1"
    user = MagicMock()
    session.get.side_effect = [expert, user]
    with patch("app.services.expert_service.neo4j_sync.upsert_expert", new=AsyncMock()):
        done = await delete_expert(session, "EXP-1", operator_id="U-1")
    assert done.status == ExpertStatus.INACTIVE
    assert user.is_active is False
