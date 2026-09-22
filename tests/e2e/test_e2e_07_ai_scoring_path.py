"""E2E-7 AI 评分 LLM 支路（既有 6 条 E2E 结构上未覆盖的一条）。

为什么要单列：E2E-1 的 `ui_review_all` 也在报价维度点了「AI 辅助评分」，但报价维度
后端直接走 price_calc 公式实时出分（前端等的是「报价公式」文案），**不调 LLM**；
其余维度一律人工填分。即这条真实链路在浏览器端从未被触发过：

    ReviewWorkbenchView.aiScore() → POST /api/v1/reviews/{id}/score (SSE)
    → review_service.stream_score → build_score_prompt → _score_system
    → app/prompts/score_system.md（.format() 注入 5 个占位符）

本用例选**非报价**维度（技术）走这条支路，断言：
1. AI 建议分出现且可解析为浮点数（证明 build_score_prompt 真的跑通、LLM 返回可用结果）；
2. 无降级 Banner、无评分中断帧（模板 `.format()` 若因字面大括号炸掉，端点会发 error 帧、
   aiText 落「【评分中断】…」，本用例即抓这个）；
3. 证据溯源区有标书原文引用（score 事件与 source 事件同源，有分必有引用）。

不覆盖：对话路径（build_chat_prompt，已由 HTTP E2E 覆盖）；镜像内是否含模板
（那是「容器审计 ≠ 镜像取证」的另一个命题，本用例证不了）；首登改密流程本身
（fixture 造的是「已入职」账号，本用例走的是业务路径）。
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import BASE_URL, Api, login, _sql
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

# 报价维度必须排除：它走 price_calc 公式，不调 LLM（正是本用例要区分的对象）
DIM_UNDER_TEST = "技术"
# LLM 真实流式，给足超时（含 DeepSeek 冷启动）
AI_TIMEOUT_MS = 240_000


# ==================== 前置：API 造 UNDER_REVIEW 标段 ====================


def _expert_for_dimension(lot_id: str, dim_name: str) -> str:
    """找被匹配到该维度的专家用户名。

    维度不是平均分配的：实测同批匹配结果为 EXP1→报价/服务/资信、EXP2→技术、EXP3→商务
    （`lot_expert_assignment.dimension_ids` 是 JSON 数组）。故按库里的实际分配结果选人，
    而不是假定某个专家有某个维度。
    """
    dims = {r[0]: r[1] for r in _sql(
        "SELECT dimension_id, name FROM scoring_dimension WHERE lot_id = :l", {"l": lot_id})}
    target = next((d for d, n in dims.items() if n == dim_name), None)
    assert target, f"标段 {lot_id} 无维度「{dim_name}」，现有={sorted(dims.values())}"

    for expert_id, dim_ids in _sql(
            "SELECT expert_id, dimension_ids FROM lot_expert_assignment WHERE lot_id = :l",
            {"l": lot_id}):
        if isinstance(dim_ids, str):
            dim_ids = json.loads(dim_ids or "[]")
        if target in (dim_ids or []):
            return expert_username(expert_id)
    raise AssertionError(f"无专家被分配到维度「{dim_name}」（{target}）")


def _ready_lot(admin, pm) -> tuple[str, list[str], str]:
    """API 造「可评审」标段 + 专家申报完成。返回 (lot_id, bid_ids, assignment_id)。

    编排对齐 E2E-4 的 `_ready_lot`：导入 → 建项目/标段/维度 → 投标 → 等解析
    → 关投标 → 匹配专家 → 专家回避申报。全程复用 conftest/helpers 的共享实现。
    """
    import_experts(admin, EXPERT_ROWS)
    import_suppliers(admin, SUPPLIER_ROWS)
    _, lot_id = create_project_full(pm)
    bid_ids = upload_bids(pm, lot_id, [r["supplier_id"] for r in SUPPLIER_ROWS],
                          ["1,000,000", "1,200,000", "800,000"])
    wait_parsed(lot_id, bid_ids)

    r = pm.post(f"/lots/{lot_id}/close-bidding")
    assert r.status_code == 200, r.text
    r = pm.post(f"/lots/{lot_id}/match-experts", json={"tags": ["软件开发"]})
    assert r.status_code == 200, r.text

    rows = _sql("SELECT id, expert_id FROM lot_expert_assignment WHERE lot_id=:l", {"l": lot_id})
    assert rows, "专家未匹配到"
    assignment_id = rows[0][0]

    # 回避申报：全部被匹配专家逐个确认投标供应商无冲突（assignment 表无 supplier_id，
    # 从标书反查）。对齐 E2E-1「先全员申报、再逐维度评审」的编排。
    suppliers = _sql("SELECT DISTINCT supplier_id FROM bid_document WHERE lot_id=:l", {"l": lot_id})
    confs = [{"supplier_id": s[0], "has_conflict": False, "relation_type": None,
              "relation_detail": None} for s in suppliers]
    for aid, expert_id in rows:
        exp = Api("E2E-DUMMY", expert_username(expert_id))
        try:
            r = exp.post(f"/experts/assignments/{aid}/declare", json={"confirmations": confs})
            assert r.status_code == 200, f"{expert_id} 申报失败: {r.text}"
        finally:
            exp.close()
    return lot_id, bid_ids, assignment_id


# ==================== UI：进入指定维度的评审工作台 ====================


def _open_dimension_review(page, dim_name: str) -> str:
    """任务矩阵里点该维度列首行的「去评审」进入工作台，返回落地的维度名。

    走真实 UI 而非直接拼 URL：同时验证「标书 × 维度」矩阵的单元格跳转接线。
    """
    page.locator(".task-card").first.wait_for(state="visible", timeout=20000)
    table = page.locator(".task-card").first.locator(".el-table").first
    headers = table.locator(".el-table__header th")
    idx = None
    for i in range(headers.count()):
        if headers.nth(i).inner_text().strip() == dim_name:
            idx = i
            break
    assert idx is not None, \
        f"任务矩阵未找到「{dim_name}」列，表头={[headers.nth(i).inner_text().strip() for i in range(headers.count())]}"

    cell = table.locator(".el-table__body tbody tr").first.locator("td").nth(idx)
    cell.get_by_role("button").click()
    page.get_by_text("人工打分").first.wait_for(state="visible", timeout=20000)

    qs = parse_qs(urlparse(page.url).query)
    return (qs.get("dimension_name") or [""])[0]


def _wait_ai_answer(page, timeout: int = AI_TIMEOUT_MS) -> str:
    """等 AI 建议落 DOM（score 事件 → aiText 追加「【AI 建议得分】X」）。返回 aiText。"""
    page.wait_for_function(
        "() => { const el = document.querySelector('.ai-pre');"
        " return !!el && el.innerText.includes('AI 建议得分'); }",
        timeout=timeout,
    )
    return page.locator(".ai-pre").inner_text()


# ==================== 主流程 ====================


@pytest.mark.e2e
def test_e2e_07_ai_scoring_llm_path(page, admin_api, pm_api):
    lot_id, bid_ids, _ = _ready_lot(admin_api, pm_api)
    bid_id = bid_ids[0]

    # ---- 1. 专家登录 → 任务列表 → 选非报价维度进工作台 ----
    # 维度分配由匹配算法决定，需按库里实际结果选专家（见 _expert_for_dimension）
    expert = _expert_for_dimension(lot_id, DIM_UNDER_TEST)
    login(page, expert)
    page.goto(f"{BASE_URL}/expert/tasks")
    dim_name = _open_dimension_review(page, DIM_UNDER_TEST)
    assert dim_name == DIM_UNDER_TEST, f"落地维度非预期: {dim_name!r}"
    assert dim_name != "报价", "报价维度走公式，不覆盖 LLM 支路"

    max_score = float(page.locator(".ctx-max").inner_text().replace("满分", "").replace("分", "").strip())

    # ---- 2. 点「AI 辅助评分」→ 走 SSE LLM 流 ----
    page.get_by_role("button", name="AI 辅助评分").click()

    try:
        ai_text = _wait_ai_answer(page)
    except Exception:
        # 失败现场取证：把 AI 区/降级 Banner 的实际文案带进断言信息
        box = page.locator(".ai-box").inner_text() if page.locator(".ai-box").count() else "<无 .ai-box>"
        banner = page.locator(".degrade-banner").inner_text() if page.locator(".degrade-banner").count() else ""
        raise AssertionError(
            f"等待 AI 建议分超时（{AI_TIMEOUT_MS}ms）。ai-box={box[:500]!r} banner={banner[:200]!r}"
        ) from None

    # ---- 3. 断言：无降级 Banner、无中断帧 ----
    assert page.locator(".degrade-banner").count() == 0, \
        f"出现降级 Banner（AI 不可用）：{page.locator('.degrade-banner').inner_text()!r}"
    assert "AI 辅助评分暂不可用" not in page.locator(".el-main").inner_text()
    assert "【评分中断】" not in ai_text, f"评分流中断帧: {ai_text[:500]!r}"

    # ---- 4. 断言：AI 建议分可解析为浮点数且在 [0, max_score] ----
    m = re.search(r"【AI 建议得分】\s*([\-0-9.]+)", ai_text)
    assert m, f"未匹配到 AI 建议分。aiText={ai_text[:500]!r}"
    raw = m.group(1)
    assert raw != "-", f"AI 未给出可解析分数（score=None 兜底）。aiText={ai_text[:500]!r}"
    score = float(raw)
    assert 0 <= score <= max_score, f"AI 建议分越界: {score} ∉ [0, {max_score}]"

    # ---- 5. 断言：证据溯源区有标书原文（有 score 必有 source 事件） ----
    cites = page.locator(".cite-item")
    assert cites.count() > 0, "证据溯源区无引用（.cite-item 为空）"
    cite_text = cites.first.inner_text()
    assert len(cite_text.strip()) > 20, f"引用内容过短: {cite_text!r}"

    # ---- 6. 断言：AI 建议分已回填人工打分框（用户可见的闭环） ----
    shown = float(page.locator(".score-box .el-input-number input").input_value())
    assert abs(shown - score) < 0.05, f"打分框回填值 {shown} 与建议分 {score} 不一致"

    # 原始取证：AI 建议全文 + 格式（`-s` 时打到 stdout，供人核对是真 LLM 作答而非兜底）
    print(f"[E2E-7] lot={lot_id} bid={bid_id} expert={expert} dim={dim_name} "
          f"AI建议分={score}/{max_score} 引用数={cites.count()} aiText长度={len(ai_text)}")
    print(f"[E2E-7] 引用原文片段={cite_text.strip()[:80]!r}")
    print("[E2E-7] aiText 全文 >>>")
    print(ai_text)
    print("<<< aiText 全文")
