#!/usr/bin/env bash
# ============================================================
# 初始化演示数据（新机器跑完 docker compose up 后执行一次）
#
# 流程：建表 → Milvus/Neo4j 索引 → 合成数据生成/导入 → 标书正文强化
#       → 3 个演示场景推进（正常评审 / 冲突回避 / 围串标）
#
# 前置：
#   1. docker compose up -d 已启动且 /health/ready 全 ok
#   2. 本机已 poetry install（scripts 与 alembic 需在宿主机跑，见 README）
# 幂等：import_synthetic_mysql 为 TRUNCATE 重建语义，重复执行会重建合成数据
# 耗时：首次约 20 分钟（BGE-M3 模型下载 + 52 份标书向量化入库）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== [1/6] 建表（alembic upgrade head，幂等）=="
poetry run alembic upgrade head

echo "== [2/6] Milvus collection + Neo4j 索引/约束（幂等）=="
poetry run python scripts/init_milvus.py
poetry run python scripts/init_neo4j.py

echo "== [3/6] 生成合成数据（确定性 seed）+ 质量门禁校验 =="
poetry run python scripts/generate_synthetic_data.py --projects 5 --experts 30 --suppliers 20
poetry run python scripts/validate_synthetic_data.py

echo "== [4/6] 导入 MySQL + Neo4j（TRUNCATE 重建）=="
poetry run python scripts/import_synthetic_mysql.py
poetry run python scripts/import_synthetic_neo4j.py

echo "== [5/6] 52 份标书正文强化 + BGE-M3 向量化入库 Milvus（约 15 分钟，幂等）=="
poetry run python scripts/enrich_synthetic_bids.py

echo "== [6/6] 推进 3 个演示场景（LOT-008 EVALUATED / LOT-009 UNDER_REVIEW / LOT-007 PRE_SCREEN）=="
poetry run python scripts/advance_p7_scenarios.py

echo
echo "初始化完成。运行 ./scripts/demo.sh 观看 3 个业务场景演示。"
