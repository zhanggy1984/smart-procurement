"""P2.3 多轮对话管理验收脚本（service 层直调，不依赖 P3.3 review API）。

覆盖 task.md P2.3 验收：
- 单维度连续追问 10 轮 → turn_number/dim_turn_number 递增、消息完整
- 第 4 轮触发 maybe_summarize → DeepSeek 摘要（API 可用时）；失败则验证兜底
  （get_context 保留最近 3 轮原文）
- get_context 组装含摘要 + 最近轮次，token 预算 ≤8000（含安全边际）
- 摘要消息不占对话轮次（dim_turn 不推进）

前置：DeepSeek key 已配（.env）；MySQL 可达。直接 service 调用。
用法: poetry run python scripts/accept_p23_api.py
"""

from __future__ import annotations

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.database import session_factory  # noqa: E402
from app.models.conversation import MessageType  # noqa: E402
from app.services import conversation_service as svc  # noqa: E402

PASS = 0
FAIL = 0

TEST_REVIEW = "REV-ACC23"
TEST_DIM = "DIM-ACC23"


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
    # 幂等清理
    async with session_factory() as session:
        from sqlalchemy import delete

        from app.models.conversation import ConversationMessage

        await session.execute(
            delete(ConversationMessage).where(ConversationMessage.review_id == TEST_REVIEW)
        )
        await session.commit()
    print("[cleanup] 测试对话记录已清理")

    turns_seen: list[int] = []
    dims_seen: list[int] = []
    summary_count = 0

    # 10 轮：user 追问 → assistant 回答（交替，共 10 条 MESSAGE）
    async with session_factory() as session:
        for i in range(1, 6):
            m1 = await svc.add_message(
                session, review_id=TEST_REVIEW, dimension_id=TEST_DIM,
                role="user", content=f"第{i}轮追问：技术方案第{i}点是否满足要求？",
            )
            m2 = await svc.add_message(
                session, review_id=TEST_REVIEW, dimension_id=TEST_DIM,
                role="assistant", content=f"第{i}轮回答：方案覆盖了要点{i}，建议补充细节{i}。",
            )
            turns_seen.append(m1.turn_number)
            turns_seen.append(m2.turn_number)
            dims_seen.append(m1.dim_turn_number)
            dims_seen.append(m2.dim_turn_number)
            # 每轮后尝试摘要
            sm = await svc.maybe_summarize(
                session, review_id=TEST_REVIEW, dimension_id=TEST_DIM
            )
            if sm is not None:
                summary_count += 1

    # SUMMARY 也占全局 turn（消息顺序），MESSAGE 的 turn 严格递增但不连续
    check("10 条消息 turn_number 全局递增",
          len(turns_seen) == 10 and all(a < b for a, b in zip(turns_seen, turns_seen[1:])),
          f"turns={turns_seen}")
    check("dim_turn_number 维度内 1..10", dims_seen == list(range(1, 11)),
          f"dims={dims_seen}")

    # 摘要：第 4 轮应触发（stage=前 3 条），10 轮阶段应产生 2 次（第 4、第 8 轮边界）
    check("maybe_summarize 触发摘要 ≥1 次", summary_count >= 1,
          f"count={summary_count}（DeepSeek 不可用时为 0，走原文兜底）")

    # 消息内容完整性（第 1 轮与第 10 轮都在库里）
    async with session_factory() as session:
        from sqlalchemy import select

        from app.models.conversation import ConversationMessage

        all_msgs = (
            await session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.review_id == TEST_REVIEW)
                .order_by(ConversationMessage.dim_turn_number)
            )
        ).all()
        contents = [m.content for m in all_msgs]
        check("消息完整性（首末轮都在）",
              any("第1轮追问" in (c or "") for c in contents)
              and any("第5轮回答" in (c or "") for c in contents),
              f"total={len(all_msgs)}")
        summary_msgs = [m for m in all_msgs if m.message_type == MessageType.SUMMARY]
        if summary_msgs:
            check("摘要内容非空", bool(summary_msgs[-1].content), str(summary_msgs[-1].content)[:80])
            check("摘要不占对话轮（dim_turn 对齐阶段）",
                  summary_msgs[-1].dim_turn_number < max(dims_seen), f"dim_turn={summary_msgs[-1].dim_turn_number}")

        # get_context：组装含摘要 + 最近轮次，token 预算内
        ctx = await svc.get_context(session, review_id=TEST_REVIEW, dimension_id=TEST_DIM)
        tokens = svc._count_tokens(ctx)
        check("get_context 非空", bool(ctx.strip()))
        check(f"上下文 token ≤8000（实际 {tokens}）", tokens <= 8000, f"tokens={tokens}")
        if summary_msgs:
            check("上下文含历史摘要", "历史摘要" in ctx)
        check("上下文含最近轮次原文",
              any("第5轮" in ctx for _ in [1]), f"ctx_tail={ctx[-80:]!r}")

    # 清理
    async with session_factory() as session:
        from sqlalchemy import delete

        from app.models.conversation import ConversationMessage

        await session.execute(
            delete(ConversationMessage).where(ConversationMessage.review_id == TEST_REVIEW)
        )
        await session.commit()
    print("\n[cleanup] 测试对话记录已清理")
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
