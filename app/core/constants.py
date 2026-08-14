"""业务受控值常量（P1.3 起）。

与 scripts/synthetic/common.py 保持一致（同源：solution.md 受控值设计）。
P4.1 起受控词表改为管理员维护（DB 表），此文件为运行时的基础词表。

注意：此文件是"当前受控值的唯一事实来源"，导入校验（P1.4）与
项目创建校验（P1.3）共用，严禁各模块自造字面量。
"""

from __future__ import annotations

# ==================== 地区（受控下拉框） ====================
REGIONS = ("华东", "华南", "华北", "华中", "西南", "西北", "东北")

# ==================== 项目类型（受控） ====================
PROJECT_TYPES = ("GOODS", "SERVICE", "ENGINEERING")

# ==================== 专家专业标签（受控词表） ====================
EXPERT_TAGS = (
    "教育信息化",
    "软件开发",
    "系统集成",
    "网络安全",
    "人工智能",
    "大数据",
    "云计算",
    "物联网",
    "电子政务",
    "智慧城市",
    "安防监控",
    "通信工程",
    "医疗信息化",
    "金融科技",
    "能源信息化",
    "交通信息化",
)

# ==================== 项目状态机 ====================
# solution.md 1.1：DRAFT→BIDDING→UNDER_REVIEW→AWARDED（终态）
PROJECT_STATUSES = ("DRAFT", "BIDDING", "UNDER_REVIEW", "AWARDED")
# 标段：BIDDING→PRE_SCREEN→UNDER_REVIEW→EVALUATED，+ABANDONED/DISQUALIFIED
LOT_STATUSES = ("BIDDING", "PRE_SCREEN", "UNDER_REVIEW", "EVALUATED", "ABANDONED", "DISQUALIFIED")

# 权重和校验容差（SUM(weight)=1.0±0.001）
WEIGHT_SUM_TOLERANCE = 0.001

# ==================== 企查查关系类型 → Neo4j 关系类型（P1.4） ====================
# 企查查 CSV/pending_conflict 的 relation_type 为中文（股东/任职/法定代表人），
# 映射到 Neo4j 回避关系。股东→持股，任职/法定代表人→当前任职（endDate 缺失语义）。
QCC_RELATION_TO_NEO4J = {
    "股东": "HOLDS_SHARE",
    "任职": "EMPLOYED_BY",
    "法定代表人": "EMPLOYED_BY",
}
