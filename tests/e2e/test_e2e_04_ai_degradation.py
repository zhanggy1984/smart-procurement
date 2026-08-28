"""E2E-4 AI 不可用降级流（task.md 6 步）。

route 拦截 ai-status 模拟 AI 不可用（不动后端）：评审工作台顶部红色 Banner
"AI 辅助评分暂不可用" → 纯人工评分可用（AI 按钮隐藏）→ AI 恢复 → "切换回 AI
辅助模式"按钮出现 → 点击切回。

验收：降级 UI 行为正确，报价公式不受影响（price_calc 走纯公式）。
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL, login, _sql
from helpers import (create_project_full, expert_username, import_experts,
                     import_suppliers, upload_bids, wait_parsed)

EXPERT_ROWS = [
    {"expert_id": "E2E-EXP1", "name": "E2E专家甲", "region": "西北", "exp": 15, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP2", "name": "E2E专家乙", "region": "西北", "exp": 12, "tags": ["软件开发"]},
    {"expert_id": "E2E-EXP3", "name": "E2E专家丙", "region": "西北", "exp": 10, "tags": ["软件开发"]},
]
SUPPLIER_ROWS = [
    {"supplier_id": "E2E-SUP1", "name": "E2E供应商甲", "code": "913100000000000011"},
    {"supplier_id": "E2E-SUP2", "name": "E2E供应商乙", "code": "913100000000000012"},
    {"supplier_id": "E2E-SUP3", "name": "E2E供应商丙", "code": "913100000000000013"},
]


def _ready_lot(pm_api, admin_api, exp_id="E2E-EXP1") -> tuple[str, str]:
    """API 造 UNDER_REVIEW 标段 + 专家申报完成，返回 (lot_id, assignment_id)。"""
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
                "WHERE expert_id=:e AND lot_id=:l", {"e": exp_id, "l": lot_id})
    assert rows
    assignment_id = rows[0][0]
    # 申报确认供应商 = 该标段投标商（assignment 表无 supplier_id）
    suppliers = _sql("SELECT DISTINCT supplier_id FROM bid_document WHERE lot_id=:l", {"l": lot_id})
    from conftest import Api
    exp = Api("E2E-DUMMY", expert_username(exp_id))
    try:
        confs = [{"supplier_id": s[0], "has_conflict": False, "relation_type": None,
                  "relation_detail": None} for s in suppliers]
        r = exp.post(f"/experts/assignments/{assignment_id}/declare", json={"confirmations": confs})
        assert r.status_code == 200, r.text
    finally:
        exp.close()
    return lot_id, assignment_id


@pytest.mark.e2e
def test_e2e_04_ai_degradation(page, admin_api, pm_api):
    lot_id, assignment_id = _ready_lot(pm_api, admin_api)

    # 进入评审工作台（先打开，route 拦截 ai-status 返回 unavailable）
    def ai_unavailable(route):
        route.fulfill(json={"status": "unavailable", "enabled": True, "circuit": "OPEN"})
    page.route("**/api/v1/reviews/ai-status", ai_unavailable)

    login(page, expert_username("E2E-EXP1"))
    page.goto(f"{BASE_URL}/expert/tasks")
    page.get_by_role("button", name="去评审").first.wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="去评审").first.click()

    # 降级 Banner + 纯人工模式（AI 按钮隐藏）
    page.get_by_text("AI 辅助评分暂不可用").wait_for(state="visible", timeout=20000)
    assert not page.get_by_role("button", name="AI 辅助评分").is_visible(timeout=2000)
    # 「纯人工模式」el-tag 挂载带 el-zoom-in-center appear 过渡（初始 opacity:0），且与
    # Banner(el-alert-fade) easing 不同步；上句 wait_for 返回时其 opacity 可能仍为 0，
    # 无等待 is_visible() 会偶发 False → 与 Banner 一致用 wait_for 等过渡完成。
    page.get_by_text("纯人工模式").wait_for(state="visible", timeout=20000)
    # 纯人工打分可用
    page.locator(".el-input-number input").fill("18")
    page.get_by_placeholder("填写评审依据与结论").fill("E2E 降级流人工评审")
    assert page.get_by_role("button", name="保存草稿").is_enabled()

    # AI 恢复 → 前端 15s 轮询探测 → 切回按钮亮起 → 点击切回
    page.unroute("**/api/v1/reviews/ai-status")
    page.get_by_role("button", name="切换回 AI 辅助模式").wait_for(state="visible", timeout=45000)
    page.get_by_role("button", name="切换回 AI 辅助模式").click()
    page.get_by_text("已切换回 AI 辅助模式").wait_for(state="visible", timeout=10000)
    page.get_by_role("button", name="AI 辅助评分").wait_for(state="visible", timeout=15000)
