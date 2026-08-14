"""合成数据 → Neo4j 导入（P1.1）。

把 data/synthetic/*.json 以 MERGE 幂等方式写入图库（可重复执行，不产生重复节点）。

节点与属性对齐 init_neo4j.py 的唯一约束（expertId / supplierId / bidId / lotId /
projectId / dimensionId）。Neo4j 仅存标识属性 + 关系所需属性，敏感字段不落图。

用法:
  poetry run python scripts/import_synthetic_neo4j.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from neo4j import GraphDatabase

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402

DEFAULT_DATA_DIR = Path("data/synthetic")


def _node_cypher(label: str, id_key: str, props: list[str]) -> str:
    """构造 MERGE 节点语句：MERGE (n:Label {idKey: $id}) SET n.prop=$prop ..."""
    set_clauses = ", ".join(f"n.{k}=${k}" for k in props if k != id_key)
    return f"MERGE (n:{label} {{{id_key}: ${id_key}}}) SET {set_clauses}"


def _merge_rel(
    session,
    a_match: str,
    b_match: str,
    rel_type: str,
    props: dict,
    a_val,
    b_val,
) -> None:
    """MERGE 关系并动态 SET 非 null 属性。

    Neo4j 关系属性不允许 null：null 值通过"不设置属性"表达（属性缺失），
    查询时 `r.endDate IS NULL` 能匹配缺失属性（当前任职语义）。
    """
    set_pairs = [f"r.{k}=${k}" for k, v in props.items() if v is not None]
    set_clause = (" SET " + ", ".join(set_pairs)) if set_pairs else ""
    params = {"a": a_val, "b": b_val, **{k: v for k, v in props.items() if v is not None}}
    session.run(f"{a_match} {b_match} MERGE (a)-[r:{rel_type}]->(b){set_clause}", **params)


def import_neo4j(data_dir: Path) -> None:
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        experts = json.loads((data_dir / "experts.json").read_text(encoding="utf-8"))
        suppliers = json.loads((data_dir / "suppliers.json").read_text(encoding="utf-8"))
        projects = json.loads((data_dir / "projects.json").read_text(encoding="utf-8"))
        lots = json.loads((data_dir / "lots.json").read_text(encoding="utf-8"))
        dimensions = json.loads((data_dir / "dimensions.json").read_text(encoding="utf-8"))
        bids = json.loads((data_dir / "bids.json").read_text(encoding="utf-8"))
        conflicts = json.loads((data_dir / "conflicts.json").read_text(encoding="utf-8"))
        supplier_links = json.loads((data_dir / "supplier_links.json").read_text(encoding="utf-8"))

        with driver.session() as session:
            # ========== 节点（MERGE 幂等） ==========
            for e in experts:
                session.run(
                    _node_cypher("Expert", "expertId", ["name", "organization", "region", "experience", "status"]),
                    expertId=e["expert_id"], name=e["name"], organization=e["organization"],
                    region=e["region"], experience=e["experience"], status=e["status"],
                )
            for s in suppliers:
                session.run(
                    _node_cypher("Supplier", "supplierId", ["name", "uniformCreditCode", "legalPerson", "industry", "scale", "blacklisted"]),
                    supplierId=s["supplier_id"], name=s["name"], uniformCreditCode=s["uniform_credit_code"],
                    legalPerson=s["legal_person"], industry=s["industry"], scale=s["scale"],
                    blacklisted=s["blacklisted"],
                )
            for p in projects:
                session.run(
                    _node_cypher("ProcurementProject", "projectId", ["projectCode", "name", "type", "region", "budget", "status"]),
                    projectId=p["project_id"], projectCode=p["project_code"], name=p["name"],
                    type=p["type"], region=p["region"], budget=p["budget"], status=p["status"],
                )
            for l in lots:
                session.run(
                    _node_cypher("Lot", "lotId", ["lotCode", "name", "budget", "status"]),
                    lotId=l["lot_id"], lotCode=l["lot_code"], name=l["name"],
                    budget=l["budget"], status=l["status"],
                )
            for d in dimensions:
                session.run(
                    _node_cypher("ScoringDimension", "dimensionId", ["name", "maxScore", "weight"]),
                    dimensionId=d["dimension_id"], name=d["name"],
                    maxScore=d["max_score"], weight=d["weight"],
                )
            for b in bids:
                session.run(
                    _node_cypher("BidDocument", "bidId", ["bidAmount", "status"]),
                    bidId=b["bid_id"], bidAmount=b["bid_amount"], status=b["status"],
                )
            print(f"[import] 节点: Expert {len(experts)} / Supplier {len(suppliers)} / "
                  f"Project {len(projects)} / Lot {len(lots)} / Dimension {len(dimensions)} / Bid {len(bids)}")

            # ========== 业务关系 ==========
            # CONTAINS_LOT（项目→标段）/ HAS_DIMENSION（标段→维度）
            # / BELONGS_TO（标书→标段）/ SUBMITTED_BY（标书→供应商）
            for l in lots:
                session.run(
                    "MATCH (a:ProcurementProject {projectId:$pid}), (b:Lot {lotId:$lid}) MERGE (a)-[:CONTAINS_LOT]->(b)",
                    pid=l["project_id"], lid=l["lot_id"],
                )
            for d in dimensions:
                session.run(
                    "MATCH (a:Lot {lotId:$lid}), (b:ScoringDimension {dimensionId:$did}) MERGE (a)-[:HAS_DIMENSION]->(b)",
                    lid=d["lot_id"], did=d["dimension_id"],
                )
            for b in bids:
                session.run(
                    "MATCH (a:BidDocument {bidId:$bid}), (b:Lot {lotId:$lid}) MERGE (a)-[:BELONGS_TO]->(b)",
                    bid=b["bid_id"], lid=b["lot_id"],
                )
                session.run(
                    "MATCH (a:BidDocument {bidId:$bid}), (b:Supplier {supplierId:$sid}) MERGE (a)-[:SUBMITTED_BY]->(b)",
                    bid=b["bid_id"], sid=b["supplier_id"],
                )
            print("[import] 业务关系: CONTAINS_LOT / HAS_DIMENSION / BELONGS_TO / SUBMITTED_BY")

            # ========== 专家回避冲突关系 ==========
            for c in conflicts:
                rtype = c["relation_type"]
                if rtype == "SAME_ORGANIZATION":
                    _merge_rel(
                        session,
                        "MATCH (a:Expert {expertId:$a})",
                        "MATCH (b:Expert {expertId:$b})",
                        "SAME_ORGANIZATION",
                        {"period": c.get("period")},
                        c["expert_a_id"], c["expert_b_id"],
                    )
                elif rtype == "HOLDS_SHARE":
                    _merge_rel(
                        session,
                        "MATCH (a:Expert {expertId:$a})",
                        "MATCH (b:Supplier {supplierId:$b})",
                        "HOLDS_SHARE",
                        {"ratio": c["ratio"]},
                        c["expert_id"], c["supplier_id"],
                    )
                elif rtype == "EMPLOYED_BY":
                    # endDate=None（当前任职）→ 属性缺失，可被 r.endDate IS NULL 匹配
                    _merge_rel(
                        session,
                        "MATCH (a:Expert {expertId:$a})",
                        "MATCH (b:Supplier {supplierId:$b})",
                        "EMPLOYED_BY",
                        {"role": c["role"], "startDate": c["start_date"], "endDate": c.get("end_date")},
                        c["expert_id"], c["supplier_id"],
                    )
                elif rtype == "RELATIVE_EMPLOYED":
                    _merge_rel(
                        session,
                        "MATCH (a:Expert {expertId:$a})",
                        "MATCH (b:Supplier {supplierId:$b})",
                        "RELATIVE_EMPLOYED",
                        {"relationType": c["relation_type_detail"], "relativeName": c["relative_name"]},
                        c["expert_id"], c["supplier_id"],
                    )
            print(f"[import] 专家回避关系: {len(conflicts)} 条")

            # ========== 供应商关联关系 ==========
            for link in supplier_links:
                rtype = link["relation_type"]
                if rtype == "SAME_CONTROLLER":
                    _merge_rel(
                        session,
                        "MATCH (a:Supplier {supplierId:$a})",
                        "MATCH (b:Supplier {supplierId:$b})",
                        "SAME_CONTROLLER",
                        {},
                        link["supplier_a_id"], link["supplier_b_id"],
                    )
                elif rtype == "AFFILIATE_OF":
                    _merge_rel(
                        session,
                        "MATCH (a:Supplier {supplierId:$a})",
                        "MATCH (b:Supplier {supplierId:$b})",
                        "AFFILIATE_OF",
                        {"relationType": link["relation_type_detail"]},
                        link["supplier_a_id"], link["supplier_b_id"],
                    )
                elif rtype == "BID_TOGETHER":
                    _merge_rel(
                        session,
                        "MATCH (a:Supplier {supplierId:$a})",
                        "MATCH (b:Supplier {supplierId:$b})",
                        "BID_TOGETHER",
                        {"projectId": link["project_id"], "times": link["times"]},
                        link["supplier_a_id"], link["supplier_b_id"],
                    )
            print(f"[import] 供应商关联: {len(supplier_links)} 条")
        print("[import] Neo4j 导入完成")
    finally:
        driver.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="合成数据导入 Neo4j（P1.1）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="数据目录")
    args = parser.parse_args()
    import_neo4j(args.data_dir)


if __name__ == "__main__":
    main()
