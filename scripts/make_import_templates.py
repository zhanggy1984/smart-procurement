"""生成 P1.4 导入模板（Excel + CSV），供 API 验收（P7.7 重跑可复用）。

从 data/synthetic/*.json 读取合成数据，生成到 data/import_templates/：
- expert_import.xlsx        30 行：姓名/单位/地区/从业年限/专业标签/身份证号/邮箱/电话
- supplier_import.xlsx      20 行：企业名称/统一社会信用代码/法定代表人/所属行业/企业规模
- conflict_import.csv       50 行：企查查风格（姓名/企业名称/统一社会信用代码/关系类型/职位/持股比例），
  覆盖全部导入分支：
    * 真实冲突（合成数据 EMPLOYED_BY/HOLDS_SHARE）→ matched（Neo4j 幂等重放，不新增污染）
    * 真人假企 → pending 冷数据（供应商入库后唤醒）
    * 假人真企 / 双假 → person_unmatched
    * 未知关系类型（监事）→ unknown_relation

列头与 app/core/importer.py 的 HEADERS 保持一致（改动需同步）。

用法:
  poetry run python scripts/make_import_templates.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from openpyxl import Workbook

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_DATA_DIR = Path("data/synthetic")
DEFAULT_OUT_DIR = Path("data/import_templates")

# 内部验收模板：保留"编号"列复用合成 ID（与 Neo4j 重导对齐）。
# 对外下载模板（app/api/v1/import_templates.py，列头取自 importer）不含编号列；
# 上传时编号作为多余列被 importer 容忍，service 优先复用（row.get("编号")）。
EXPERT_HEADERS = ["编号", "姓名", "单位", "地区", "从业年限", "专业标签", "身份证号", "邮箱", "电话"]
SUPPLIER_HEADERS = ["编号", "企业名称", "统一社会信用代码", "法定代表人", "所属行业", "企业规模"]
CONFLICT_HEADERS = ["姓名", "企业名称", "统一社会信用代码", "关系类型", "职位", "持股比例"]

# 企查查无法覆盖的回避类型（SAME_ORGANIZATION / RELATIVE_EMPLOYED 走专家自申报），CSV 不表达
CSV_EXPRESSIBLE = {"EMPLOYED_BY", "HOLDS_SHARE"}


def _write_xlsx(path: Path, headers: list[str], rows: list[list]) -> None:
    """写 Excel（首行表头 + 数据行）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "sheet1"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    print(f"[template] {path} ({len(rows)} 行)")


def build_expert_rows(experts: list[dict]) -> list[list]:
    """合成数据专家 → Excel 行（首列编号复用合成 ID，与 Neo4j 重导对齐）。专业标签用分号分隔。"""
    rows = []
    for e in experts:
        rows.append(
            [
                e["expert_id"],
                e["name"],
                e["organization"],
                e["region"],
                e["experience"],
                ";".join(e["specializations"]),
                e["id_number"],
                e["email"],
                e["phone"],
            ]
        )
    return rows


def build_supplier_rows(suppliers: list[dict]) -> list[list]:
    """合成数据供应商 → Excel 行（首列编号复用合成 ID）。"""
    return [
        [s["supplier_id"], s["name"], s["uniform_credit_code"], s["legal_person"], s["industry"], s["scale"]]
        for s in suppliers
    ]


def build_conflict_rows(experts: list[dict], suppliers: list[dict], conflicts: list[dict]) -> list[list]:
    """构造 50 行企查查风格 CSV，覆盖全部导入分支。

    匹配口径与 conflict_service 一致：姓名→专家，信用代码优先其次企业名→供应商。
    """
    name_by_id = {e["expert_id"]: e["name"] for e in experts}
    by_id = {s["supplier_id"]: s for s in suppliers}
    rows: list[list] = []

    # 1. 真实冲突幂等重放（matched，Neo4j MERGE 不新增）
    for c in conflicts:
        if c["relation_type"] not in CSV_EXPRESSIBLE:
            continue
        supplier = by_id[c["supplier_id"]]
        if c["relation_type"] == "EMPLOYED_BY":
            rows.append(
                [name_by_id[c["expert_id"]], supplier["name"], supplier["uniform_credit_code"],
                 "任职", c.get("role") or "董事", ""]
            )
        else:  # HOLDS_SHARE
            rows.append(
                [name_by_id[c["expert_id"]], supplier["name"], supplier["uniform_credit_code"],
                 "股东", "", str(c.get("ratio", 0.05))]
            )

    # 2. 真人 + 假企 → pending 冷数据（15 行）
    pending_names = [e["name"] for e in experts[:15]]
    for i, name in enumerate(pending_names):
        rows.append([name, f"待注册科技有限公司{i + 1:02d}", "", "任职", "监事", ""])

    # 3. 假人 + 真企 → person_unmatched（15 行）
    for i, s in enumerate(suppliers[:15]):
        rows.append([f"测试人员{i + 1:02d}", s["name"], s["uniform_credit_code"], "股东", "", "0.05"])

    # 4. 双假（假人 + 假企）→ person_unmatched（10 行）
    for i in range(10):
        rows.append([f"外部人员{i + 1:02d}", f"未知企业{i + 1:02d}", "", "任职", "执行董事", ""])

    # 5. 未知关系类型（真实专家 + 真实企业 + "监事"）→ unknown_relation（8 行）
    for i, s in enumerate(suppliers[-8:]):
        rows.append([experts[-(i + 1)]["name"], s["name"], s["uniform_credit_code"], "监事", "监事", ""])

    assert len(rows) == 50, f"冲突模板必须 50 行，实际 {len(rows)}"
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 P1.4 导入模板（P7.7 可复用）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="合成数据目录")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="模板输出目录")
    args = parser.parse_args()

    experts = json.loads((args.data_dir / "experts.json").read_text(encoding="utf-8"))
    suppliers = json.loads((args.data_dir / "suppliers.json").read_text(encoding="utf-8"))
    conflicts = json.loads((args.data_dir / "conflicts.json").read_text(encoding="utf-8"))

    _write_xlsx(args.out_dir / "expert_import.xlsx", EXPERT_HEADERS, build_expert_rows(experts))
    _write_xlsx(args.out_dir / "supplier_import.xlsx", SUPPLIER_HEADERS, build_supplier_rows(suppliers))

    # conflict CSV 用 utf-8-sig（带 BOM），impoter 用 utf-8-sig 解码
    csv_rows = build_conflict_rows(experts, suppliers, conflicts)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "conflict_import.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CONFLICT_HEADERS)
        writer.writerows(csv_rows)
    print(f"[template] {args.out_dir / 'conflict_import.csv'} ({len(csv_rows)} 行)")


if __name__ == "__main__":
    main()
