"""合成数据质量门禁（P1.1 / P7.1）。

从 data/synthetic/*.json + bid_content/*.txt 校验 10 项门禁（P7.1 清单全量实现）：
1. 专业标签多样性 ≥5
2. 冲突密度 8%-15%（关系条数 / 专家数）
3. 每标段可用专家 ≥3（无任何回避冲突的专家）
4. 标书内容 ≥2000 字
5. ≥1 对 SAME_CONTROLLER + ≥1 对 BID_TOGETHER
6. 4 种回避类型各 ≥1 条
7. 每标段 4-5 个维度，权重和 = 1.0 ± 0.001
8. 每标段 ≥3 家投标供应商
9. 专家状态 ACTIVE ≥80%，含 INACTIVE/BLACKLISTED
10. 含 ≥1 个 blacklisted 供应商

任何一项失败 exit(1)。供 generate 后 / CI / P7.1 复用。

用法:
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

DEFAULT_DATA_DIR = Path("data/synthetic")

# 回避冲突类型（Neo4j 关系名）
AVOIDANCE_TYPES = {"EMPLOYED_BY", "HOLDS_SHARE", "SAME_ORGANIZATION", "RELATIVE_EMPLOYED"}


def load_json(path: Path) -> list | dict:
    if not path.exists():
        print(f"[validate] 缺少数据文件: {path}")
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def _conflict_experts(conflicts: list[dict]) -> set[str]:
    """返回所有涉及回避冲突的专家 id（HOLDS_SHARE/EMPLOYED_BY/RELATIVE_EMPLOYED 单方 + SAME_ORGANIZATION 双方）。"""
    result: set[str] = set()
    for c in conflicts:
        if c["relation_type"] == "SAME_ORGANIZATION":
            result.add(c["expert_a_id"])
            result.add(c["expert_b_id"])
        else:
            result.add(c["expert_id"])
    return result


def check_experts(experts: list[dict], data_dir: Path) -> tuple[bool, str]:
    """门禁 9：专家状态分布。"""
    if not experts:
        return False, "无专家数据"
    statuses = [e["status"] for e in experts]
    active_ratio = statuses.count("ACTIVE") / len(experts)
    has_inactive = "INACTIVE" in statuses
    has_blacklisted = "BLACKLISTED" in statuses
    ok = active_ratio >= 0.8 and has_inactive and has_blacklisted
    detail = f"ACTIVE={active_ratio:.1%} (≥80%), INACTIVE={has_inactive}, BLACKLISTED={has_blacklisted}"
    return ok, detail


def check_suppliers(suppliers: list[dict]) -> tuple[bool, str]:
    """门禁 10：含 blacklisted 供应商。"""
    blacklisted = [s for s in suppliers if s["blacklisted"]]
    ok = len(blacklisted) >= 1
    return ok, f"blacklisted 供应商 {len(blacklisted)} 个（≥1）"


def check_tag_diversity(experts: list[dict]) -> tuple[bool, str]:
    """门禁 1：专业标签多样性。"""
    all_tags = {tag for e in experts for tag in e["specializations"]}
    ok = len(all_tags) >= 5
    return ok, f"专业标签 {len(all_tags)} 个（≥5）: {sorted(all_tags)}"


def check_conflict_density(conflicts: list[dict], experts: list[dict]) -> tuple[bool, str]:
    """门禁 2：冲突密度 8%-15%。"""
    density = len(conflicts) / len(experts)
    ok = 0.08 <= density <= 0.15
    return ok, f"冲突密度 {density:.1%}（8%-15%），关系 {len(conflicts)} 条 / 专家 {len(experts)} 人"


def check_avoidance_coverage(conflicts: list[dict]) -> tuple[bool, str]:
    """门禁 6：4 种回避类型各 ≥1 条。"""
    covered = {c["relation_type"] for c in conflicts}
    missing = AVOIDANCE_TYPES - covered
    ok = not missing
    return ok, f"回避类型覆盖 {sorted(covered)}（缺: {sorted(missing) if missing else '无'}）"


def check_available_experts(experts: list[dict], conflicts: list[dict]) -> tuple[bool, str]:
    """门禁 3：无冲突专家数 ≥3（P1.1 无 assignment，按全局口径）。

    标段投标供应商已知，但专家未分配（P4.2 才匹配），故用全局无冲突专家数近似。
    """
    conflicted = _conflict_experts(conflicts)
    available = [e for e in experts if e["expert_id"] not in conflicted]
    ok = len(available) >= 3
    return ok, f"无冲突专家 {len(available)} 人（≥3），涉及冲突 {len(conflicted)} 人"


def check_bid_content(data_dir: Path, bids: list[dict]) -> tuple[bool, str]:
    """门禁 4：每份标书 ≥2000 字。"""
    content_dir = data_dir / "bid_content"
    too_short: list[str] = []
    total = 0
    for bid in bids:
        path = content_dir / f"{bid['bid_id']}.txt"
        if not path.exists():
            too_short.append(f"{bid['bid_id']}(缺失)")
            continue
        length = len(path.read_text(encoding="utf-8").strip())
        total += 1
        if length < 2000:
            too_short.append(f"{bid['bid_id']}({length}字)")
    ok = len(too_short) == 0 and total == len(bids)
    detail = f"标书 {total}/{len(bids)} 份达标，不足: {too_short if too_short else '无'}"
    return ok, detail


def check_supplier_links(supplier_links: list[dict]) -> tuple[bool, str]:
    """门禁 5：≥1 对 SAME_CONTROLLER + ≥1 对 BID_TOGETHER。"""
    types = [l["relation_type"] for l in supplier_links]
    has_controller = types.count("SAME_CONTROLLER") >= 1
    has_bid_together = types.count("BID_TOGETHER") >= 1
    ok = has_controller and has_bid_together
    return ok, f"SAME_CONTROLLER={has_controller}, BID_TOGETHER={has_bid_together}, AFFILIATE_OF={types.count('AFFILIATE_OF')}"


def check_dimensions(dimensions: list[dict], lots: list[dict]) -> tuple[bool, str]:
    """门禁 7：每标段 4-5 维度，权重和 = 1.0 ± 0.001。"""
    problems: list[str] = []
    for lot in lots:
        lot_dims = [d for d in dimensions if d["lot_id"] == lot["lot_id"]]
        count = len(lot_dims)
        weight_sum = sum(d["weight"] for d in lot_dims)
        if not (4 <= count <= 5):
            problems.append(f"{lot['lot_id']} 维度数={count}")
        if abs(weight_sum - 1.0) > 0.001:
            problems.append(f"{lot['lot_id']} 权重和={weight_sum:.4f}")
    ok = not problems
    return ok, f"维度门禁检查通过（问题: {problems if problems else '无'}）"


def check_bidder_count(bids: list[dict], lots: list[dict]) -> tuple[bool, str]:
    """门禁 8：每标段 ≥3 家投标供应商。"""
    problems: list[str] = []
    for lot in lots:
        bidders = {b["supplier_id"] for b in bids if b["lot_id"] == lot["lot_id"]}
        if len(bidders) < 3:
            problems.append(f"{lot['lot_id']} 仅 {len(bidders)} 家")
    ok = not problems
    return ok, f"每标段投标供应商 ≥3（问题: {problems if problems else '无'}）"


CHECKS: list[tuple[str, object]] = []


def main() -> None:
    parser = argparse.ArgumentParser(description="合成数据质量门禁")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="数据目录")
    args = parser.parse_args()
    data_dir: Path = args.data_dir

    experts = load_json(data_dir / "experts.json")
    suppliers = load_json(data_dir / "suppliers.json")
    lots = load_json(data_dir / "lots.json")
    dimensions = load_json(data_dir / "dimensions.json")
    bids = load_json(data_dir / "bids.json")
    conflicts = load_json(data_dir / "conflicts.json")
    supplier_links = load_json(data_dir / "supplier_links.json")

    results: list[tuple[str, bool, str]] = [
        ("专业标签多样性", *check_tag_diversity(experts)),
        ("冲突密度", *check_conflict_density(conflicts, experts)),
        ("可用专家数", *check_available_experts(experts, conflicts)),
        ("标书内容质量", *check_bid_content(data_dir, bids)),
        ("围串标测试数据", *check_supplier_links(supplier_links)),
        ("回避冲突覆盖", *check_avoidance_coverage(conflicts)),
        ("评分维度完整性", *check_dimensions(dimensions, lots)),
        ("投标供应商数", *check_bidder_count(bids, lots)),
        ("专家状态分布", *check_experts(experts, data_dir)),
        ("供应商黑名单", *check_suppliers(suppliers)),
    ]

    print(f"[validate] 共 {len(results)} 项门禁：\n")
    failed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
        if not ok:
            failed += 1

    print(f"\n[validate] 结果: {len(results) - failed}/{len(results)} 通过")
    if failed:
        sys.exit(1)
    print("[validate] 全部通过 ✓")


if __name__ == "__main__":
    main()
