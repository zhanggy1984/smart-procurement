"""AI 智能评标系统 — FastAPI 应用入口。

P0.3 阶段：
- lifespan 启动时校验四库连通性 + DeepSeek API key，任一硬依赖失败则 exit(1)
- /health/live  存活探针（不依赖中间件）
- /health/ready 就绪探针（返回四库连通状态）
"""

import asyncio
import sys
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import contracts
from app.api.v1 import api_v1_router
from app.core import database, logging, milvus, neo4j
from app.core.config import settings
from app.core.errors import is_dependency_error
from app.core.middleware import RequestIDMiddleware
from app.services import config_service

logger = structlog.get_logger(__name__)

# 硬依赖（失败即 exit(1)）
HARD_DEPENDENCIES = ("mysql", "neo4j", "redis")
# 软依赖（连通性纳入 ready 上报，但不阻塞启动）
SOFT_DEPENDENCIES = ("milvus", "deepseek", "bge_m3")


def _jwt_secret_secure(secret: str) -> bool:
    """JWT 密钥强度校验：非空、非公开默认值、≥32 字符。

    防漏配 JWT_SECRET_KEY 时静默用 config.py 默认值（change-me-in-production，
    公开已知）签发 token 伪造任意身份。不满足即拒绝启动（fail loud）。
    """
    return bool(secret) and secret != "change-me-in-production" and len(secret) >= 32


async def _check_dependency(name: str) -> bool:
    """单项依赖连通性检查，返回是否可用。"""
    try:
        if name == "mysql":
            await database.check_connection()
        elif name == "neo4j":
            await neo4j.check_connection()
        elif name == "milvus":
            await milvus.check_connection()
        elif name == "redis":
            from redis.asyncio import Redis

            r = Redis.from_url(settings.redis_url, socket_connect_timeout=5)
            await r.ping()
            await r.aclose()
        elif name == "deepseek":
            # 仅校验 key 是否配置（真实调用在 P3.1 深测）；停用开关打开时视为可用
            if not settings.deepseek_enabled:
                return True
            if not settings.deepseek_api_key or settings.deepseek_api_key.startswith("sk-xxx"):
                return False
        elif name == "bge_m3":
            if not settings.bge_m3_endpoint:
                return True  # dev 模式直连，视为可用
            import httpx

            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.bge_m3_endpoint.rstrip('/')}/health")
                return resp.status_code == 200
        return True
    except Exception:  # noqa: BLE001
        return False


async def _ready_status() -> dict[str, str]:
    """返回各依赖状态。"""
    status: dict[str, str] = {}
    for name in HARD_DEPENDENCIES + SOFT_DEPENDENCIES:
        status[name] = "ok" if await _check_dependency(name) else "fail"
    return status


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动校验 + 关闭清理。配置缺陷/硬依赖失败时 exit(1)。"""
    logging.setup_logging(settings.log_level)
    # JWT 密钥安全校验（fail loud）：默认值/弱密钥拒绝启动，防公开密钥伪造身份
    if not _jwt_secret_secure(settings.jwt_secret_key):
        print("[startup] JWT_SECRET_KEY 缺失/为默认值/过短（须 ≥32 随机字符），拒绝启动",
              file=sys.stderr)
        sys.exit(1)
    checks = await asyncio.gather(*[_check_dependency(n) for n in HARD_DEPENDENCIES])
    failed = [n for n, ok in zip(HARD_DEPENDENCIES, checks) if not ok]
    if failed:
        print(f"[startup] 硬依赖不可用: {failed}，退出进程", file=sys.stderr)
        sys.exit(1)

    print(f"[startup] 硬依赖就绪: {[n for n, ok in zip(HARD_DEPENDENCIES, checks) if ok]}")

    # 软依赖启动预热（Milvus collection load，失败仅告警）
    try:
        milvus.load_collection()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] Milvus 预热跳过: {e}")

    # 系统配置缓存预热（P6.2）：加载 system_config 到内存，失败仅告警（默认值兜底）
    try:
        await config_service.load_all()
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 系统配置加载跳过: {e}")

    yield

    # 关闭清理
    await database.dispose_engine()
    await neo4j.close_driver()
    milvus.disconnect()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI 智能评标系统：专家智能匹配、回避检测、标书 RAG 检索、AI 辅助打分、围串标检测",
    lifespan=lifespan,
)

# X-Request-ID 全链路追踪中间件（P3.6）：先于路由注册
app.add_middleware(RequestIDMiddleware)

# 业务路由（/api/v1 前缀）
app.include_router(api_v1_router)
# 标准契约清单端点（统一 GET /api/contracts，平台脚手架发现用）
app.include_router(contracts.router, prefix="/api")


# ==================== P8 异常兜底：全局 exception handler ====================


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 handler：外部依赖故障 → 503，其他未捕获异常 → 500（JSON 响应体）。

    失败偏置：中间件挂（Redis/Neo4j/MinIO/MySQL/Milvus）返回"依赖暂不可用"
    让前端走降级 UI；真正服务器 bug 返回 500 并落 error 日志。

    边界：StreamingResponse 响应头发出后抛的异常不被捕获（只能断流），
    由各 SSE 端点 gen() 内 try/except 兜底（见 reviews.py）。
    """
    if is_dependency_error(exc):
        logger.warning("http.dependency_error", path=request.url.path, error=str(exc))
        return JSONResponse(status_code=503, content={"detail": "核心依赖暂不可用，请稍后重试"})
    logger.error("http.unhandled", path=request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请稍后重试"})


@app.get("/health/live", tags=["health"], summary="存活探针")
async def health_live() -> dict:
    """进程存活检查，不依赖任何外部中间件。"""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"], summary="就绪探针")
async def health_ready() -> JSONResponse:
    """返回四库连通状态。任一硬依赖失败返回 503。"""
    status = await _ready_status()
    hard_ok = all(status[n] == "ok" for n in HARD_DEPENDENCIES)
    body = {"status": "ok" if hard_ok else "degraded", **status}
    return JSONResponse(content=body, status_code=200 if hard_ok else 503)
