"""P6.5 三态演示数据推进脚本：把指定 lot 推进到 EVALUATED 或 UNDER_REVIEW。

参照 advance_p6_demo.py（LOT-001）的完整链条：
close_bidding（真实三检）→ match_experts（真实匹配）→ declare（无冲突）
→ 每 FROZEN 标书 × 每维度 创建评审 + 打分 + 提交 → complete_review（真实执行）。

三态目标（SUP-007 供应商视角）：
- LOT-002 → EVALUATED，SUP-007 得分最高 → 已中标
- LOT-014 → EVALUATED，SUP-007 得分居中 → 未中标
- LOT-013 → UNDER_REVIEW（只 close-bidding 停住，不 complete）→ 评审中

lot 选择依据：match_experts 强约束专家 region == 项目 region（P4.2 匹配算法，不可改）。
LOT-002 华中专家充足；LOT-013/014 华东有效标书 3 家。LOT-004/006（西北）缺匹配专家、
LOT-012（仅 2 家有效标书 close_bidding 判 ABANDONED）不可用——仅作 reset 还原。

分数档位：中标方 0.9×max，其余 0.82×max；LOT-014 中 SUP-007 单独 0.7×max 制造落标。
打分用合成值不走 AI（与 advance_p6_demo 一致）。

用法：poetry run python scripts/advance_p6_lot.py [LOT-002] [LOT-013] [LOT-014] ...（默认 PLAN 内）
未在 PLAN 中的 lot 仅执行 reset（还原状态，不推进）。
幂等：重跑先清理该 lot 的 review/assignment 并还原标书/标段状态。
"""

import argparse
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text

from app.core.database import session_factory
from app.services import closeout_service, expert_declaration_service, expert_match_service, review_service

# lot → (中标供应商, 各标书分数档位) ；WINNER 档位 0.9，其余 0.82，special 覆盖个别标书
PLAN = {
    "LOT-002": {"winner": "SUP-007", "special": {}, "tags": ["大数据", "软件开发", "系统集成", "电子政务"]},
    "LOT-014": {"winner": "SUP-009", "special": {"SUP-007": 0.7}, "tags": ["大数据", "网络安全", "智慧城市"]},
    "LOT-013": {"winner": None, "special": {}, "tags": [], "stop_at_review": True},
}


async def _query(session, sql, **params):
    return (await session.execute(text(sql), params)).all()


async def reset(session, lot_id: str) -> None:
    """清理该 lot 的推进残留（幂等）。"""
    await session.execute(text("DELETE FROM expert_review WHERE bid_id IN (SELECT bid_id FROM bid_document WHERE lot_id=:lot)"), {"lot": lot_id})
    await session.execute(text("DELETE FROM expert_conflict_declaration WHERE assignment_id IN (SELECT id FROM lot_expert_assignment WHERE lot_id=:lot)"), {"lot": lot_id})
    await session.execute(text("DELETE FROM lot_expert_assignment WHERE lot_id=:lot"), {"lot": lot_id})
    await session.execute(text("UPDATE bid_document SET status='SUBMITTED' WHERE lot_id=:lot"), {"lot": lot_id})
    await session.execute(text("UPDATE lot SET status='BIDDING' WHERE lot_id=:lot"), {"lot": lot_id})
    await session.commit()


async def advance(session, lot_id: str) -> None:
    plan = PLAN[lot_id]
    print(f"\n== 推进 {lot_id} ==")

    # lot 参与标书（有效标书：非 DISQUALIFIED、非黑名单供应商）
    bid_rows = await _query(
        session,
        "SELECT b.bid_id, b.supplier_id, s.blacklisted FROM bid_document b "
        "JOIN supplier s ON b.supplier_id = s.supplier_id WHERE b.lot_id=:lot",
        lot=lot_id,
    )
    bids = [r.bid_id for r in bid_rows if not r.blacklisted]
    suppliers = [r.supplier_id for r in bid_rows if not r.blacklisted]
    print(f"  参与标书: {bids}")

    # 维度
    dim_rows = await _query(session, "SELECT dimension_id, max_score FROM scoring_dimension WHERE lot_id=:lot", lot=lot_id)
    dims = {r.dimension_id: float(r.max_score) for r in dim_rows}
    print(f"  维度: {dims}")

    # 标书置 PARSED（模拟解析完成；合成标书无实体文件无法真实解析）
    bid_in = ",".join(f"'{b}'" for b in bids)
    await session.execute(text(f"UPDATE bid_document SET status='PARSED' WHERE bid_id IN ({bid_in})"))
    await session.commit()

    # close_bidding 真实执行（三检 → 标书 FROZEN + lot UNDER_REVIEW / PRE_SCREEN）
    from app.services import fraud_detection_service as fraud
    try:
        result = await fraud.close_bidding(session, lot_id=lot_id, operator_id="U-001")
        print("  风险:", result["risk"], "评分:", result["total_score"])
    except Exception as e:  # noqa: BLE001
        print("  close_bidding 失败:", e)
        return
    lot_status = (await _query(session, "SELECT status FROM lot WHERE lot_id=:lot", lot=lot_id))[0][0]
    print("  lot 状态:", lot_status)
    if lot_status != "UNDER_REVIEW":
        await session.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:lot"), {"lot": lot_id})
        await session.commit()
        print("  PRE_SCREEN → UNDER_REVIEW（模拟 PM 确认）")

    # LOT-012 只推进到评审中
    if plan.get("stop_at_review"):
        print("  == 停在 UNDER_REVIEW（评审中样例） ==")
        return

    # match_experts
    match = await expert_match_service.match_experts(session, lot_id=lot_id, tags=plan["tags"], operator_id="U-001")
    print(f"  匹配专家 {len(match['assigned'])} 人, insufficient={match['insufficient']}")
    rows = await _query(session, "SELECT id, expert_id, dimension_ids, status FROM lot_expert_assignment WHERE lot_id=:lot", lot=lot_id)
    assigned = []
    for r in rows:
        m = dict(r._mapping)
        m["dimension_ids"] = json.loads(m["dimension_ids"]) if m["dimension_ids"] else []
        assigned.append(m)
        print("   ", m["expert_id"], m["dimension_ids"], m["status"])

    # declare 无冲突
    in_progress = []
    for a in assigned:
        confirmations = [{"supplier_id": sup, "has_conflict": False} for sup in suppliers]
        r = await expert_declaration_service.declare(
            session, assignment_id=a["id"], expert_id=a["expert_id"], confirmations=confirmations
        )
        print("   ", a["expert_id"], "→", r["status"])
        if r["status"] == "IN_PROGRESS":
            in_progress.append(a)
    await session.commit()

    # 打分：中标方 0.9×max，其余 0.82×max，special 覆盖
    winner = plan["winner"]
    for bid in bids:
        sup = next(r.supplier_id for r in bid_rows if r.bid_id == bid)
        for dim, max_score in dims.items():
            expert = next((a for a in in_progress if dim in a["dimension_ids"]), None)
            if not expert:
                print(f"  跳过 {bid} {dim}: 无匹配专家")
                continue
            ratio = 0.9 if sup == winner else 0.82
            ratio = plan["special"].get(sup, ratio)
            review = await review_service.create_review(session, expert_id=expert["expert_id"], bid_id=bid, dimension_id=dim)
            await session.commit()
            score = round(max_score * ratio, 2)
            await review_service.save_score(
                session, review_id=review.review_id, expert_id=expert["expert_id"],
                score=score, comment="AI 辅助评分，方案完整，符合评分标准。",
                ai_suggestion={"score": score, "reason": "综合评分"},
            )
            await review_service.submit_review(session, review_id=review.review_id, expert_id=expert["expert_id"])
            await session.commit()
    print("  评审创建+提交完成")

    # complete_review
    done = await closeout_service.complete_review(session, lot_id=lot_id, operator_id="U-001")
    print(f"  完成: {lot_id} → {done['status']}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("lots", nargs="*", default=list(PLAN.keys()))
    args = parser.parse_args()
    async with session_factory() as session:
        for lot_id in args.lots:
            print(f"== [重置] {lot_id} ==")
            await reset(session, lot_id)
            if lot_id not in PLAN:
                print(f"  {lot_id} 不在推进计划，已还原")
                continue
            await advance(session, lot_id)
    print("\n== 三态推进完成 ==")


asyncio.run(main())
