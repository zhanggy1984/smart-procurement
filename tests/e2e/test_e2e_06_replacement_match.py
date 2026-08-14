"""E2E-6 专家回避申报→自动补匹配流（task.md 6 步）。

PM 匹配 5 专家 → 专家A 申报"曾在供应商甲任职" → assignment CONFLICT_DECLARED
→ 系统自动补入备选专家F → 专家F 申报 → 进入评审。

验收：A 申报冲突后其 assignment 转 CONFLICT_DECLARED，补入 F（待申报）。
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL, login, _sql
from helpers import (create_project_full, expert_username, import_experts,
                     import_suppliers, upload_bids, wait_parsed)

EXPERT_ROWS = [
    {"expert_id": "E2E-EXP-A", "name": "E2E申报专家甲", "region": "西北", "exp": 15, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-B", "name": "E2E专家乙", "region": "西北", "exp": 12, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-C", "name": "E2E专家丙", "region": "西北", "exp": 10, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-D", "name": "E2E专家丁", "region": "西北", "exp": 8, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-E", "name": "E2E专家戊", "region": "西北", "exp": 6, "tags": ["软件开发"]},
    # 备选池专家 F：tag 匹配、未入选前 5
    {"expert_id": "E2E-EXP-F", "name": "E2E备选专家己", "region": "西北", "exp": 9, "tags": ["软件开发"]},
]
SUPPLIER_ROWS = [
    {"supplier_id": "E2E-SUP1", "name": "E2E供应商甲", "code": "913100000000000011"},
    {"supplier_id": "E2E-SUP2", "name": "E2E供应商乙", "code": "913100000000000012"},
    {"supplier_id": "E2E-SUP3", "name": "E2E供应商丙", "code": "913100000000000013"},
]


@pytest.mark.e2e
def test_e2e_06_replacement_match(page, admin_api, pm_api):
    import_experts(admin_api, EXPERT_ROWS)
    import_suppliers(admin_api, SUPPLIER_ROWS)
    _, lot_id = create_project_full(pm_api)
    bid_ids = upload_bids(pm_api, lot_id, ["E2E-SUP1", "E2E-SUP2", "E2E-SUP3"])
    wait_parsed(lot_id, bid_ids)
    r = pm_api.post(f"/lots/{lot_id}/close-bidding")
    assert r.status_code == 200, r.text
    r = pm_api.post(f"/lots/{lot_id}/match-experts", json={"tags": ["软件开发"]})
    assert r.status_code == 200, r.text

    rows = _sql("SELECT id FROM lot_expert_assignment "
                "WHERE expert_id='E2E-EXP-A' AND lot_id=:l", {"l": lot_id})
    assert rows
    assignment_id = rows[0][0]

    # 专家A UI 申报冲突（曾在供应商甲任职）
    login(page, expert_username("E2E-EXP-A"))
    page.goto(f"{BASE_URL}/expert/declarations")
    page.locator(".el-table__row").first.wait_for(state="visible", timeout=15000)
    sup1_row = page.locator("tr", has_text="E2E-SUP1")
    sup1_row.locator(".el-switch").click()
    sup1_row.locator(".el-select").click()
    page.locator(".el-select-dropdown__item", has_text="曾任公司人员").last.click()
    sup1_row.locator("input[placeholder*='曾任该公司']").fill("曾在E2E供应商甲任技术总监")
    page.get_by_role("button", name="提交回避申报").click()
    page.get_by_text("已申报回避").wait_for(state="visible", timeout=15000)

    # 断言：A → CONFLICT_DECLARED；系统补入 F（待申报）
    st = _sql("SELECT status FROM lot_expert_assignment WHERE id=:a",
              {"a": assignment_id})[0][0]
    assert st == "CONFLICT_DECLARED", f"A 应 CONFLICT_DECLARED: {st}"
    f_rows = _sql("SELECT status FROM lot_expert_assignment WHERE expert_id='E2E-EXP-F' AND lot_id=:l",
                  {"l": lot_id})
    assert f_rows and f_rows[0][0] == "PENDING_DECLARATION", f"应补入专家F: {f_rows}"

    # 专家F UI 申报无冲突 → 进入评审
    login(page, expert_username("E2E-EXP-F"))
    page.goto(f"{BASE_URL}/expert/tasks")
    page.get_by_role("button", name="去申报").first.wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="去申报").first.click()
    page.get_by_role("button", name="提交回避申报").click()
    page.get_by_text("申报完成").first.wait_for(state="visible", timeout=15000)
    st_f = _sql("SELECT status FROM lot_expert_assignment WHERE expert_id='E2E-EXP-F' AND lot_id=:l",
                {"l": lot_id})[0][0]
    assert st_f == "IN_PROGRESS", f"F 应进入评审: {st_f}"
