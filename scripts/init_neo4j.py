"""Neo4j 初始化脚本 — 幂等创建节点/关系索引与 constraint。

设计依据：solution.md 1.4 节索引定义。全部使用 IF NOT EXISTS 幂等执行，
应用启动与手动执行均安全。

Neo4j 5 语法：
- 索引:  CREATE INDEX index_name IF NOT EXISTS FOR (n:Label) ON (n.prop)
- 约束:  CREATE CONSTRAINT name IF NOT EXISTS FOR (n:Label) REQUIRE n.prop IS UNIQUE

用法: poetry run python scripts/init_neo4j.py
"""

import sys

from neo4j import GraphDatabase

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402


# 节点索引（单属性）— 注意：主键属性（expertId 等）不建普通索引，
# 由下方 UNIQUE 约束的 backing index 覆盖，避免同一 label+property 上
# 普通索引与约束冲突（Neo4j 5 不允许共存）。
NODE_INDEXES = [
    "CREATE INDEX idx_expert_status IF NOT EXISTS FOR (e:Expert) ON (e.status)",
]

# 复合索引（候选搜索加速）
COMPOSITE_INDEXES = [
    "CREATE INDEX idx_expert_status_region IF NOT EXISTS FOR (e:Expert) ON (e.status, e.region)",
]

# 关系索引（冲突检测和供应商关联遍历加速）
RELATIONSHIP_INDEXES = [
    "CREATE INDEX idx_holds_share_ratio IF NOT EXISTS FOR ()-[r:HOLDS_SHARE]-() ON (r.ratio)",
    "CREATE INDEX idx_employed_by_end IF NOT EXISTS FOR ()-[r:EMPLOYED_BY]-() ON (r.endDate)",
]

# 唯一约束（保证业务主键唯一，MERGE 依赖）
CONSTRAINTS = [
    "CREATE CONSTRAINT uniq_expert_id IF NOT EXISTS FOR (e:Expert) REQUIRE e.expertId IS UNIQUE",
    "CREATE CONSTRAINT uniq_supplier_id IF NOT EXISTS FOR (s:Supplier) REQUIRE s.supplierId IS UNIQUE",
    "CREATE CONSTRAINT uniq_bid_id IF NOT EXISTS FOR (b:BidDocument) REQUIRE b.bidId IS UNIQUE",
    "CREATE CONSTRAINT uniq_lot_id IF NOT EXISTS FOR (l:Lot) REQUIRE l.lotId IS UNIQUE",
    "CREATE CONSTRAINT uniq_project_id IF NOT EXISTS FOR (p:ProcurementProject) REQUIRE p.projectId IS UNIQUE",
    "CREATE CONSTRAINT uniq_dimension_id IF NOT EXISTS FOR (d:ScoringDimension) REQUIRE d.dimensionId IS UNIQUE",
]


def init_neo4j() -> None:
    """连接 Neo4j 并幂等执行全部索引/constraint 创建。"""
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    all_statements = (
        NODE_INDEXES + COMPOSITE_INDEXES + RELATIONSHIP_INDEXES + CONSTRAINTS
    )
    try:
        with driver.session() as session:
            for stmt in all_statements:
                session.run(stmt)
                print(f"OK: {stmt}")
        print(f"\n[init_neo4j] 完成，共 {len(all_statements)} 条语句")
    finally:
        driver.close()


if __name__ == "__main__":
    init_neo4j()
