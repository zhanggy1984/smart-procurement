"""B.3 smart-procurement 契约改造 e2e 验证（评测 §5.1 SSE 契约）。

容器内运行：
    docker cp scripts/verify_sp_e2e.py sp-app:/app/verify_sp_e2e.py
    docker exec sp-app python /app/verify_sp_e2e.py

验证内容：
1. 硬断言（所有分支必须满足）：
   - meta 首帧（agent/model/interface/contract_version 齐全）
   - tool_call(knowledge_retrieval) 存在（检索动作观测，args/result/status 齐全）
   - 每个事件 data 都内置 ts（unix ms）
   - done 收尾
2. AI 路径条件断言（出现 reasoning 时）：
   - reasoning/answer/thought 逐 delta 双发（事件数一致）
   - usage 存在（真实 token 聚合）
   - answer 拼接 == done.content（契约 done 是最终输出）
3. 报告实际走的分支（AI / NO_EVIDENCE / LLM_DOWN），降级分支不算契约失败，
   但会提示验证不完整。

选样：一条「非报价维度」评审（报价走公式分支，无 reasoning 双发，不作本脚本目标）。
认证：复用/新建 sp_verify 用户（display_name=专家名，_resolve_expert 按 name 反查 expert_id）。
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from sqlalchemy import select, text

from app.core.database import session_factory
from app.core.security import hash_password
from app.models.user import Role, User

# 容器内 app 直连自身 8000（契约事件由 app 产出，走 nginx 反代内容一致）
BASE = "http://localhost:8000/api/v1"
PASSWORD = "SpVerify#2026"
USERNAME = "sp_verify"

_passed: list[str] = []
_failed: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passed.append(name)
        print(f"  ✓ {name}")
    else:
        _failed.append(name)
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))


async def _pick_review(s) -> dict:
    """取一条非报价维度评审（CONFIRMED 可重评，stream_score 不锁状态）。"""
    row = (await s.execute(text(
        "SELECT r.review_id, e.name AS expert_name, d.name AS dim_name "
        "FROM expert_review r "
        "JOIN expert e ON e.expert_id = r.expert_id "
        "JOIN scoring_dimension d ON d.dimension_id = r.dimension_id "
        "WHERE d.name != '报价' "
        "ORDER BY r.updated_at DESC LIMIT 1"
    ))).one()
    return dict(row._mapping)


async def _ensure_verify_user(s, expert_name: str) -> None:
    """幂等创建/复用 sp_verify 用户。display_name=专家名（与 expert.name 关联）。"""
    u = (await s.execute(select(User).where(User.username == USERNAME))).scalar_one_or_none()
    if u is None:
        s.add(User(
            user_id=f"VFY-{expert_name[:8]}",
            username=USERNAME,
            password_hash=hash_password(PASSWORD),
            role=Role.REVIEW_EXPERT,
            display_name=expert_name,
            email="sp_verify@itest.local",
            is_active=True,
        ))
        await s.commit()
        print(f"  * 创建验证用户 sp_verify → display_name={expert_name}")
    elif u.display_name != expert_name:
        u.display_name = expert_name
        await s.commit()
        print(f"  * 复用 sp_verify，display_name 更新为 {expert_name}")


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BASE}/auth/login",
                          json={"username": USERNAME, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["access_token"]


def _parse_sse(lines: list[str]) -> list[dict]:
    """SSE 帧解析：id:/event:/data: 三段，空行分隔。"""
    events, cur = [], {}
    for ln in lines:
        if ln.startswith("id: "):
            cur["id"] = int(ln[4:])
        elif ln.startswith("event: "):
            cur["event"] = ln[7:]
        elif ln.startswith("data: "):
            cur["data"] = json.loads(ln[6:])
        elif ln == "" and cur:
            events.append(cur)
            cur = {}
    if cur:
        events.append(cur)
    return events


async def _verify_chat(client: httpx.AsyncClient, token: str, review_id: str) -> None:
    """chat SSE 契约：meta 首帧 → thinking(CHAT) → reasoning/answer/thought 双发 → usage → done。

    对话接口无检索动作，不做 tool_call 断言；done.content 需等于 answer 拼接。
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with client.stream(
        "POST", f"{BASE}/reviews/{review_id}/chat", headers=headers,
        json={"question": "请结合评分标准，重点说明团队资质这条维度的关键扣分点"},
    ) as resp:
        print(f"  HTTP {resp.status_code}")
        resp.raise_for_status()
        lines = [ln async for ln in resp.aiter_lines()]
    events = _parse_sse(lines)
    names = [e["event"] for e in events]
    print(f"  事件序列(截断): {names[:8]} ... [{names[-2:]}]")

    _check("chat meta 首帧", names[0] == "meta")
    _check("chat done 收尾", names[-1] == "done")
    _check("chat 所有事件含 ts", all("ts" in e.get("data", {}) for e in events))
    reasoning = [e for e in events if e["event"] == "reasoning"]
    _check("chat reasoning/answer/thought 三发",
           reasoning and len(reasoning) == sum(1 for e in events if e["event"] == "answer") == sum(1 for e in events if e["event"] == "thought"))
    usage = next((e for e in events if e["event"] == "usage"), None)
    _check("chat usage 存在且为正", usage is not None and all(usage["data"].get(k, 0) > 0 for k in
           ("prompt_tokens", "completion_tokens", "total_tokens")))
    done = next(e for e in events if e["event"] == "done")
    answer_text = "".join(e["data"]["delta"] for e in events if e["event"] == "answer")
    _check("chat answer 拼接 == done.content", answer_text == done["data"].get("content", ""),
           f"len(answer)={len(answer_text)} len(done.content)={len(done['data'].get('content',''))}")


async def main() -> None:
    async with session_factory() as s:
        review = await _pick_review(s)
        await _ensure_verify_user(s, review["expert_name"])
    print(f"目标评审: {review['review_id']} | 专家: {review['expert_name']} | 维度: {review['dim_name']}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        token = await _login(client)
        headers = {"Authorization": f"Bearer {token}"}

        print("\n[1] 契约事件完整性")
        async with client.stream(
            "POST", f"{BASE}/reviews/{review['review_id']}/score", headers=headers
        ) as resp:
            print(f"  HTTP {resp.status_code}")
            resp.raise_for_status()
            lines = [ln async for ln in resp.aiter_lines()]
        events = _parse_sse(lines)
        names = [e["event"] for e in events]
        print(f"  事件序列: {names}")

        # 硬断言
        _check("meta 首帧", names[0] == "meta",
               f"首个事件应为 meta，实际 {names[0] if names else '无事件'}")
        meta = next(e["data"] for e in events if e["event"] == "meta")
        _check("meta 字段齐全", all(k in meta for k in
               ("agent", "model", "interface", "contract_version")),
               f"meta={meta}")
        tool = next((e for e in events if e["event"] == "tool_call"), None)
        _check("tool_call(knowledge_retrieval)", tool is not None and tool["data"]["name"] == "knowledge_retrieval")
        if tool:
            _check("tool_call 字段齐全", all(k in tool["data"] for k in
                   ("id", "name", "args", "result", "status")),
                   f"tool_call={tool['data']}")
        _check("所有事件含 ts", all("ts" in e.get("data", {}) for e in events),
               f"缺 ts 的事件: {[e['event'] for e in events if 'ts' not in e.get('data', {})]}")
        _check("done 收尾", names[-1] == "done", f"最后事件应为 done，实际 {names[-1] if names else '无'}")

        # 分支判定
        reasoning_events = [e for e in events if e["event"] == "reasoning"]
        if any(e["data"].get("stage") == "NO_EVIDENCE" for e in events
               if e["event"] == "thinking"):
            print(f"\n[2] 分支: NO_EVIDENCE（检索无命中，未调 LLM）—— 契约完整但 AI 路径未验证")
            print("  可通过造标书/检查 milvus 集合后重跑验证 AI 双发。")
        elif any(e["data"].get("stage") == "LLM_DOWN" for e in events
                 if e["event"] == "thinking"):
            print(f"\n[2] 分支: LLM_DOWN（断路器触发）—— 契约完整但 AI 路径未验证")
        elif reasoning_events:
            print(f"\n[2] AI 路径条件断言（reasoning 事件 {len(reasoning_events)} 个）")
            # reasoning/answer/thought 同 delta 双发：三类事件数一致
            n_ans = sum(1 for e in events if e["event"] == "answer")
            n_thought = sum(1 for e in events if e["event"] == "thought")
            _check("reasoning/answer/thought 事件数一致",
                   n_ans == len(reasoning_events) == n_thought,
                   f"reasoning={len(reasoning_events)} answer={n_ans} thought={n_thought}")
            _check("delta 均非空", all(e["data"].get("delta") for e in reasoning_events))
            usage = next((e for e in events if e["event"] == "usage"), None)
            _check("usage 存在", usage is not None)
            if usage:
                u = usage["data"]
                _check("usage 字段齐全且为正", all(u.get(k, 0) > 0 for k in
                       ("prompt_tokens", "completion_tokens", "total_tokens")),
                       f"usage={u}")
            done = next(e for e in events if e["event"] == "done")
            answer_text = "".join(e["data"]["delta"] for e in events if e["event"] == "answer")
            _check("answer 拼接 == done.content", answer_text == done["data"].get("content", ""),
                   f"len(answer)={len(answer_text)} len(done.content)={len(done['data'].get('content',''))}")
            _check("done 带 score 事件", any(e["event"] == "score" for e in events))
            score = next((e for e in events if e["event"] == "score"), None)
            if score:
                # done 显式结构化分数（§5.1 扩展），评测端不依赖正则提取
                _check("done 显式带结构化 score",
                       done["data"].get("score") == score["data"].get("score"),
                       f"done.score={done['data'].get('score')} score事件={score['data'].get('score')}")
                print(f"  score={score['data'].get('score')} | usage={usage['data'] if usage else None}")

        print(f"\n[3] chat SSE 契约")
        await _verify_chat(client, token, review["review_id"])

    print(f"\n========== 结果: {len(_passed)} 通过 / {len(_failed)} 失败 ==========")
    if _failed:
        print(f"失败项: {_failed}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
