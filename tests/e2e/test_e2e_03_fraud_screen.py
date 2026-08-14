"""E2E-3 围串标检测流（task.md 8 步）— 当前实现范围：初筛 MEDIUM + SAME_CONTROLLER 证据。

⚠️ 缺口：close-bidding MEDIUM 后 lot 停 PRE_SCREEN，**无 PM 放行/深度检测/废标
API**（grep 全代码库 PRE_SCREEN 只被 close_bidding 写入）。前端 BidScreenView 对
PRE_SCREEN 仅显示"待确认"无按钮。task.md 的"放行→深度 HIGH→废标"链路未实现，
E2E 验证到初筛为止，缺口记入 P7.4 报告（演示走 MEDIUM 分支会卡死）。
"""

from __future__ import annotations

import pytest

from conftest import BASE_URL, login, _sql
from helpers import (create_project_full, import_suppliers, neo4j_run,
                     upload_bids, wait_parsed)

S_A, S_B, S_C = "E2E-SUP-A", "E2E-SUP-B", "E2E-SUP-C"
SUPPLIER_ROWS = [
    {"supplier_id": S_A, "name": "E2E围标供应商A", "code": "913100000000000031"},
    {"supplier_id": S_B, "name": "E2E围标供应商B", "code": "913100000000000032"},
    {"supplier_id": S_C, "name": "E2E围标供应商C", "code": "913100000000000033"},
]


@pytest.mark.e2e
def test_e2e_03_fraud_initial_screen(page, admin_api, pm_api):
    import_suppliers(admin_api, SUPPLIER_ROWS)
    # Neo4j 直造 SAME_CONTROLLER（A→B，无导入途径，仅 fraud 读取）
    neo4j_run(
        "MATCH (a:Supplier {supplierId:$a}), (b:Supplier {supplierId:$b}) "
        "MERGE (a)-[r:SAME_CONTROLLER]->(b)",
        {"a": S_A, "b": S_B})

    _, lot_id = create_project_full(pm_api)
    bid_ids = upload_bids(pm_api, lot_id, [S_A, S_B, S_C],
                          amounts=["1,000,000", "1,010,000", "1,500,000"])
    wait_parsed(lot_id, bid_ids)

    # 初筛：SAME_CONTROLLER(+30) + 报价集中 → MEDIUM，图证据可审计
    r = pm_api.post(f"/lots/{lot_id}/close-bidding")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["risk"] == "MEDIUM", f"预期 MEDIUM: {data}"
    assert any("SAME_CONTROLLER" in str(e) for e in data["evidence"]["graph"]), data["evidence"]["graph"]

    # lot 停 PRE_SCREEN（PM 待办，无放行实现）
    rows = _sql("SELECT status FROM lot WHERE lot_id=:l", {"l": lot_id})
    assert rows[0][0] == "PRE_SCREEN", rows

    # UI：围串标待办显示"确认放行"按钮（P7.4 闭环补齐后）。
    # 用 lot_code 精确匹配（多残留 E2E lot 同名「E2E测试标段」，且 lot.created_at 可为 NULL 导致排序不定）
    lot_code = _sql("SELECT lot_code FROM lot WHERE lot_id=:l", {"l": lot_id})[0][0]
    login(page, "e2e_pm")
    page.goto(f"{BASE_URL}/pm/tasks")
    row = page.locator("tr", has_text=lot_code).first
    row.wait_for(state="visible", timeout=15000)
    assert "确认放行" in row.inner_text(), row.inner_text()

    # ---- P7.4 闭环：PM 确认放行 → 深度检测（SAME_CONTROLLER + 同模板标书高相似）→ HIGH 不放行 ----
    r = pm_api.post(f"/lots/{lot_id}/confirm-prescreen")
    assert r.status_code == 200, r.text
    deep = r.json()
    assert deep["released"] is False, f"SAME_CONTROLLER+标书高相似应深度高风险不放行: {deep}"
    assert deep["risk"] in ("HIGH", "CRITICAL"), f"深度检测应 HIGH/CRITICAL: {deep['risk']}"
    # lot 仍 PRE_SCREEN（未放行）
    rows = _sql("SELECT status FROM lot WHERE lot_id=:l", {"l": lot_id})
    assert rows[0][0] == "PRE_SCREEN"

    # ---- PM 废标：标记涉事供应商A 标书 DISQUALIFIED ----
    a_bid = _sql("SELECT bid_id FROM bid_document WHERE lot_id=:l AND supplier_id=:s",
                 {"l": lot_id, "s": S_A})[0][0]
    r = pm_api.post(f"/lots/{lot_id}/bids/{a_bid}/disqualify")
    assert r.status_code == 200, r.text
    st = _sql("SELECT status FROM bid_document WHERE bid_id=:b", {"b": a_bid})[0][0]
    assert st == "DISQUALIFIED", f"标书应 DISQUALIFIED: {st}"
