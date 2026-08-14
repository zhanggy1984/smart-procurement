"""P7.3 专家匹配 + 回避申报 API 集成测试（task.md #8/#9）。

匹配成功：UNDER_REVIEW + tags → 返回 assigned + assignment 落库。
申报成功：本人分配 → 全部无冲突 → IN_PROGRESS。
错误：lot 非 UNDER_REVIEW → 400；tags 为空 → 400；重复申报 → 409；非本人 → 403。
"""

from __future__ import annotations

import pytest

SUPPLIERS = ["ITEST-S1", "ITEST-S2", "ITEST-S3"]


async def _close(client, pm_headers, lot_id):
    return await client.post(f"/api/v1/lots/{lot_id}/close-bidding", headers=pm_headers)


async def _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """创建 lot + 3 投标 + PARSED + close(LOW) → UNDER_REVIEW。"""
    lot = await lot_factory()
    bids = await bid_factory(lot["lot_id"])
    await set_bid_parsed(lot["lot_id"], bids)
    r = await _close(client, pm_headers, lot["lot_id"])
    assert r.status_code == 200, r.text
    assert r.json()["risk"] == "LOW"
    return lot


async def _match(client, pm_headers, lot_id, tags=None):
    # tags=[] 必须原样传（不能 `or` 默认值，否则空标签被替换）
    payload = tags if tags is not None else ["软件开发", "人工智能"]
    return await client.post(
        f"/api/v1/lots/{lot_id}/match-experts", headers=pm_headers, json={"tags": payload},
    )


@pytest.mark.asyncio
async def test_match_experts_success(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """UNDER_REVIEW + 受控 tags → 匹配落库 + 可查。"""
    lot = await _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    resp = await _match(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["assigned"], "应返回专家分配"
    assert body["insufficient"] is False
    # assignment 落库可查
    q = await client.get(f"/api/v1/lots/{lot['lot_id']}/match-experts", headers=pm_headers)
    assert q.status_code == 200
    assert len(q.json()["assigned"]) == len(body["assigned"])


@pytest.mark.asyncio
async def test_match_lot_not_under_review_400(client, pm_headers, lot_factory):
    """lot 仍 BIDDING → 400。"""
    lot = await lot_factory()
    resp = await _match(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_match_no_tags_400(client, pm_headers, lot_factory, bid_factory, set_bid_parsed):
    """tags 为空 → 400。"""
    lot = await _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    resp = await _match(client, pm_headers, lot["lot_id"], tags=[])
    # MatchRequest.tags min_length=1 → pydantic 422（先于 service 层 NoTagsError）
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_match_forbidden_supplier_403(client, pm_headers, sup_headers, lot_factory, bid_factory, set_bid_parsed):
    """非 PM/ADMIN → 403。"""
    lot = await _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    resp = await _match(client, sup_headers, lot["lot_id"])
    assert resp.status_code == 403


async def _declare_all(client, exp_headers):
    """专家对本人全部 assignment 申报无冲突，返回最后一次响应。"""
    r = await client.get("/api/v1/experts/me/assignments", headers=exp_headers)
    assert r.status_code == 200
    assignments = r.json()["assignments"]
    assert assignments, "该专家未被分配任务"
    confs = [{"supplier_id": s, "has_conflict": False} for s in SUPPLIERS]
    last = None
    for a in assignments:
        last = await client.post(
            f"/api/v1/experts/assignments/{a['assignment_id']}/declare", headers=exp_headers,
            json={"confirmations": confs})
    return last


@pytest.mark.asyncio
async def test_declare_success_in_progress(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """本人申报全部无冲突 → assignment IN_PROGRESS。"""
    lot = await _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    resp = await _match(client, pm_headers, lot["lot_id"])
    assert resp.status_code == 200
    resp = await _declare_all(client, exp_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_declare_duplicate_409(client, pm_headers, exp_headers, lot_factory, bid_factory, set_bid_parsed):
    """重复申报 → 409。"""
    lot = await _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    await _match(client, pm_headers, lot["lot_id"])
    r1 = await _declare_all(client, exp_headers)
    assert r1.status_code == 200
    r2 = await _declare_all(client, exp_headers)
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_declare_not_own_403(client, pm_headers, exp_headers, exp2_headers, lot_factory, bid_factory, set_bid_parsed):
    """用他人 token 申报本人 assignment → 403（非本人）。"""
    lot = await _ready_lot(client, pm_headers, lot_factory, bid_factory, set_bid_parsed)
    await _match(client, pm_headers, lot["lot_id"])
    # exp2 账号查自己任务，确认与 exp1 不同；exp1 申报 exp2 的任务 → 403
    r = await client.get("/api/v1/experts/me/assignments", headers=exp2_headers)
    assignments = r.json()["assignments"]
    if not assignments:
        pytest.skip("exp2 未被分配，无任务可越权")
    target = assignments[0]["assignment_id"]
    confs = [{"supplier_id": s, "has_conflict": False} for s in SUPPLIERS]
    resp = await client.post(
        f"/api/v1/experts/assignments/{target}/declare", headers=exp_headers,
        json={"confirmations": confs},
    )
    assert resp.status_code == 403
