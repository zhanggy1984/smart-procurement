"""Neo4j 同步（P1.3 直接调用 / P1.6 worker 消费 outbox 复用）。

全部 MERGE 幂等：重复同步不产生重复节点/关系（Reconciliation 重放安全）。
属性命名与 scripts/init_neo4j.py 的唯一约束对齐（expertId/bidId/... camelCase）。
"""

from __future__ import annotations

from app.core import neo4j


async def upsert_project(project_id: str, **props) -> None:
    """MERGE ProcurementProject 节点并 SET 属性。"""
    driver = neo4j.get_driver()
    async with driver.session() as session:
        await session.run(
            "MERGE (p:ProcurementProject {projectId:$id}) "
            "SET p.projectCode=$projectCode, p.name=$name, p.type=$type, "
            "p.region=$region, p.budget=$budget, p.status=$status",
            id=project_id,
            projectCode=props.get("project_code"),
            name=props.get("name"),
            type=props.get("type"),
            region=props.get("region"),
            budget=str(props.get("budget")) if props.get("budget") else None,
            status=props.get("status"),
        )


async def upsert_lot(lot_id: str, project_id: str, **props) -> None:
    """MERGE Lot 节点 + CONTAINS_LOT 关系（项目→标段）。"""
    driver = neo4j.get_driver()
    async with driver.session() as session:
        await session.run(
            "MERGE (l:Lot {lotId:$id}) "
            "SET l.lotCode=$lotCode, l.name=$name, l.budget=$budget, l.status=$status",
            id=lot_id,
            lotCode=props.get("lot_code"),
            name=props.get("name"),
            budget=str(props.get("budget")) if props.get("budget") else None,
            status=props.get("status"),
        )
        await session.run(
            "MATCH (p:ProcurementProject {projectId:$pid}), (l:Lot {lotId:$lid}) "
            "MERGE (p)-[:CONTAINS_LOT]->(l)",
            pid=project_id,
            lid=lot_id,
        )


async def upsert_dimension(dimension_id: str, lot_id: str, **props) -> None:
    """MERGE ScoringDimension 节点 + HAS_DIMENSION 关系（标段→维度）。"""
    driver = neo4j.get_driver()
    async with driver.session() as session:
        await session.run(
            "MERGE (d:ScoringDimension {dimensionId:$id}) "
            "SET d.name=$name, d.maxScore=$maxScore, d.weight=$weight",
            id=dimension_id,
            name=props.get("name"),
            maxScore=float(props.get("max_score")) if props.get("max_score") else None,
            weight=float(props.get("weight")) if props.get("weight") else None,
        )
        await session.run(
            "MATCH (l:Lot {lotId:$lid}), (d:ScoringDimension {dimensionId:$did}) "
            "MERGE (l)-[:HAS_DIMENSION]->(d)",
            lid=lot_id,
            did=dimension_id,
        )


async def upsert_expert(expert_id: str, **props) -> None:
    """MERGE Expert 节点并 SET 属性。敏感字段（身份证等）不落图。

    与 init_neo4j.py 的 Expert 唯一约束对齐（expertId）。
    """
    driver = neo4j.get_driver()
    async with driver.session() as session:
        await session.run(
            "MERGE (e:Expert {expertId:$id}) "
            "SET e.name=$name, e.organization=$organization, e.region=$region, "
            "e.experience=$experience, e.status=$status",
            id=expert_id,
            name=props.get("name"),
            organization=props.get("organization"),
            region=props.get("region"),
            experience=props.get("experience"),
            status=props.get("status"),
        )


async def upsert_supplier(supplier_id: str, **props) -> None:
    """MERGE Supplier 节点并 SET 属性。"""
    driver = neo4j.get_driver()
    async with driver.session() as session:
        await session.run(
            "MERGE (s:Supplier {supplierId:$id}) "
            "SET s.name=$name, s.uniformCreditCode=$uniformCreditCode, "
            "s.legalPerson=$legalPerson, s.industry=$industry, s.scale=$scale, "
            "s.blacklisted=$blacklisted",
            id=supplier_id,
            name=props.get("name"),
            uniformCreditCode=props.get("uniform_credit_code"),
            legalPerson=props.get("legal_person"),
            industry=props.get("industry"),
            scale=props.get("scale"),
            blacklisted=props.get("blacklisted", False),
        )


async def upsert_conflict_relation(
    relation_type: str,
    *,
    expert_id: str,
    supplier_id: str | None = None,
    expert_b_id: str | None = None,
    **props,
) -> None:
    """MERGE 专家回避关系（幂等，对齐 import_synthetic_neo4j._merge_rel）。

    - 专家→供应商：EMPLOYED_BY（role/startDate/endDate）/ HOLDS_SHARE（ratio）/
      RELATIVE_EMPLOYED（relationType/relativeName）
    - 专家→专家：SAME_ORGANIZATION（period）

    None 属性通过"不设置"表达（Neo4j 关系属性不允许 null），
    `r.endDate IS NULL` 可匹配缺失属性 = 当前任职语义。
    """
    driver = neo4j.get_driver()
    rel_props = {k: v for k, v in props.items() if v is not None}
    set_clause = (" SET " + ", ".join(f"r.{k}=${k}" for k in rel_props)) if rel_props else ""
    if relation_type == "SAME_ORGANIZATION":
        cypher = (
            "MATCH (a:Expert {expertId:$a}), (b:Expert {expertId:$b}) "
            f"MERGE (a)-[r:{relation_type}]->(b){set_clause}"
        )
        b = expert_b_id
    else:
        cypher = (
            "MATCH (a:Expert {expertId:$a}), (b:Supplier {supplierId:$b}) "
            f"MERGE (a)-[r:{relation_type}]->(b){set_clause}"
        )
        b = supplier_id
    async with driver.session() as session:
        await session.run(cypher, a=expert_id, b=b, **rel_props)


async def upsert_bid(bid_id: str, lot_id: str, supplier_id: str, **props) -> None:
    """MERGE BidDocument 节点 + BELONGS_TO(标书→标段) + SUBMITTED_BY(标书→供应商)。

    关系方向按 solution.md：`(BidDocument)-[:BELONGS_TO]->(Lot)`、
    `(BidDocument)-[:SUBMITTED_BY]->(Supplier)`。MERGE 幂等，Reconciliation 重放安全。
    bidAmount 用字符串存图（与 upsert_lot 的 budget 一致，避免 Neo4j 数值精度问题）。
    """
    driver = neo4j.get_driver()
    amount = props.get("bid_amount")
    async with driver.session() as session:
        await session.run(
            "MERGE (b:BidDocument {bidId:$id}) "
            "SET b.bidAmount=$amount, b.status=$status",
            id=bid_id,
            amount=str(amount) if amount is not None else None,
            status=props.get("status"),
        )
        await session.run(
            "MATCH (b:BidDocument {bidId:$bid}), (l:Lot {lotId:$lot}) "
            "MERGE (b)-[:BELONGS_TO]->(l)",
            bid=bid_id,
            lot=lot_id,
        )
        await session.run(
            "MATCH (b:BidDocument {bidId:$bid}), (s:Supplier {supplierId:$sup}) "
            "MERGE (b)-[:SUBMITTED_BY]->(s)",
            bid=bid_id,
            sup=supplier_id,
        )


async def upsert_bid_together(supplier_a: str, supplier_b: str) -> None:
    """MERGE 供应商共投关系 BID_TOGETHER（P3.5 归档，无向；重复 MERGE 幂等）。"""
    driver = neo4j.get_driver()
    async with driver.session() as session:
        await session.run(
            "MATCH (a:Supplier {supplierId:$a}), (b:Supplier {supplierId:$b}) "
            "MERGE (a)-[:BID_TOGETHER]->(b)",
            a=supplier_a,
            b=supplier_b,
        )
