"""E2E-5 供应商黑名单级联流（task.md 5 步）。

供应商A 参与 LOT-01 评审(UNDER_REVIEW) + LOT-02 已定标(AWARDED) → 管理员拉黑 →
LOT-01 关联评审 SUSPENDED、LOT-02 不受影响 → PM 收到黑名单通知。

前置：拉黑无前端入口（API updateSupplierStatus），LOT-02 AWARDED 用 DB 直造
（级联逻辑集成测试已覆盖真实链路，此处验证真实环境拉黑触发后的级联）。
"""

from __future__ import annotations

import pytest

from conftest import _sql
from helpers import (create_project_full, expert_username, import_experts,
                     import_suppliers, upload_bids, wait_parsed)

EXPERT_ROWS = [
    {"expert_id": "E2E-EXP1", "name": "E2E专家甲", "region": "西北", "exp": 15, "tags": ["软件开发"]},
]
SUPPLIER_ROWS = [
    {"supplier_id": "E2E-SUP1", "name": "E2E供应商甲", "code": "913100000000000011"},
    {"supplier_id": "E2E-SUP2", "name": "E2E供应商乙", "code": "913100000000000012"},
    {"supplier_id": "E2E-SUP3", "name": "E2E供应商丙", "code": "913100000000000013"},
]


def _under_review_with_review(pm_api, admin_api, lot_tag: str) -> tuple[str, str]:
    """造 UNDER_REVIEW 标段 + 1 条 CONFIRMED 评审，返回 (lot_id, review_id)。"""
    import_experts(admin_api, EXPERT_ROWS)
    import_suppliers(admin_api, SUPPLIER_ROWS)
    project_id, lot_id = create_project_full(pm_api)
    # 黑名单级联通知发给项目负责人（managed_by），E2E 用 PM 承接断言
    _sql("UPDATE project SET managed_by='E2E-U-PM' WHERE project_id=:p", {"p": project_id})
    bid_ids = upload_bids(pm_api, lot_id, ["E2E-SUP1", "E2E-SUP2", "E2E-SUP3"])
    wait_parsed(lot_id, bid_ids)
    r = pm_api.post(f"/lots/{lot_id}/close-bidding")
    assert r.status_code == 200, r.text
    r = pm_api.post(f"/lots/{lot_id}/match-experts", json={"tags": ["软件开发"]})
    assert r.status_code == 200, r.text
    rows = _sql("SELECT id FROM lot_expert_assignment "
                "WHERE expert_id='E2E-EXP1' AND lot_id=:l", {"l": lot_id})
    suppliers = _sql("SELECT DISTINCT supplier_id FROM bid_document WHERE lot_id=:l", {"l": lot_id})
    from conftest import Api
    exp = Api("E2E-DUMMY", expert_username("E2E-EXP1"))
    try:
        confs = [{"supplier_id": s[0], "has_conflict": False, "relation_type": None,
                  "relation_detail": None} for s in suppliers]
        r = exp.post(f"/experts/assignments/{rows[0][0]}/declare", json={"confirmations": confs})
        assert r.status_code == 200, r.text
        dim = _sql("SELECT dimension_id FROM scoring_dimension WHERE lot_id=:l LIMIT 1",
                   {"l": lot_id})[0][0]
        r = exp.post("/reviews", json={"bid_id": bid_ids[0], "dimension_id": dim})
        assert r.status_code == 201, r.text
        rid = r.json()["review_id"]
        r = exp.put(f"/reviews/{rid}/score", json={"score": 20.0, "comment": "E2E黑名单级联评审",
                                                   "ai_suggestion": None})
        assert r.status_code == 200, r.text
        r = exp.post(f"/reviews/{rid}/submit")
        assert r.status_code == 200, r.text
    finally:
        exp.close()
    return lot_id, rid


@pytest.mark.e2e
def test_e2e_05_blacklist_cascade(admin_api, pm_api):
    # LOT-01 UNDER_REVIEW + CONFIRMED 评审
    lot1, review1 = _under_review_with_review(pm_api, admin_api, "L1")

    # LOT-02 AWARDED（DB 直造前置状态：project/lot AWARDED + review CONFIRMED；
    # 级联豁免按 project.status=AWARDED 判定，系统无独立 award_result 表）
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _sql("INSERT INTO project (project_id, project_code, name, type, region, budget, status, created_at, updated_at) "
         "VALUES ('E2E-PJ-AW', 'E2E-PJ-AW', 'E2E已定标项目', 'SERVICE', '西北', 2000000, 'AWARDED', :n, :n)",
         {"n": now})
    _sql("INSERT INTO lot (lot_id, project_id, lot_code, name, budget, status, created_at, updated_at) "
         "VALUES ('E2E-LOT-AW', 'E2E-PJ-AW', 'E2E-LOT-AW', 'E2E已定标标段', 500000, 'AWARDED', :n, :n)",
         {"n": now})
    _sql("INSERT INTO bid_document (bid_id, lot_id, supplier_id, status, bid_amount, created_at, updated_at) "
         "VALUES ('E2E-BID-AW', 'E2E-LOT-AW', 'E2E-SUP1', 'FROZEN', 1000000, :n, :n)", {"n": now})
    _sql("INSERT INTO expert_review (review_id, expert_id, bid_id, dimension_id, score, comment, "
         "status, created_at, updated_at) VALUES ('E2E-REV-AW', 'E2E-EXP1', 'E2E-BID-AW', 'E2E-DIM-AW', "
         "18, 'E2E已定标评审', 'CONFIRMED', :n, :n)", {"n": now})

    # 管理员拉黑 SUP1（无前端 UI，走 API）
    r = admin_api.put("/suppliers/E2E-SUP1/status", json={"blacklisted": True})
    assert r.status_code == 200, r.text

    # 级联断言：LOT-01 评审 SUSPENDED；LOT-02(AWARDED) 不受影响
    st = _sql("SELECT status FROM expert_review WHERE review_id=:r", {"r": review1})[0][0]
    assert st == "SUSPENDED", f"LOT-01 评审应 SUSPENDED: {st}"
    st2 = _sql("SELECT status FROM expert_review WHERE review_id='E2E-REV-AW'")[0][0]
    assert st2 == "CONFIRMED", f"AWARDED 评审不应被挂起: {st2}"

    # PM 收到黑名单通知
    n = _sql("SELECT COUNT(*) FROM notification WHERE user_id='E2E-U-PM'")[0][0]
    assert n >= 1, "PM 未收到黑名单通知"

    # 解除拉黑 → SUSPENDED 评审还原
    r = admin_api.put("/suppliers/E2E-SUP1/status", json={"blacklisted": False})
    assert r.status_code == 200, r.text
    st3 = _sql("SELECT status FROM expert_review WHERE review_id=:r", {"r": review1})[0][0]
    assert st3 == "CONFIRMED", f"解除拉黑应还原评审: {st3}"
