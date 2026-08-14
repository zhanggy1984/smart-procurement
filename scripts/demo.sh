#!/usr/bin/env bash
# ============================================================
# AI 智能评标系统 — 3 大业务场景演示
#
# 场景1 正常评审  ：数据治理标段 LOT-008（EVALUATED）
#                   展示 AI 智能评标闭环产出：报价公式打分 + 技术维度 AI 评分
#                   → 评标汇总排名 → 定标，供应商端可见中标结果
# 场景2 冲突回避  ：平台基础设施标段 LOT-009（UNDER_REVIEW）
#                   展示 Neo4j 图谱利益冲突检测：专家 EXP-005 与投标商 SUP-010
#                   持股回避 → 申报冲突 → 自动补充匹配专家 → 专家×维度评审矩阵
# 场景3 围串标检测：移动应用标段 LOT-007（PRE_SCREEN）
#                   展示围串标多路检测：SUP-012/013 同一实控人 + 报价集中
#                   + 标书文本高相似 → 综合风险 HIGH 59.2 → PM 风险待办
#
# 前置：
#   1. docker compose up -d 已启动（/health/ready 返回 ok）
#   2. 数据已初始化（新机器先执行 ./scripts/setup_demo.sh）
# 用法：./scripts/demo.sh   （项目根目录运行）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="http://localhost:8000/api/v1"

# 验收账号密码：优先读环境变量，兜底与系统初始密码一致（INITIAL_PASSWORD）
TEST_PASSWORD="${SP_TEST_PASSWORD:-Smart@2026}"

echo "=========================================="
echo "AI 智能评标系统 — 演示"
echo "=========================================="

echo ""
echo "== ① 健康检查（8 中间件/服务状态）=="
curl -s http://localhost:8000/health/ready | poetry run python -m json.tool

echo ""
echo "== ② 登录（admin / ${TEST_PASSWORD}）=="
TOKEN=$(curl -s -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"${TEST_PASSWORD}\"}" \
  | poetry run python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "  获取 access_token: ${TOKEN:0:24}..."
AUTH="Authorization: Bearer $TOKEN"

echo ""
echo "=========================================="
echo "【场景1】正常评审 — 数据治理标段 LOT-008（已定标 EVALUATED）"
echo "=========================================="
echo "-- 评标汇总（4 家 FROZEN 标书 × 5 维度，综合得分归一化排名）--"
curl -s "$BASE/lots/LOT-008/summary" -H "$AUTH" | poetry run python -m json.tool

echo ""
echo "=========================================="
echo "【场景2】冲突回避 — 平台基础设施标段 LOT-009（评审中 UNDER_REVIEW）"
echo "=========================================="
echo "-- 专家 × 维度评审矩阵（回避申报后进入评审，EXP-005 冲突已剔除/补入）--"
curl -s "$BASE/lots/LOT-009/reviews" -H "$AUTH" | poetry run python -m json.tool

echo ""
echo "=========================================="
echo "【场景3】围串标检测 — 移动应用标段 LOT-007（风险待办 PRE_SCREEN）"
echo "=========================================="
echo "-- 投标标书（SUP-012/013 同一实控人，深度检测综合风险 HIGH 59.2）--"
curl -s "$BASE/lots/LOT-007/bids" -H "$AUTH" | poetry run python -m json.tool

echo ""
echo "=========================================="
echo "演示结束。更多 API 见 solution.md 4.6 已实现清单；"
echo "前端界面 http://localhost:8080 （演示账号 admin/pm1/expert_01/supplier_01 @ ${TEST_PASSWORD}）"
echo "=========================================="
