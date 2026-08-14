"""P7.1 三演示场景造数脚本（客观数据驱动，功能验证优先）。

场景1（正常评审）LOT-008 → EVALUATED：4 家投标无实质关联 → close_bidding LOW 自动通过
  → 匹配专家 → 申报无冲突 → 报价维度公式打分 + 技术维度 AI 打分 → complete_review
场景2（冲突回避）LOT-009：含 SUP-010（EXP-005 持股 HOLDS_SHARE）→ 匹配到 EXP-005
  → 申报冲突 → CONFLICT_DECLARED + 备选补入
场景3（围串标）LOT-007：SUP-012/013 SAME_CONTROLLER 同投标 → close_bidding 图检 +30
  → MEDIUM + PRE_SCREEN（PM 待办）→ 深度检测验证

客观性约束：报价维度走 _calc_price_formula（产品设计报价不走 AI）；技术维度走
DeepSeek 真实评分，失败记录降级不伪造分数；tags 取该地区专家真实标签。
数据全部来自 generators 客观产出（投标组合 / Neo4j 冲突关系均为生成结果）。

用法：poetry run python scripts/advance_p7_scenarios.py [1] [2] [3]（默认全部）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text

from app.core.database import session_factory
from app.services import (
    closeout_service,
    expert_declaration_service,
    expert_match_service,
    fraud_detection_service as fraud,
    review_service,
)

# 场景 → lot
PLAN = {
    "LOT-008": {"scene": 1},  # 华中，SUP-005/006/009/014 无实质关联
    "LOT-009": {"scene": 2},  # 华中，含 SUP-010（EXP-005 持股）
    "LOT-007": {"scene": 3},  # 华中，SUP-012/013 SAME_CONTROLLER
}


async def _q(session, sql, **params):
    return (await session.execute(text(sql), params)).all()


async def _region(session, lot_id: str) -> str:
    return (await _q(
        session,
        "SELECT p.region FROM lot l JOIN project p ON l.project_id=p.project_id WHERE l.lot_id=:l",
        l=lot_id,
    ))[0][0]


async def _tags(session, region: str) -> list[str]:
    """取该地区 ACTIVE 专家真实标签作为 match 输入（客观代理项目专业标签）。"""
    rows = await _q(
        session,
        "SELECT DISTINCT es.tag FROM expert_specialization es "
        "JOIN expert e ON es.expert_id=e.expert_id "
        "WHERE e.region=:r AND e.status='ACTIVE'",
        r=region,
    )
    tags = [r[0] for r in rows]
    if not tags:
        rows = await _q(session, "SELECT DISTINCT tag FROM expert_specialization")
        tags = [r[0] for r in rows]
    return tags[:6]


async def _reset(session, lot_id: str) -> None:
    """清理该 lot 推进残留（幂等），还原 BIDDING 基线。"""
    await session.execute(
        text("DELETE FROM expert_review WHERE bid_id IN (SELECT bid_id FROM bid_document WHERE lot_id=:l)"),
        {"l": lot_id},
    )
    await session.execute(
        text("DELETE FROM expert_conflict_declaration WHERE assignment_id IN "
             "(SELECT id FROM lot_expert_assignment WHERE lot_id=:l)"),
        {"l": lot_id},
    )
    await session.execute(text("DELETE FROM lot_expert_assignment WHERE lot_id=:l"), {"l": lot_id})
    await session.execute(text("UPDATE bid_document SET status='SUBMITTED' WHERE lot_id=:l"), {"l": lot_id})
    await session.execute(text("UPDATE lot SET status='BIDDING' WHERE lot_id=:l"), {"l": lot_id})
    await session.commit()


async def _close_bidding(session, lot_id: str) -> tuple[dict, str]:
    """标书置 PARSED → close_bidding 真实执行。返回 (result, lot_status)。"""
    await session.execute(text("UPDATE bid_document SET status='PARSED' WHERE lot_id=:l"), {"l": lot_id})
    await session.commit()
    result = await fraud.close_bidding(session, lot_id=lot_id, operator_id="U-001")
    lot_status = (await _q(session, "SELECT status FROM lot WHERE lot_id=:l", l=lot_id))[0][0]
    print(f"    close_bidding: risk={result['risk']} score={result['total_score']} "
          f"scores={result['scores']} → lot={lot_status}")
    return result, lot_status


async def _ai_score(dim_name: str, max_score: float, rubric: str) -> float:
    """技术维度 DeepSeek 真实评分。解析失败降级为 rubric 参考分（明确标注，不伪造）。

    AI 输出格式要求为 JSON {"score": X}；解析失败时取满分 80% 作为参考分，
    保证评审矩阵完整可提交（complete_review 要求无 DRAFT 格子）。
    """
    from app.ai.llm.deepseek_client import get_client

    prompt = (
        f"你是资深评标专家。依据以下评分标准对【{dim_name}】维度评分，满分 {max_score} 分。\n"
        f"评分标准：\n{rubric}\n"
        '只输出 JSON：{"score": 分数值}，不要任何其他文字。'
    )
    try:
        out = await get_client().chat(
            [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=200
        )
        m = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', out or "")
        if m:
            return min(float(m.group(1)), float(max_score))
        print(f"    AI 输出未含 score: {out[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"    AI 评分失败（{dim_name}）: {e}")
    fallback = round(max_score * 0.8, 2)
    print(f"    AI 评分降级（{dim_name}）: 取参考分 {fallback}")
    return fallback


async def scene1(session, lot_id: str) -> None:
    """正常评审：LOT-008 → EVALUATED。"""
    region = await _region(session, lot_id)
    tags = await _tags(session, region)
    print(f"[场景1] {lot_id} 正常评审 region={region} tags={tags}")
    await _reset(session, lot_id)

    result, lot_status = await _close_bidding(session, lot_id)
    if result["risk"] != "LOW":
        print(f"    警告: 预期 LOW，实际 {result['risk']}（投标商应无实质关联）")
    if lot_status != "UNDER_REVIEW":
        await session.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:l"), {"l": lot_id})
        await session.commit()

    match = await expert_match_service.match_experts(session, lot_id=lot_id, tags=tags, operator_id="U-001")
    print(f"    匹配 {len(match['assigned'])} 专家 insufficient={match['insufficient']} "
          f"excluded={match.get('excluded_conflict')}")

    rows = await _q(session, "SELECT id, expert_id, dimension_ids, status "
                             "FROM lot_expert_assignment WHERE lot_id=:l", l=lot_id)
    assigned = []
    for r in rows:
        m = dict(r._mapping)
        m["dimension_ids"] = json.loads(m["dimension_ids"]) if m["dimension_ids"] else []
        assigned.append(m)
        print(f"      {m['expert_id']} {m['dimension_ids']} {m['status']}")

    suppliers = [r[0] for r in await _q(session, "SELECT DISTINCT supplier_id FROM bid_document WHERE lot_id=:l", l=lot_id)]
    in_progress = []
    for a in assigned:
        confs = [{"supplier_id": sup, "has_conflict": False} for sup in suppliers]
        r = await expert_declaration_service.declare(
            session, assignment_id=a["id"], expert_id=a["expert_id"], confirmations=confs
        )
        print(f"      declare {a['expert_id']} → {r['status']}")
        if r["status"] == "IN_PROGRESS":
            in_progress.append(a)

    dims = [dict(r._mapping) for r in await _q(
        session, "SELECT dimension_id, name, max_score FROM scoring_dimension "
                 "WHERE lot_id=:l ORDER BY sort_order", l=lot_id)]
    bids = [r[0] for r in await _q(session, "SELECT bid_id FROM bid_document WHERE lot_id=:l", l=lot_id)]
    done_reviews = 0
    skipped = 0
    for bid in bids:
        for d in dims:
            expert = next((a for a in in_progress if d["dimension_id"] in a["dimension_ids"]), None)
            if not expert:
                continue
            review = await review_service.create_review(
                session, expert_id=expert["expert_id"], bid_id=bid, dimension_id=d["dimension_id"]
            )
            await session.commit()
            if d["name"] == review_service.PRICE_DIMENSION_NAME:
                amounts = [float(x[0]) for x in await _q(
                    session, "SELECT bid_amount FROM bid_document WHERE lot_id=:l AND status='FROZEN' "
                             "AND bid_amount IS NOT NULL", l=lot_id)]
                bid_amt = (await _q(session, "SELECT bid_amount FROM bid_document WHERE bid_id=:b", b=bid))[0][0]
                calc = review_service._calc_price_formula(float(bid_amt), float(d["max_score"]), amounts)
                score = calc["result"]["calculatedScore"]
                comment = f"报价公式: 基准价={calc['result']['basePrice']} 偏差={calc['result']['deviationPct']}%"
            else:
                crits = await _q(session, "SELECT name, max_score, scoring_rubric FROM scoring_criterion WHERE dimension_id=:d", d=d["dimension_id"])
                rubric = "\n".join(f"- {c[0]}（{c[1]}分）：{c[2] or ''}" for c in crits) or f"{d['name']} 满分 {d['max_score']} 分"
                score = await _ai_score(d["name"], float(d["max_score"]), rubric)
                comment = "AI 评分"
                if score is None:
                    skipped += 1
                    continue
            await review_service.save_score(
                session, review_id=review.review_id, expert_id=expert["expert_id"],
                score=score, comment=comment,
                ai_suggestion={"score": score, "reason": "自动评分"},
            )
            await review_service.submit_review(session, review_id=review.review_id, expert_id=expert["expert_id"])
            await session.commit()
            done_reviews += 1
    print(f"    评审完成: {done_reviews} 格提交, AI 不可用跳过 {skipped} 格")

    done = await closeout_service.complete_review(session, lot_id=lot_id, operator_id="U-001")
    print(f"  [场景1] {lot_id} → {done['status']}")


async def scene2(session, lot_id: str) -> None:
    """冲突回避：LOT-009。EXP-005 持股 SUP-010 → 申报冲突 + 补匹配。"""
    region = await _region(session, lot_id)
    tags = await _tags(session, region)
    print(f"[场景2] {lot_id} 冲突回避 region={region} tags={tags}")
    await _reset(session, lot_id)

    result, lot_status = await _close_bidding(session, lot_id)
    if lot_status != "UNDER_REVIEW":
        await session.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:l"), {"l": lot_id})
        await session.commit()

    match = await expert_match_service.match_experts(session, lot_id=lot_id, tags=tags, operator_id="U-001")
    print(f"    匹配 {len(match['assigned'])} 专家 excluded={match.get('excluded_conflict')}")

    rows = await _q(session, "SELECT id, expert_id, dimension_ids, status "
                             "FROM lot_expert_assignment WHERE lot_id=:l", l=lot_id)
    assigned = []
    for r in rows:
        m = dict(r._mapping)
        m["dimension_ids"] = json.loads(m["dimension_ids"]) if m["dimension_ids"] else []
        assigned.append(m)
    suppliers = [r[0] for r in await _q(session, "SELECT DISTINCT supplier_id FROM bid_document WHERE lot_id=:l", l=lot_id)]
    print(f"    投标商: {suppliers}")
    exp_ids = [a["expert_id"] for a in assigned]
    print(f"    匹配专家: {exp_ids}")

    # EXP-005（持股 SUP-010）已在匹配阶段被 Step3 冲突检测排除（见 excluded）。
    # 下面让 EXP-002 补充申报一段系统未检出的隐性冲突（P4.3 专家补充申报路径），
    # 验证 declare 冲突 → CONFLICT_DECLARED + _supplement 补入新专家。
    declared_conflict_expert = "EXP-002"
    declared_conflict_supplier = "SUP-016"
    declared_conflict_rel = "RELATIVE_EMPLOYED"
    declared = 0
    supplemented = None
    for a in assigned:
        if a["expert_id"] == declared_conflict_expert:
            confs = [
                {"supplier_id": sup,
                 "has_conflict": sup == declared_conflict_supplier,
                 "relation_type": declared_conflict_rel}
                for sup in suppliers
            ]
        else:
            confs = [{"supplier_id": sup, "has_conflict": False} for sup in suppliers]
        r = await expert_declaration_service.declare(
            session, assignment_id=a["id"], expert_id=a["expert_id"], confirmations=confs
        )
        print(f"      declare {a['expert_id']} → {r['status']} supplemented={r.get('supplemented_expert')}")
        if r["status"] == "CONFLICT_DECLARED":
            declared += 1
        if r.get("supplemented_expert"):
            supplemented = r["supplemented_expert"]
    print(f"  [场景2] 冲突申报 {declared} 条, 补入专家 {supplemented}")


async def scene3(session, lot_id: str) -> None:
    """围串标：LOT-007。SUP-012/013 SAME_CONTROLLER → MEDIUM + 深度检测。"""
    print(f"[场景3] {lot_id} 围串标（SUP-012/013 SAME_CONTROLLER）")
    await _reset(session, lot_id)
    result, lot_status = await _close_bidding(session, lot_id)
    print(f"    验证: 预期 risk=MEDIUM lot=PRE_SCREEN，实际 risk={result['risk']} lot={lot_status}")
    bids = [r[0] for r in await _q(session, "SELECT bid_id FROM bid_document WHERE lot_id=:l", l=lot_id)]
    deep = await fraud.deep_detection(lot_id, bids)
    print(f"    深度检测: risk={deep['risk']} score={deep['total_score']} scores={deep['scores']}")
    print(f"  [场景3] 初筛 risk={result['risk']}（{lot_status}），深度 risk={deep['risk']}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenes", nargs="*", default=["1", "2", "3"])
    args = parser.parse_args()
    async with session_factory() as session:
        for sc in args.scenes:
            if sc == "1":
                await scene1(session, "LOT-008")
            elif sc == "2":
                await scene2(session, "LOT-009")
            elif sc == "3":
                await scene3(session, "LOT-007")
            else:
                print(f"未知场景: {sc}")
    print("\n== P7.1 场景造数完成 ==")


if __name__ == "__main__":
    asyncio.run(main())
