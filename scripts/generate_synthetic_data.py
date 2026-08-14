"""合成数据生成入口（P1.1）。

生成全套研发用假数据并落盘到 data/synthetic/：
- 专家/供应商/项目/标段/维度/投标/冲突关系/供应商关联（JSON）
- 标书全文（bid_content/*.txt，≥2000 字）

种子确定性：--seed 固定则输出完全一致（CI 与本地可复现）。

用法:
  poetry run python scripts/generate_synthetic_data.py
  poetry run python scripts/generate_synthetic_data.py --projects 5 --experts 30 --suppliers 20 --seed 42

落盘后校验:
  poetry run python scripts/validate_synthetic_data.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台 GBK 下中文输出乱码，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from scripts.synthetic.generators import INITIAL_PASSWORD, generate_all  # noqa: E402

DEFAULT_OUTPUT_DIR = Path("data/synthetic")


def write_json(path: Path, data: list[dict] | dict) -> None:
    """写 JSON（ensure_ascii=False 保留中文，indent=2 便于 diff）。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成合成数据（P1.1）")
    parser.add_argument("--projects", type=int, default=5, help="项目数（每项目 3 标段）")
    parser.add_argument("--experts", type=int, default=30, help="专家数")
    parser.add_argument("--suppliers", type=int, default=20, help="供应商数")
    parser.add_argument("--seed", type=int, default=20260811, help="随机种子（确定性）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="输出目录")
    args = parser.parse_args()

    output_dir: Path = args.output
    bid_content_dir = output_dir / "bid_content"
    bid_content_dir.mkdir(parents=True, exist_ok=True)

    print(f"[generate] 生成 {args.projects} 项目 / {args.experts} 专家 / {args.suppliers} 供应商 ...")
    result = generate_all(
        seed=args.seed,
        n_projects=args.projects,
        n_experts=args.experts,
        n_suppliers=args.suppliers,
    )
    result.bid_content_dir = str(bid_content_dir)

    # 标书全文写盘（validate 从文件校验字数）
    for bid_id, content in result.bid_contents.items():
        (bid_content_dir / f"{bid_id}.txt").write_text(content, encoding="utf-8")

    # 元数据
    meta = {
        "seed": args.seed,
        "projects": args.projects,
        "experts": args.experts,
        "suppliers": args.suppliers,
        "generated_at": "2026-08-11",  # 固定时间，保证文件内容可复现（diff 友好）
    }

    # 各实体 JSON
    write_json(output_dir / "meta.json", meta)
    write_json(output_dir / "users.json", result.users)
    write_json(output_dir / "experts.json", result.experts)
    write_json(output_dir / "suppliers.json", result.suppliers)
    write_json(output_dir / "projects.json", result.projects)
    write_json(output_dir / "lots.json", result.lots)
    write_json(output_dir / "dimensions.json", result.dimensions)
    write_json(output_dir / "bids.json", result.bids)
    write_json(output_dir / "conflicts.json", result.conflicts)
    write_json(output_dir / "supplier_links.json", result.supplier_links)

    print(f"[generate] 完成：{len(result.users)} 用户 / {len(result.experts)} 专家 / "
          f"{len(result.suppliers)} 供应商 / {len(result.projects)} 项目 / "
          f"{len(result.lots)} 标段 / {len(result.bids)} 投标 / "
          f"{len(result.conflicts)} 冲突关系 / {len(result.supplier_links)} 供应商关联")
    print(f"[generate] 输出目录: {output_dir}")
    print(f"[generate] 初始登录密码（所有账号统一）: {INITIAL_PASSWORD}")


if __name__ == "__main__":
    main()
