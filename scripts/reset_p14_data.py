"""重置专家/供应商数据（P1.4 验收前置；P7.7 重跑可复用）。

- MySQL：删除专家/供应商登录账号（users.role IN REVIEW_EXPERT/SUPPLIER，保留 admin/PM）
  + TRUNCATE expert_specialization / expert / supplier
- Neo4j：DETACH DELETE 全部 Expert / Supplier 节点（关联关系随之删除）

不重置 bid_document / project / lot 等业务表：它们引用供应商/专家 ID 为逻辑外键
（无 DB 约束），悬空可接受。P1.4 验收后用 API 导入（Excel 带合成 ID）重建专家/供应商，
再跑 import_synthetic_neo4j.py 幂等重导即可恢复完整知识图谱。

用法:
  poetry run python scripts/reset_p14_data.py
"""

from __future__ import annotations

import asyncio
import sys

from neo4j import GraphDatabase
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402


async def reset_mysql() -> None:
    """删除专家/供应商账号 + 清空专家/供应商表。"""
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE role IN ('REVIEW_EXPERT', 'SUPPLIER')")
            )
            await conn.execute(text("TRUNCATE TABLE expert_specialization"))
            await conn.execute(text("TRUNCATE TABLE expert"))
            await conn.execute(text("TRUNCATE TABLE supplier"))
        print("[reset] MySQL: 专家/供应商账号与记录已清空（admin/PM 保留）")
    finally:
        await engine.dispose()


def reset_neo4j() -> None:
    """DETACH DELETE 全部 Expert/Supplier 节点（关系随之删除）。"""
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        with driver.session() as session:
            summary = session.run("MATCH (n:Expert) DETACH DELETE n").consume()
            print(f"[reset] Neo4j Expert 节点删除 {summary.counters.nodes_deleted} 个")
            summary = session.run("MATCH (n:Supplier) DETACH DELETE n").consume()
            print(f"[reset] Neo4j Supplier 节点删除 {summary.counters.nodes_deleted} 个")
    finally:
        driver.close()


async def main() -> None:
    await reset_mysql()
    reset_neo4j()
    print("[reset] 完成：专家/供应商数据已重置，等待 API 导入重建")


if __name__ == "__main__":
    asyncio.run(main())
