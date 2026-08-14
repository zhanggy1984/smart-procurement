"""P6 演示数据推进脚本：把 LOT-001 完整推进到 EVALUATED。

流程（服务层真实调用，评审打分用合成值不走 AI）：
1. 清理旧数据（幂等重跑）：expert_review / assignment / declaration / lot 状态还原
2. 3 家标书 SUBMITTED → PARSED（模拟解析完成；合成标书无实体文件无法真实解析）
3. close_bidding 真实执行（三检 → LOT-001 无关联 LOW → 标书 FROZEN + lot UNDER_REVIEW）
   若 MEDIUM+ → PRE_SCREEN，脚本置 UNDER_REVIEW（模拟 PM 确认，无确认端点）
4. match_experts 真实执行（tags 取受控词表）→ assignment PENDING_DECLARATION
5. declare 逐家确认无冲突 → IN_PROGRESS
6. 每 FROZEN 标书 × 每维度 create_review + save_score + submit_review
7. complete_review 真实执行 → EVALUATED + 报告 PDF
"""

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text

from app.core.database import session_factory
from app.services import closeout_service, expert_declaration_service, expert_match_service, review_service

LOT = "LOT-001"
BIDS = ["BID-001", "BID-002", "BID-003"]
SUPPLIERS = ["SUP-001", "SUP-002", "SUP-003"]
TAGS = ["教育信息化", "软件开发"]

# 维度 → 满分
DIMS = {
    "DIM-LOT-001-1": 30,
    "DIM-LOT-001-2": 20,
    "DIM-LOT-001-3": 15,
    "DIM-LOT-001-4": 20,
}


async def reset(s):
    """清理旧推进数据，还原状态。"""
    bid_ids = ",".join(f"'{b}'" for b in BIDS)
    await s.execute(text(f"DELETE FROM expert_review WHERE bid_id IN ({bid_ids})"))
    await s.execute(text("DELETE FROM expert_conflict_declaration"))
    # 还原标书/标段状态（避免 close_bidding 二次执行因状态不符失败）
    await s.execute(text(f"UPDATE bid_document SET status='SUBMITTED' WHERE bid_id IN ({bid_ids})"))
    await s.execute(text("UPDATE lot SET status='BIDDING' WHERE lot_id=:lot"), {"lot": LOT})
    await s.execute(text("DELETE FROM lot_expert_assignment WHERE lot_id=:lot"), {"lot": LOT})
    await s.commit()


async def main():
    async with session_factory() as s:
        print("== [1/7] 清理旧数据 ==")
        await reset(s)

        print("== [2/7] 标书置 PARSED（模拟解析完成） ==")
        bid_ids = ",".join(f"'{b}'" for b in BIDS)
        await s.execute(text(f"UPDATE bid_document SET status='PARSED' WHERE bid_id IN ({bid_ids})"))
        await s.commit()

        print("== [3/7] close_bidding 真实执行 ==")
        from app.services import fraud_detection_service as fraud
        result = await fraud.close_bidding(s, lot_id=LOT, operator_id="U-001")
        print("  风险:", result["risk"], "评分:", result["total_score"], "scores:", result["scores"])
        lot_status = (await s.execute(text("SELECT status FROM lot WHERE lot_id=:lot"), {"lot": LOT})).scalar_one()
        print("  lot 状态:", lot_status)
        if lot_status != "UNDER_REVIEW":
            # MEDIUM+ → PRE_SCREEN，模拟 PM 确认（无确认端点）
            await s.execute(text("UPDATE lot SET status='UNDER_REVIEW' WHERE lot_id=:lot"), {"lot": LOT})
            await s.commit()
            print("  PRE_SCREEN → UNDER_REVIEW（模拟 PM 确认）")

        print("== [4/7] match_experts 真实执行 ==")
        match = await expert_match_service.match_experts(s, lot_id=LOT, tags=TAGS, operator_id="U-001")
        print(f"  分配 {len(match['assigned'])} 专家, insufficient={match['insufficient']}, excluded={len(match.get('excluded', []))}")

        # 从 DB 查 assignment 行（含 id/dimension_ids/status），比返回 dict 更可靠
        rows = (await s.execute(text(
            "SELECT id, expert_id, dimension_ids, status FROM lot_expert_assignment WHERE lot_id=:lot"
        ), {"lot": LOT})).all()
        assigned = []
        for r in rows:
            m = dict(r._mapping)
            import json
            m["dimension_ids"] = json.loads(m["dimension_ids"]) if m["dimension_ids"] else []
            assigned.append(m)
            print("   ", m["expert_id"], m["dimension_ids"], m["status"])

        print("== [5/7] 回避申报确认（无冲突） ==")
        in_progress_experts = []
        for a in assigned:
            confirmations = [{"supplier_id": sup, "has_conflict": False} for sup in SUPPLIERS]
            r = await expert_declaration_service.declare(
                s, assignment_id=a["id"], expert_id=a["expert_id"], confirmations=confirmations
            )
            print("   ", a["expert_id"], "→", r["status"])
            if r["status"] == "IN_PROGRESS":
                in_progress_experts.append(a)
        await s.commit()

        print("== [6/7] 创建评审 + 打分 + 提交 ==")
        # 每标书 × 每维度 由匹配专家评审（维度分配优先）
        for bid in BIDS:
            for dim, max_score in DIMS.items():
                expert = next((a for a in in_progress_experts if dim in a["dimension_ids"]), None)
                if not expert:
                    print(f"  跳过 {bid} {dim}: 无匹配专家")
                    continue
                review = await review_service.create_review(
                    s, expert_id=expert["expert_id"], bid_id=bid, dimension_id=dim
                )
                await s.commit()
                score = round(max_score * 0.86, 2)
                await review_service.save_score(
                    s, review_id=review.review_id, expert_id=expert["expert_id"],
                    score=score, comment="AI 辅助评分，方案完整，符合评分标准。",
                    ai_suggestion={"score": score, "reason": "综合评分"},
                )
                await review_service.submit_review(
                    s, review_id=review.review_id, expert_id=expert["expert_id"]
                )
                await s.commit()
        print("  评审创建+提交完成")

        print("== [7/7] complete_review 真实执行 ==")
        done = await closeout_service.complete_review(s, lot_id=LOT, operator_id="U-001")
        print("  结果:", done)
        print("== 推进完成：LOT-001 →", done["status"], "==")


asyncio.run(main())
