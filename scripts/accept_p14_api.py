"""P1.4 API 验收脚本（本地 uvicorn :8001）。

覆盖 task.md P1.4 验收：
- 15. POST /experts/import   Excel 30 行 → 全入库 → Neo4j Expert 30 节点
- 16. POST /suppliers/import Excel 20 行 → 全入库 → Neo4j Supplier 20 节点
- 17. POST /conflicts/import CSV 50 行 → 正确匹配 → Neo4j EMPLOYED_BY/HOLDS_SHARE
- 18. PUT /suppliers/{id}/status 拉黑 → 未封存标书 DISQUALIFIED + 非 AWARDED 评审 SUSPENDED
      + AWARDED 项目评审不变 + 非管理员 403 + supplier 不存在 404
- 附：冷数据唤醒闭环（pending → 供应商入库 → ACTIVATED + Neo4j 关系）

前置：已执行 reset_p14_data.py；本地 uvicorn 起在 :8001。

用法:
  poetry run python scripts/accept_p14_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from io import BytesIO
from pathlib import Path

import httpx
from neo4j import GraphDatabase
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402

BASE = "http://localhost:8001/api/v1"
DATA_DIR = Path("data/import_templates")

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """断言并统计验收结果。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def db() -> AsyncEngine:
    return create_async_engine(settings.database_url)


def neo4j_driver():
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD = os.environ.get("SP_TEST_PASSWORD", "123456")


async def login(username: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"登录失败: {r.status_code} {r.text}"
        return r.json()["access_token"]


async def import_file(token: str, endpoint: str, path: Path, mime: str) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (path.name, path.read_bytes(), mime)},
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        return r.status_code, body


def build_supplier_excel(rows: list[list]) -> bytes:
    """构造供应商 Excel（列头对齐 supplier 模板）。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["编号", "企业名称", "统一社会信用代码", "法定代表人", "所属行业", "企业规模"])
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def verify_neo4j(driver, cypher: str, **params) -> int:
    with driver.session() as session:
        result = session.run(cypher, **params)
        return result.single()[0]


async def main() -> None:
    global PASS, FAIL
    engine = await db()
    driver = neo4j_driver()

    # ==================== 清理历史验收残留（幂等重跑） ====================
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM expert_review WHERE review_id IN ('RVW-ACTIVE','RVW-AWARDED')"))
        await conn.execute(text("DELETE FROM pending_conflict"))
        await conn.execute(text("UPDATE project SET status='BIDDING' WHERE project_id='PRJ-005'"))

    # ==================== 登录（admin） ====================
    token = await login("admin", TEST_PASSWORD)
    check("admin 登录", bool(token))

    # ==================== 15. 专家导入 ====================
    print("\n[15] 专家导入（Excel 30 行）")
    status, body = await import_file(
        token, "experts/import", DATA_DIR / "expert_import.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    check("experts/import 状态码 201", status == 201, f"status={status} body={body}")
    check("专家全入库", isinstance(body, dict) and body.get("imported") == 30, str(body))
    async with engine.begin() as conn:
        cnt = (await conn.execute(text("SELECT COUNT(*) FROM expert"))).scalar_one()
        cnt_tags = (await conn.execute(text("SELECT COUNT(*) FROM expert_specialization"))).scalar_one()
    check("MySQL expert=30", cnt == 30, f"actual={cnt}")
    check("MySQL expert_specialization>0", cnt_tags > 0, f"actual={cnt_tags}")

    # ==================== 16. 供应商导入 ====================
    print("\n[16] 供应商导入（Excel 20 行）")
    status, body = await import_file(
        token, "suppliers/import", DATA_DIR / "supplier_import.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    check("suppliers/import 状态码 201", status == 201, f"status={status} body={body}")
    check("供应商全入库", isinstance(body, dict) and body.get("imported") == 20, str(body))
    async with engine.begin() as conn:
        cnt = (await conn.execute(text("SELECT COUNT(*) FROM supplier"))).scalar_one()
        users = (await conn.execute(
            text("SELECT COUNT(*) FROM users WHERE role IN ('REVIEW_EXPERT','SUPPLIER')")
        )).scalar_one()
    check("MySQL supplier=20", cnt == 20, f"actual={cnt}")
    check("登录账号自动创建=50", users == 50, f"actual={users}")

    # ==================== Neo4j 节点确认 ====================
    print("\n[Neo4j] 节点确认")
    experts_n = await verify_neo4j(driver, "MATCH (e:Expert) RETURN count(e)")
    suppliers_n = await verify_neo4j(driver, "MATCH (s:Supplier) RETURN count(s)")
    check("Neo4j Expert=30", experts_n == 30, f"actual={experts_n}")
    check("Neo4j Supplier=20", suppliers_n == 20, f"actual={suppliers_n}")

    # ==================== 17. 冲突 CSV 导入 ====================
    print("\n[17] 冲突 CSV 导入（50 行）")
    status, body = await import_file(token, "conflicts/import", DATA_DIR / "conflict_import.csv", "text/csv")
    check("conflicts/import 状态码 201", status == 201, f"status={status} body={body}")
    if isinstance(body, dict):
        check("matched=2（真实冲突幂等）", body.get("matched") == 2, str(body))
        check("pending=15（真人假企冷数据）", body.get("pending") == 15, str(body))
        check("person_unmatched=25（假人）", body.get("person_unmatched") == 25, str(body))
        check("unknown_relation=8（监事）", body.get("unknown_relation") == 8, str(body))
    async with engine.begin() as conn:
        pending = (await conn.execute(
            text("SELECT COUNT(*) FROM pending_conflict WHERE status='PENDING'")
        )).scalar_one()
    check("pending_conflict 落库=15", pending == 15, f"actual={pending}")

    # Neo4j 冲突关系确认（EXP-005→SUP-010 任职+持股，姓名匹配到重建后的专家/供应商）
    held = await verify_neo4j(driver, "MATCH (:Expert)-[r:EMPLOYED_BY]->(:Supplier) RETURN count(r)")
    share = await verify_neo4j(driver, "MATCH (:Expert)-[r:HOLDS_SHARE]->(:Supplier) RETURN count(r)")
    check("Neo4j EMPLOYED_BY>=1", held >= 1, f"actual={held}")
    check("Neo4j HOLDS_SHARE>=1", share >= 1, f"actual={share}")

    # ==================== 附：冷数据唤醒闭环 ====================
    print("\n[附加] 冷数据唤醒闭环（pending → 供应商入库 → ACTIVATED）")
    # 构造含"待注册科技有限公司01"的供应商 Excel（与 CSV pending 行的假企同名）
    supplier_excel = build_supplier_excel(
        [["SUP-TMP01", "待注册科技有限公司01", "", "张三", "软件和信息技术服务业", "中型"]]
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/suppliers/import",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("wake.xlsx", supplier_excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    check("唤醒供应商导入 201", r.status_code == 201, r.text)
    async with engine.begin() as conn:
        activated = (await conn.execute(
            text("SELECT COUNT(*) FROM pending_conflict WHERE company_name='待注册科技有限公司01' AND status='ACTIVATED'")
        )).scalar_one()
    check("pending 被激活=1", activated == 1, f"actual={activated}")

    # ==================== 18. 供应商黑名单级联 ====================
    print("\n[18] 供应商黑名单级联")
    # 前置：PRJ-005 置为 AWARDED（构造"已定标不受影响"对照）
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE project SET status='AWARDED' WHERE project_id='PRJ-005'"))
        # 非 AWARDED 项目下 SUP-006 的标书（合成数据 LOT-004 场景2 投标人）
        bid_active = (await conn.execute(text(
            "SELECT bd.bid_id, bd.lot_id FROM bid_document bd "
            "JOIN lot l ON bd.lot_id=l.lot_id JOIN project p ON l.project_id=p.project_id "
            "WHERE bd.supplier_id='SUP-006' AND p.status!='AWARDED' LIMIT 1"
        ))).first()
        # AWARDED 项目（PRJ-005）下任一份标书
        bid_awarded = (await conn.execute(text(
            "SELECT bd.bid_id, bd.lot_id, bd.supplier_id FROM bid_document bd "
            "JOIN lot l ON bd.lot_id=l.lot_id JOIN project p ON l.project_id=p.project_id "
            "WHERE p.status='AWARDED' LIMIT 1"
        ))).first()
        assert bid_active and bid_awarded, "验收前置：需要 SUP-006 标书与 PRJ-005 标书"
        # 各插一条评审（DRAFT，维度取该标段任意维度）
        dim_active = (await conn.execute(text(
            "SELECT dimension_id FROM scoring_dimension WHERE lot_id=:lot LIMIT 1"
        ), {"lot": bid_active.lot_id})).scalar_one()
        dim_awarded = (await conn.execute(text(
            "SELECT dimension_id FROM scoring_dimension WHERE lot_id=:lot LIMIT 1"
        ), {"lot": bid_awarded.lot_id})).scalar_one()
        await conn.execute(text(
            "INSERT INTO expert_review (review_id, expert_id, bid_id, dimension_id, status) "
            "VALUES ('RVW-ACTIVE', 'EXP-001', :bid, :dim, 'DRAFT')"
        ), {"bid": bid_active.bid_id, "dim": dim_active})
        await conn.execute(text(
            "INSERT INTO expert_review (review_id, expert_id, bid_id, dimension_id, status) "
            "VALUES ('RVW-AWARDED', 'EXP-001', :bid, :dim, 'CONFIRMED')"
        ), {"bid": bid_awarded.bid_id, "dim": dim_awarded})

    # 拉黑 SUP-006
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.put(
            f"{BASE}/suppliers/SUP-006/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"blacklisted": True},
        )
    check("拉黑返回 200", r.status_code == 200, r.text)

    async with engine.begin() as conn:
        active_bid_status = (await conn.execute(text(
            "SELECT status FROM bid_document WHERE bid_id=:bid"
        ), {"bid": bid_active.bid_id})).scalar_one()
        active_rev = (await conn.execute(text(
            "SELECT status, previous_status FROM expert_review WHERE review_id='RVW-ACTIVE'"
        ))).first()
        awarded_bid_status = (await conn.execute(text(
            "SELECT status FROM bid_document WHERE bid_id=:bid"
        ), {"bid": bid_awarded.bid_id})).scalar_one()
        awarded_rev = (await conn.execute(text(
            "SELECT status FROM expert_review WHERE review_id='RVW-AWARDED'"
        ))).first()
    check("未封存标书→DISQUALIFIED", active_bid_status == "DISQUALIFIED", f"actual={active_bid_status}")
    check("非 AWARDED 评审→SUSPENDED+快照", active_rev.status == "SUSPENDED" and active_rev.previous_status == "DRAFT",
          f"actual={active_rev}")
    check("AWARDED 项目标书不变", awarded_bid_status == "SUBMITTED", f"actual={awarded_bid_status}")
    check("AWARDED 项目评审不变", awarded_rev.status == "CONFIRMED", f"actual={awarded_rev}")
    # supplier 状态与 Neo4j
    async with engine.begin() as conn:
        sup = (await conn.execute(text(
            "SELECT blacklisted, status FROM supplier WHERE supplier_id='SUP-006'"
        ))).first()
    check("供应商 blacklisted=True", sup.blacklisted == 1, f"actual={sup}")
    check("供应商 status=INACTIVE", sup.status == "INACTIVE", f"actual={sup.status}")

    # 解除拉黑 → 评审还原（标书不还原）
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.put(
            f"{BASE}/suppliers/SUP-006/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"blacklisted": False},
        )
    check("解除拉黑返回 200", r.status_code == 200, r.text)
    async with engine.begin() as conn:
        restored = (await conn.execute(text(
            "SELECT status, previous_status FROM expert_review WHERE review_id='RVW-ACTIVE'"
        ))).first()
        bid_after = (await conn.execute(text(
            "SELECT status FROM bid_document WHERE bid_id=:bid"
        ), {"bid": bid_active.bid_id})).scalar_one()
    check("评审按 previous_status 还原", restored.status == "DRAFT" and restored.previous_status is None,
          f"actual={restored}")
    check("标书保持 DISQUALIFIED（废标不可逆）", bid_after == "DISQUALIFIED", f"actual={bid_after}")

    # ==================== 错误路径 ====================
    print("\n[错误路径]")
    # 非管理员 403：用专家账号
    expert_token = await login("expert_01", TEST_PASSWORD)
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.put(
            f"{BASE}/suppliers/SUP-006/status",
            headers={"Authorization": f"Bearer {expert_token}"},
            json={"blacklisted": True},
        )
    check("非管理员拉黑 403", r.status_code == 403, f"status={r.status_code}")
    # supplier 不存在 404
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.put(
            f"{BASE}/suppliers/SUP-999/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"blacklisted": True},
        )
    check("supplier 不存在 404", r.status_code == 404, f"status={r.status_code}")
    # 空文件 400
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/experts/import",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("empty.xlsx", b"", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    check("空文件 400", r.status_code == 400, f"status={r.status_code}")
    # 格式错误 422（缺列 CSV）
    bad_csv = ("﻿" + "姓名,关系类型\n张三,任职\n").encode("utf-8")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{BASE}/conflicts/import",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("bad.csv", bad_csv, "text/csv")},
        )
    check("CSV 缺列 422", r.status_code == 422, f"status={r.status_code}")

    # ==================== 清理：恢复验收前状态（AWARDED 改回） ====================
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE project SET status='BIDDING' WHERE project_id='PRJ-005'"))
        await conn.execute(text("DELETE FROM expert_review WHERE review_id IN ('RVW-ACTIVE','RVW-AWARDED')"))
    print("\n[cleanup] PRJ-005 状态与临时评审已还原")

    driver.close()
    await engine.dispose()
    print(f"\n========== 验收结果: PASS={PASS} FAIL={FAIL} ==========")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
