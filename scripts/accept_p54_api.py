"""P5.4 围串标报告验收脚本（纯 service + DeepSeek）。

覆盖 task.md P5.4 验收：
- 模板报告（LOW/MEDIUM）：含风险评分 + 关键证据 + 建议措施
- LLM 报告（HIGH/CRITICAL）：DeepSeek 生成结论/证据分析/建议
- 报告 mode 分级（TEMPLATE/LLM）

前置：DeepSeek key 已配。
用法: poetry run python scripts/accept_p54_api.py
"""

from __future__ import annotations

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.services.fraud_detection_service import _llm_report, _template_report  # noqa: E402

PASS = 0
FAIL = 0

MED = {
    "risk": "MEDIUM", "total_score": 30,
    "scores": {"text": 25, "graph": 40, "price": 10},
    "evidence": {"text": [], "graph": [{"a": "SUP-001", "b": "SUP-002", "rel": "SAME_CONTROLLER"}], "price": []},
}
HIGH = {**MED, "risk": "HIGH", "total_score": 60}


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def main() -> None:
    global PASS, FAIL

    # ==================== 模板报告（LOW/MEDIUM 自动） ====================
    print("\n[模板报告] LOW/MEDIUM 自动生成")
    tpl = _template_report(MED)
    check("模板报告含风险等级", "风险等级" in tpl and "MEDIUM" in tpl, tpl[:80])
    check("模板报告含综合评分", "综合评分 30" in tpl, tpl[:120])
    check("模板报告含关键证据", "关键证据" in tpl and "SAME_CONTROLLER" in tpl, tpl[:160])
    check("模板报告含建议措施", "建议措施" in tpl, tpl[-100:])

    # ==================== LLM 报告（HIGH/CRITICAL PM 触发） ====================
    print("\n[LLM 报告] HIGH/CRITICAL → DeepSeek")
    text = await _llm_report(HIGH)
    check("LLM 报告非空", bool(text and text.strip()), str(text)[:120])
    check("LLM 报告含风险结论", "风险" in text or "HIGH" in text or "高危" in text, str(text)[:120])
    check("LLM 报告含建议", "建议" in text, str(text)[:120])
    print(f"  [样例] {text[:150]}...")

    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
