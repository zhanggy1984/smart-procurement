"""4.3 标准契约清单端点（平台定标准，agent 适配）。

统一 `GET /api/contracts`（公开无鉴权），声明本 agent 的 LLM 评测接口、场景清单与
**驱动契约（contract 段，manifest v2）**。平台脚手架读此端点做接口自动发现（决策
#55/#56）与 adapter 生成（{{input.*}}/{{auth.*}}/{{prepare.*}} 占位符由平台渲染）。
llm=false 为辅助接口（登录等），只登记不进 agent_interface。contract 段是平台驱动本
agent 的权威声明，改动需与平台 seed 快照保持同构（discover 会对比漂移）。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/contracts", tags=["contracts"])

MANIFEST = {
    "agent": "smart-procurement",
    "contract_version": "2.0",
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
    "contract": {
        "type": "sse", "timeout": 120,
        "prepare": [
            {"name": "login", "method": "POST", "path": "/api/v1/auth/login",
             "body": {"username": "{{auth.username}}", "password": "{{auth.password}}"},
             "extract": {"token": "access_token"}},
            # 评审需 FROZEN 标书 + 专家账号（display_name==expert.name 反查 expert_id）
            # review 每次新建会堆积（prepare 无幂等），测试环境定期清库（决策 58）
            {"name": "review", "method": "POST", "path": "/api/v1/reviews",
             "headers": {"Authorization": "Bearer {{prepare.login.token}}",
                         "Content-Type": "application/json"},
             "body": {"bid_id": "{{input.bid_id}}", "dimension_id": "{{input.dimension_id}}"},
             "extract": {"review_id": "review_id"}},
        ],
        "request": {
            "path": "/api/v1/reviews/{{prepare.review.review_id}}/chat", "method": "POST",
            "headers": {"Authorization": "Bearer {{prepare.login.token}}",
                        "Content-Type": "application/json"},
            "body": {"question": "{{input.question}}"},
        },
        # sse 无需 field_map：本 agent 的 answer{delta} / usage{prompt,completion,total_tokens} /
        # done{content} 与平台统一口径一致
    },
}


@router.get("", summary="标准契约清单")
async def contracts() -> dict:
    return MANIFEST
