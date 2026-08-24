"""4.3 标准契约清单端点（平台定标准，agent 适配）。

统一 `GET /api/contracts`（公开无鉴权），声明本 agent 的 LLM 评测接口与场景清单。
平台脚手架读此端点做接口自动发现（决策 #55/#56）。llm=false 为辅助接口（登录等），
只登记不进 agent_interface。interfaces[].path 为业务路径，与平台 seed_data 一致。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/contracts", tags=["contracts"])

MANIFEST = {
    "agent": "smart-procurement",
    "contract_version": "1.0",
    "interfaces": [
        {"name": "chat", "path": "/api/v1/reviews/{review_id}/chat", "method": "POST",
         "contract_type": "sse", "llm": True,
         "description": "评审对话（SSE 流式，透出 answer/usage/done）"},
        {"name": "score", "path": "/api/v1/reviews/{review_id}/score", "method": "POST",
         "contract_type": "sse", "llm": True,
         "description": "AI 评分（SSE，含 tool_call knowledge_retrieval；报价维度走 price_calc，run 前需用例规避）"},
        {"name": "login", "path": "/api/v1/auth/login", "method": "POST",
         "llm": False, "description": "专家/管理员鉴权（辅助接口）"},
    ],
    "scenes": [
        {"tag": "tech_scheme", "description": "技术方案评审"},
        {"tag": "price", "description": "报价评审"},
        {"tag": "conflict_interest", "description": "利益冲突检测"},
        {"tag": "collusion", "description": "围串标检测"},
    ],
}


@router.get("", summary="标准契约清单")
async def contracts() -> dict:
    return MANIFEST
