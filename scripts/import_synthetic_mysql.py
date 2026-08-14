"""合成数据 → MySQL 导入（P1.1）。

把 data/synthetic/*.json 灌入 MySQL 各表。默认"清空重建"语义（TRUNCATE 后
插入），保证脚本可重复执行（P7.7）。全部逻辑外键无 DB 约束，TRUNCATE 顺序无关。

身份证字段：id_number_hash 存 SHA256（确定性，供去重匹配），id_number_encrypted
留空——正式 Fernet 加密由 P1.2 core/crypto.py 接管（合成数据非真实身份证）。

用法:
  poetry run python scripts/import_synthetic_mysql.py
  poetry run python scripts/import_synthetic_mysql.py --no-clear
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import create_async_engine

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402

DEFAULT_DATA_DIR = Path("data/synthetic")

# 需要清空重建的表（P1.1 覆盖范围；评审/通知等表不动）
TABLES_TO_CLEAR = [
    "lot_expert_criteria",
    "scoring_criterion",
    "scoring_dimension",
    "bid_document",
    "lot",
    "project",
    "expert_specialization",
    "expert",
    "supplier",
    "users",
]


def _hash_id_number(id_number: str) -> str:
    """身份证号 SHA256 哈希（P1.1 占位；P1.2 起由 core/crypto.py 统一）。"""
    return hashlib.sha256(id_number.encode("utf-8")).hexdigest()


async def import_all(data_dir: Path, clear: bool) -> None:
    """清空（可选）+ 插入全部合成数据。"""
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            meta = MetaData()
            await conn.run_sync(meta.reflect)

            if clear:
                print("[import] 清空重建模式：TRUNCATE 全部导入表...")
                for table in TABLES_TO_CLEAR:
                    await conn.execute(text(f"TRUNCATE TABLE {table}"))
                print(f"[import] 已清空 {len(TABLES_TO_CLEAR)} 张表")

            users = json.loads((data_dir / "users.json").read_text(encoding="utf-8"))
            experts = json.loads((data_dir / "experts.json").read_text(encoding="utf-8"))
            suppliers = json.loads((data_dir / "suppliers.json").read_text(encoding="utf-8"))
            projects = json.loads((data_dir / "projects.json").read_text(encoding="utf-8"))
            lots = json.loads((data_dir / "lots.json").read_text(encoding="utf-8"))
            dimensions = json.loads((data_dir / "dimensions.json").read_text(encoding="utf-8"))
            bids = json.loads((data_dir / "bids.json").read_text(encoding="utf-8"))

            t = meta.tables

            # ---- users ----
            await conn.execute(
                t["users"].insert(),
                [
                    {
                        "user_id": u["user_id"],
                        "username": u["username"],
                        "password_hash": u["password_hash"],
                        "role": u["role"],
                        "display_name": u["display_name"],
                        "email": u.get("email"),
                        "phone": u.get("phone"),
                        "is_active": u["is_active"],
                    }
                    for u in users
                ],
            )
            print(f"[import] users: {len(users)}")

            # ---- expert + expert_specialization ----
            spec_rows: list[dict] = []
            for e in experts:
                for tag in e["specializations"]:
                    spec_rows.append({"expert_id": e["expert_id"], "tag": tag})
            await conn.execute(
                t["expert"].insert(),
                [
                    {
                        "expert_id": e["expert_id"],
                        "user_id": e["user_id"],
                        "name": e["name"],
                        "organization": e["organization"],
                        "region": e["region"],
                        "experience": e["experience"],
                        "email": e.get("email"),
                        "phone": e.get("phone"),
                        # P1.1 占位：hash 可逆匹配，encrypted 由 P1.2 crypto.py 接管
                        "id_number_encrypted": "",
                        "id_number_hash": _hash_id_number(e["id_number"]),
                        "status": e["status"],
                    }
                    for e in experts
                ],
            )
            await conn.execute(t["expert_specialization"].insert(), spec_rows)
            print(f"[import] expert: {len(experts)}, specialization: {len(spec_rows)}")

            # ---- supplier ----
            await conn.execute(
                t["supplier"].insert(),
                [
                    {
                        "supplier_id": s["supplier_id"],
                        "name": s["name"],
                        "uniform_credit_code": s["uniform_credit_code"],
                        "legal_person": s["legal_person"],
                        "industry": s["industry"],
                        "scale": s["scale"],
                        "blacklisted": s["blacklisted"],
                        "status": s["status"],
                    }
                    for s in suppliers
                ],
            )
            print(f"[import] supplier: {len(suppliers)}")

            # ---- project / lot / lot_expert_criteria ----
            await conn.execute(
                t["project"].insert(),
                [
                    {
                        "project_id": p["project_id"],
                        "project_code": p["project_code"],
                        "name": p["name"],
                        "type": p["type"],
                        "region": p["region"],
                        "budget": p["budget"],
                        "status": p["status"],
                    }
                    for p in projects
                ],
            )
            await conn.execute(
                t["lot"].insert(),
                [
                    {
                        "lot_id": l["lot_id"],
                        "project_id": l["project_id"],
                        "lot_code": l["lot_code"],
                        "name": l["name"],
                        "budget": l["budget"],
                        "status": l["status"],
                    }
                    for l in lots
                ],
            )
            await conn.execute(
                t["lot_expert_criteria"].insert(),
                [
                    {
                        "lot_id": l["lot_id"],
                        **l["expert_criteria"],
                    }
                    for l in lots
                ],
            )
            print(f"[import] project: {len(projects)}, lot: {len(lots)}, lot_expert_criteria: {len(lots)}")

            # ---- scoring_dimension + scoring_criterion ----
            criterion_rows: list[dict] = []
            for d in dimensions:
                for c in d["criteria"]:
                    criterion_rows.append(
                        {
                            "criterion_id": c["criterion_id"],
                            "dimension_id": d["dimension_id"],
                            "name": c["name"],
                            "description": c["description"],
                            "scoring_rubric": c["scoring_rubric"],
                            "max_score": c["max_score"],
                            "sort_order": c["sort_order"],
                        }
                    )
            await conn.execute(
                t["scoring_dimension"].insert(),
                [
                    {
                        "dimension_id": d["dimension_id"],
                        "lot_id": d["lot_id"],
                        "name": d["name"],
                        "max_score": d["max_score"],
                        "weight": d["weight"],
                        "sort_order": d["sort_order"],
                    }
                    for d in dimensions
                ],
            )
            await conn.execute(t["scoring_criterion"].insert(), criterion_rows)
            print(f"[import] dimension: {len(dimensions)}, criterion: {len(criterion_rows)}")

            # ---- bid_document ----
            await conn.execute(
                t["bid_document"].insert(),
                [
                    {
                        "bid_id": b["bid_id"],
                        "lot_id": b["lot_id"],
                        "supplier_id": b["supplier_id"],
                        "bid_amount": b["bid_amount"],
                        "duration": b["duration"],
                        "team_size": b["team_size"],
                        "structured_data": b["structured_data"],
                        # file_url 留空：MinIO 上传在 P1.5 接入
                        "file_url": "",
                        "status": b["status"],
                        "parsing_step": 0,
                    }
                    for b in bids
                ],
            )
            print(f"[import] bid_document: {len(bids)}")

        print("[import] MySQL 导入完成")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="合成数据导入 MySQL（P1.1）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="数据目录")
    parser.add_argument("--no-clear", action="store_true", help="不清空表（追加模式）")
    args = parser.parse_args()
    asyncio.run(import_all(args.data_dir, clear=not args.no_clear))


if __name__ == "__main__":
    main()
