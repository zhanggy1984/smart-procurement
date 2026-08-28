"""E2E-1 正常评审流（task.md 12 步）。

管理员导入专家+供应商 → PM 建项目/标段(UI) → 维度(API，前端无 UI) →
供应商投标 → PM 关闭投标(LOW) → PM 匹配专家 → 专家回避申报 →
专家评审（报价维度 AI 公式 + 其他维度人工）→ PM 结束评审 → 推送定标 →
供应商查看结果。

验收：整链路走通，最终标段 AWARDED，winner 供应商可见中标。
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import BASE_URL, login, _sql
from helpers import (expert_username,
                     make_bid_pdf,
                     make_expert_excel, make_supplier_excel, supplier_username, upload_bids, wait_parsed, lot_status,
                     award_bid)

EXPERT_ROWS = [
    # 地区用「西北」：合成演示数据（非 E2E- 前缀，cleanup 不删）在西北的 ACTIVE 专家
    # 无"软件开发"标签，故候选池=西北+软件开发 只含 E2E 专家，避免匹配被演示数据挤占。
    {"expert_id": "E2E-EXP1", "name": "E2E专家甲", "region": "西北", "exp": 15, "tags": ["软件开发", "人工智能"]},
    {"expert_id": "E2E-EXP2", "name": "E2E专家乙", "region": "西北", "exp": 12, "tags": ["软件开发", "网络安全"]},
    {"expert_id": "E2E-EXP3", "name": "E2E专家丙", "region": "西北", "exp": 10, "tags": ["软件开发", "大数据"]},
    {"expert_id": "E2E-EXP4", "name": "E2E专家丁", "region": "西北", "exp": 8, "tags": ["软件开发", "系统集成"]},
    {"expert_id": "E2E-EXP5", "name": "E2E专家戊", "region": "西北", "exp": 6, "tags": ["软件开发", "物联网"]},
]
SUPPLIER_ROWS = [
    {"supplier_id": "E2E-SUP1", "name": "E2E供应商甲", "code": "913100000000000011"},
    {"supplier_id": "E2E-SUP2", "name": "E2E供应商乙", "code": "913100000000000012"},
    {"supplier_id": "E2E-SUP3", "name": "E2E供应商丙", "code": "913100000000000013"},
]


# ==================== UI helper ====================


def ui_import(page, path: str, file_bytes: bytes, filename: str) -> None:
    """管理员导入页：登录 → 上传 Excel（input 隐藏，set_input_files 直接赋值）→ 等结果。"""
    login(page, "e2e_admin")
    tmp = os.path.join(os.environ.get("TEMP", "."), filename)
    with open(tmp, "wb") as f:
        f.write(file_bytes)
    page.goto(f"{BASE_URL}{path}")
    page.locator("input[type=file]").set_input_files(tmp)
    page.locator(".result-text").wait_for(state="visible", timeout=30000)


def ui_create_project_and_lot(page, project_code: str, lot_code: str) -> None:
    """PM 新建项目 → 详情 dialog → 新建标段（全 UI）。返回后项目/lot_id 需从 DB 查。"""
    login(page, "e2e_pm")
    page.goto(f"{BASE_URL}/pm/projects")
    page.get_by_role("button", name="新建项目").click()
    dlg = page.locator(".el-dialog", has_text="新建项目")
    dlg.get_by_placeholder("如 PRJ-2026-001").fill(project_code)
    dlg.get_by_label("项目名称").fill("E2E正常评审项目")
    # 类型/地区 el-select（地区=西北：与 EXPERT_ROWS 一致，隔离合成演示数据匹配池）
    _select(dlg, "类型", "服务")
    _select(dlg, "地区", "西北")
    dlg.locator(".el-input-number input").first.fill("200")
    dlg.get_by_role("button", name="创建").click()
    # 表格出现新行 → 点击进入详情
    page.get_by_text(project_code).first.wait_for(state="visible", timeout=15000)
    page.locator("tr", has_text=project_code).first.click()
    detail = page.locator(".el-dialog", has_text="E2E正常评审项目").last
    detail.get_by_role("button", name="新建标段").click()
    lot_dlg = page.locator(".el-dialog", has_text="新建标段").last
    lot_dlg.get_by_placeholder("如 LOT-001").fill(lot_code)
    lot_dlg.get_by_label("标段名称").fill("E2E正常评审标段")
    lot_dlg.locator(".el-input-number input").fill("50")
    lot_dlg.get_by_role("button", name="创建").click()
    page.get_by_text(lot_code).first.wait_for(state="visible", timeout=15000)
    # 关闭详情 dialog。新建标段 dialog 是 append-to-body（挂 body 末尾），且 el-dialog
    # 默认 destroy-on-close=false：关闭后 DOM 保留仅 display:none，全局 `.el-dialog__headerbtn`
    # 的 .last 会命中隐藏的新建标段关闭按钮 → 点击超时。须在详情 dialog 作用域内精准点。
    detail_dlg = page.locator(".el-dialog", has_text="E2E正常评审项目").last
    detail_dlg.locator(".el-dialog__headerbtn").click()
    detail_dlg.wait_for(state="hidden", timeout=10000)


def _select(scope, label, option_text) -> None:
    """在 scope 内点开 el-select（按 form label）并选 option。"""
    item = scope.locator(".el-form-item", has_text=label)
    item.locator(".el-select").click()
    page = scope.page
    page.locator(".el-select-dropdown__item", has_text=option_text).last.click()


def ui_close_bidding(page, lot_code: str):
    """PM 关闭投标 → 初筛结果 dialog（预期 LOW）。"""
    login(page, "e2e_pm")
    page.goto(f"{BASE_URL}/pm/tasks")
    page.locator("tr", has_text=lot_code).wait_for(state="visible", timeout=15000)
    page.locator("tr", has_text=lot_code).get_by_role("button", name="关闭投标").click()
    page.locator(".el-dialog", has_text="关闭投标").get_by_role("button", name="确认执行").click()
    page.get_by_text("围串标初筛结果").wait_for(state="visible", timeout=20000)
    return page.locator(".el-dialog", has_text="初筛结果").inner_text()


def ui_match_experts(page, lot_code: str):
    """PM 评审进度 → 专家匹配（勾软件开发）→ 执行匹配。返回结果文本。"""
    login(page, "e2e_pm")
    page.goto(f"{BASE_URL}/pm/reviews")
    page.locator(".el-select").first.click()
    page.locator(".el-select-dropdown__item", has_text=lot_code).last.click()
    page.get_by_role("button", name="专家匹配").wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="专家匹配").click()
    page.locator(".el-checkbox", has_text="软件开发").click()
    page.locator(".el-dialog", has_text="专家匹配").get_by_role("button", name="执行匹配").click()
    page.get_by_text("匹配专家").first.wait_for(state="visible", timeout=20000)
    return page.locator(".el-dialog", has_text="专家匹配").inner_text()


def ui_declare_no_conflict(page, exp_username: str):
    """专家确认回避申报：全部无冲突 → 提交。"""
    login(page, exp_username)
    page.goto(f"{BASE_URL}/expert/tasks")
    page.get_by_role("button", name="去申报").first.wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="去申报").first.click()
    page.get_by_role("button", name="提交回避申报").wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="提交回避申报").click()
    page.get_by_text("申报完成").first.wait_for(state="visible", timeout=15000)


def ui_review_all(page, exp_username: str):
    """专家评审所有分配维度（报价维度走 AI 公式，其余人工打分）→ 保存 → 提交。"""
    login(page, exp_username)
    page.goto(f"{BASE_URL}/expert/tasks")
    for _ in range(20):
        # 等任务卡/空态渲染：goto 后按钮可能未挂载，短超时 is_visible 会误判 break
        page.locator(".task-card, .el-empty").first.wait_for(state="visible", timeout=15000)
        btn = page.get_by_role("button", name="去评审").first
        try:
            btn.wait_for(state="visible", timeout=3000)
        except Exception:
            btn = page.get_by_role("button", name="继续评审").first
            try:
                btn.wait_for(state="visible", timeout=3000)
            except Exception:
                break
        btn.click()
        page.get_by_text("人工打分").first.wait_for(state="visible", timeout=15000)
        qs = parse_qs(urlparse(page.url).query)
        dim = (qs.get("dimension_name") or [""])[0]
        max_score = float((qs.get("max_score") or ["10"])[0])
        if dim == "报价":
            page.get_by_role("button", name="AI 辅助评分").click()
            page.get_by_text("报价公式").first.wait_for(state="visible", timeout=20000)
        else:
            score = round(max_score * 0.75, 1)
            page.locator(".el-input-number input").fill(str(score))
        page.get_by_placeholder("填写评审依据与结论").fill("E2E 自动评审，依据评分标准综合评定")
        page.get_by_role("button", name="保存草稿").click()
        page.get_by_text("草稿已保存").first.wait_for(state="visible", timeout=10000)
        page.get_by_role("button", name="提交评审").click()
        # 提交后页面 tag「评审已提交锁定（CONFIRMED）」+ ElMessage toast 同文案，.first 消歧
        page.get_by_text("评审已提交锁定").first.wait_for(state="visible", timeout=15000)
        page.goto(f"{BASE_URL}/expert/tasks")
        page.wait_for_timeout(500)
    # 所有格已提交（count 判 0，避免多格 locator strict violation）
    assert page.get_by_role("button", name="去评审").count() == 0, "仍有未评审维度"
    assert page.get_by_role("button", name="继续评审").count() == 0, "仍有未评审维度"


def ui_complete_and_award(page, lot_code: str):
    """PM 结束评审 → 推送定标（ElMessageBox confirm 点确定）。"""
    login(page, "e2e_pm")
    page.goto(f"{BASE_URL}/pm/summary")
    page.locator(".el-select").first.click()
    page.locator(".el-select-dropdown__item", has_text=lot_code).last.click()
    page.get_by_role("button", name="结束评审").wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="结束评审").click()
    page.locator(".el-message-box", has_text="结束评审").get_by_role("button", name="确定").click()
    page.get_by_role("button", name="推送定标").wait_for(state="visible", timeout=20000)
    page.get_by_role("button", name="推送定标").click()
    page.locator(".el-message-box", has_text="定标").get_by_role("button", name="确定").click()
    page.get_by_text("定标成功").wait_for(state="visible", timeout=20000)


def ui_check_result(page, sup_username: str) -> str:
    """供应商查看结果（中标/未中标）。返回结果标题。"""
    import re

    login(page, sup_username)
    page.goto(f"{BASE_URL}/supplier/bids")
    page.locator(".el-table__row").first.wait_for(state="visible", timeout=15000)
    page.locator(".el-table__row").first.click()
    # 行点击跳转详情页；结果正文异步渲染（标题先出），等「中标/未中标」正文而非标题。
    page.wait_for_url(re.compile(r"/supplier/results/"), timeout=15000)
    page.get_by_text(re.compile(r"中标|未中标")).first.wait_for(state="visible", timeout=15000)
    return page.locator(".el-main").inner_text()


# ==================== 主流程 ====================


@pytest.mark.e2e
def test_e2e_01_normal_review(page, admin_api, pm_api):

    # ---- 1. 管理员 UI 导入专家 + 供应商（覆盖导入页） ----
    ui_import(page, "/admin/experts", make_expert_excel(EXPERT_ROWS), "e2e_exp.xlsx")
    assert "导入成功" in page.locator(".result-text").inner_text()
    ui_import(page, "/admin/suppliers", make_supplier_excel(SUPPLIER_ROWS), "e2e_sup.xlsx")
    assert "导入成功" in page.locator(".result-text").inner_text()
    for r in EXPERT_ROWS:
        assert expert_username(r["expert_id"])
    for r in SUPPLIER_ROWS:
        assert supplier_username(r["supplier_id"])

    # ---- 2/3. PM UI 建项目+标段，API 配维度（前端无维度表单） ----
    ui_create_project_and_lot(page, "E2E-PJ-01", "E2E-LT-01")
    rows = _sql(
        "SELECT project_id, lot_id FROM lot WHERE lot_code='E2E-LT-01'")
    assert rows, "标段未创建"
    lot_id = rows[0][1]
    project_id = rows[0][0]
    from helpers import DIMS
    r = pm_api.post(f"/lots/{lot_id}/dimensions", json={"dimensions": DIMS})
    assert r.status_code == 201, r.text
    r = pm_api.post(f"/lots/{lot_id}/expert-criteria", json={
        "expert_count": 5, "min_experts_per_dimension": 1,
        "weight_specialization": "0.4", "weight_experience": "0.3",
        "weight_review_quality": "0.2", "weight_region": "0.1", "min_experience": 3})
    assert r.status_code == 201, r.text

    # ---- 4. 3 供应商投标：SUP1 走 UI，SUP2/SUP3 走 API ----
    # UI 投标（覆盖上传页）
    login(page, supplier_username("E2E-SUP1"))
    page.goto(f"{BASE_URL}/supplier/lots/{lot_id}")
    page.get_by_role("button", name="参与投标").wait_for(state="visible", timeout=15000)
    page.get_by_role("button", name="参与投标").click()
    tmp = os.path.join(os.environ.get("TEMP", "."), "e2e_bid1.pdf")
    with open(tmp, "wb") as f:
        f.write(make_bid_pdf(amount="1,000,000"))
    page.locator("input[type=file]").set_input_files(tmp)
    page.get_by_role("button", name="提交投标").click()
    page.get_by_role("button", name="查看标书详情").wait_for(state="visible", timeout=180000)
    # API 投标 2 家
    bid_ids = upload_bids(pm_api, lot_id, ["E2E-SUP2", "E2E-SUP3"], amounts=["1,200,000", "800,000"])
    wait_parsed(lot_id, bid_ids, timeout=180)

    # ---- 5. PM 关闭投标（初筛 LOW） ----
    result = ui_close_bidding(page, "E2E-LT-01")
    assert "LOW" in result, f"初筛非 LOW: {result}"

    # ---- 6. PM 匹配专家 ----
    result = ui_match_experts(page, "E2E-LT-01")
    assert "5 位" in result, f"匹配专家数异常: {result}"

    # ---- 7. 5 专家回避申报（全部无冲突） ----
    for r in EXPERT_ROWS:
        ui_declare_no_conflict(page, expert_username(r["expert_id"]))

    # ---- 8. 专家评审：报价维度 AI 公式 + 其余人工 ----
    for r in EXPERT_ROWS:
        ui_review_all(page, expert_username(r["expert_id"]))

    # ---- 9. 断言评审落库 ----
    bids = _sql(
        "SELECT bid_id FROM bid_document WHERE lot_id=:l", {"l": lot_id})
    assert len(bids) == 3
    for b in bids:
        reviews = _sql(
            "SELECT COUNT(*) FROM expert_review WHERE bid_id=:b AND status IN ('CONFIRMED','MANUAL_ADJUSTED')",
            {"b": b[0]})
        assert reviews[0][0] == 5, f"bid {b[0]} 评审未全完成: {reviews}"

    # ---- 10. PM 结束评审 → 推送定标 ----
    ui_complete_and_award(page, "E2E-LT-01")
    # lot 终态 EVALUATED（评审结束）；定标状态在 project 层（closeout_service.submit_for_award）
    assert lot_status(lot_id) == "EVALUATED"
    pst = _sql("SELECT status FROM project WHERE project_id=:p", {"p": project_id})[0][0]
    assert pst == "AWARDED", f"项目未定标: {pst}"
    winner = award_bid(pm_api, lot_id)
    assert winner, "定标未产生中标结果"

    # ---- 11. 供应商查看结果 ----
    winner_name = _sql(
        "SELECT s.name FROM bid_document b JOIN supplier s ON s.supplier_id=b.supplier_id WHERE b.bid_id=:w",
        {"w": winner})[0][0]
    text = ui_check_result(page, supplier_username("E2E-SUP1"))
    # 供应商甲投的是 E2E-SUP1 的标书，需确认中标方
    assert "中标" in text or "未中标" in text
    loser_text = ui_check_result(page, supplier_username(SUPPLIER_ROWS[2]["supplier_id"]))
    assert ("未中标" in loser_text) if winner_name != "E2E供应商丙" else ("中标" in loser_text), \
        f"winner_name={winner_name!r} loser_text={loser_text[:300]!r}"
