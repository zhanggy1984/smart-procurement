"""企查查冲突导入 schema（P1.4）。"""

from __future__ import annotations

from pydantic import BaseModel


class ConflictImportResult(BaseModel):
    """CSV 批量导入结果。"""

    total: int              # CSV 有效行数
    matched: int            # 专家+供应商双匹配 → 写入 Neo4j
    pending: int            # 人匹配企业未匹配 → pending_conflict 冷数据
    person_unmatched: int   # 专家未匹配 → 跳过
    unknown_relation: int   # 双匹配但关系类型不在受控映射 → 跳过
