"""E2E-2 冲突回避流（task.md 10 步）。

导入冲突数据（专家A 持股供应商X 5%）→ 投标 → 匹配 → 专家A 因冲突被排除
（available=false）→ 备选专家B 递补 → 专家A 不可见该标段 → 专家B 申报+评审 → 定标。

验收：专家A 无该标段任务；匹配结果"冲突排除"含专家A；专家B 递补并完成评审。
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL, login, _sql
from helpers import (create_project_full, expert_username, import_conflicts,
                     import_experts, import_suppliers, supplier_username,
                     upload_bids, wait_parsed, neo4j_run, lot_status)

EXP_A, EXP_B = "E2E-EXP-A", "E2E-EXP-B"
EXPERT_ROWS = [
    {"expert_id": EXP_A, "name": "E2E冲突专家甲", "region": "西北", "exp": 15, "tags": ["软件开发"]},
    {"expert_id": EXP_B, "name": "E2E备选专家乙", "region": "西北", "exp": 12, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-C", "name": "E2E专家丙", "region": "西北", "exp": 10, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-D", "name": "E2E专家丁", "region": "西北", "exp": 8, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP-E", "name": "E2E专家戊", "region": "西北", "exp": 6, "tags": ["软件开发"]},
]
SUP_X, SUP1, SUP2 = "E2E-SUPX", "E2E-SUP1", "E2E-SUP2"
SUPPLIER_ROWS = [
    {"supplier_id": SUP_X, "name": "E2E冲突供应商X", "code": "913100000000000021"},
    {"supplier_id": SUP1, "name": "E2E供应商甲", "code": "913100000000000011"},
    {"supplier_id": SUP2, "name": "E2E供应商乙", "code": "913100000000000012"},
]


@pytest.mark.e2e
def test_e2e_02_conflict_avoidance(page, admin_api, pm_api):
    # ---- 1. 导入专家/供应商 + 冲突（EXP-A 持股 SUPX 5%） ----
    import_experts(admin_api, EXPERT_ROWS)
    import_suppliers(admin_api, SUPPLIER_ROWS)
    import_conflicts(admin_api, [{
        "expert_name": "E2E冲突专家甲", "supplier_name": "E2E冲突供应商X",
        "code": "913100000000000021", "rel_type": "股东", "share": "5",
    }])

    # ---- 2. 建项目 + 3 供应商投标 + 关闭投标 ----
    project_id, lot_id = create_project_full(pm_api)
    bid_ids = upload_bids(pm_api, lot_id, [SUP_X, SUP1, SUP2], amounts=["1,100,000", "1,200,000", "900,000"])
    wait_parsed(lot_id, bid_ids, timeout=180)
    r = pm_api.post(f"/lots/{lot_id}/close-bidding")
    assert r.status_code == 200, r.text

    # ---- 3. UI 匹配 → 断言 EXP-A 被冲突排除、EXP-B 递补 ----
    login(page, "e2e_pm")
    page.goto(f"{BASE_URL}/pm/reviews")
    page.locator(".el-select").first.click()
    page.locator(".el-select-dropdown__item", has_text="E2E测试标段").last.click()
    page.get_by_role("button", name="专家匹配").wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="专家匹配").click()
    page.locator(".el-checkbox", has_text="软件开发").click()
    page.locator(".el-dialog", has_text="专家匹配").get_by_role("button", name="执行匹配").click()
    page.get_by_text("匹配专家").first.wait_for(state="visible", timeout=20000)
    result = page.locator(".el-dialog", has_text="专家匹配").inner_text()
    assert EXP_A in result, f"冲突排除未显示专家A: {result}"
    assert "5 位" in result or "4 位" in result, f"匹配专家数异常: {result}"

    # ---- 4. 断言：EXP-A 无 assignment，EXP-B 待申报 ----
    rows = _sql("SELECT COUNT(*) FROM lot_expert_assignment WHERE expert_id=:e AND lot_id=:l",
                {"e": EXP_A, "l": lot_id})
    assert rows[0][0] == 0, "冲突专家A 不应被指派"
    rows = _sql("SELECT status FROM lot_expert_assignment WHERE expert_id=:e AND lot_id=:l",
                {"e": EXP_B, "l": lot_id})
    assert rows and rows[0][0] == "PENDING_DECLARATION", f"备选专家B 应待申报: {rows}"

    # ---- 5. UI：专家A 任务页不可见该标段 ----
    login(page, expert_username(EXP_A))
    page.goto(f"{BASE_URL}/expert/tasks")
    assert "暂无评审任务" in page.inner_text("body"), "冲突专家A 不应看到任何评审任务"

    # ---- 6. UI：专家B 申报（无冲突）→ 评审全部维度 ----
    from test_e2e_01_normal_review import ui_declare_no_conflict, ui_review_all
    ui_declare_no_conflict(page, expert_username(EXP_B))
    ui_review_all(page, expert_username(EXP_B))

    # ---- 7. 定标 ----
    r = pm_api.post(f"/lots/{lot_id}/complete-review")
    assert r.status_code in (200, 400), r.text
    r = pm_api.post(f"/projects/{project_id}/submit-for-award")
    assert r.status_code == 200, r.text
    # lot 终态 EVALUATED（评审结束）；定标状态在 project 层
    assert lot_status(lot_id) == "EVALUATED"
    pst = _sql("SELECT status FROM project WHERE project_id=:p", {"p": project_id})[0][0]
    assert pst == "AWARDED", f"项目未定标: {pst}"
