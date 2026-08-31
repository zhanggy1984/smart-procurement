"""API v1 路由聚合。新 API 模块在 register_v1_routers() 注册。"""

from fastapi import APIRouter

from app.api.v1 import (auth, bids, closeouts, config, conflicts, declarations, experts,
                         import_templates, matching, notifications, projects, reviews,
                         suppliers, users)

# 业务路由统一挂 /api/v1 前缀（内部各 router 不再重复加前缀）
# T15：auth 不再挂 /api/v1，改由 main.py 单独挂 /api（登录路由统一 /api/auth/login）
api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(projects.router)
api_v1_router.include_router(experts.router)
api_v1_router.include_router(suppliers.router)
api_v1_router.include_router(conflicts.router)
api_v1_router.include_router(bids.router)
api_v1_router.include_router(reviews.router)
api_v1_router.include_router(closeouts.router)
api_v1_router.include_router(matching.router)
api_v1_router.include_router(declarations.router)
api_v1_router.include_router(notifications.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(config.router)
api_v1_router.include_router(import_templates.router)
