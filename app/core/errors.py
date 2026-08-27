"""统一异常兜底判定（P8 主题④）。

is_dependency_error()：判断未处理异常是否源于外部中间件（Redis/Neo4j/MinIO/
MySQL/Milvus），供全局 exception handler 决定返回 503（依赖暂不可用）还是
500（服务器内部错误）。

- 懒导入：按需 __import__ 各 SDK 模块，未安装时 ImportError 静默跳过，
  不要求启动时中间件 SDK 全装。
- 递归溯源 __cause__ / __context__：业务层常把依赖异常包装成业务异常，
  只判最外层会漏判。
- 业务异常（HTTPException / RequestValidationError / 自定义 ServiceError）
  走既有 handler，不经此处。

边界：StreamingResponse 响应头发出后抛的异常不被 FastAPI handler 捕获
（只能断流），由各 SSE 端点 gen() 内 try/except 兜底（见 reviews.py）。
"""

from __future__ import annotations

# 依赖 SDK 异常基类（模块名 -> 类名）。只取"连接/服务端故障"层，
# 不含编程错误（如 sqlalchemy.exc.StatementError 是 SQL 拼写问题，归 500）。
_DEPENDENCY_ERRORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("redis.exceptions", ("RedisError",)),
    ("neo4j.exceptions", ("Neo4jError", "ServiceUnavailable", "TransientError")),
    ("minio.error", ("MinioError", "S3Error", "InvalidResponseError")),
    ("sqlalchemy.exc", ("OperationalError", "InterfaceError")),
    ("grpc", ("RpcError",)),
)


def is_dependency_error(exc: BaseException) -> bool:
    """判断异常是否源于外部依赖（递归溯源 __cause__/__context__）。"""
    seen: set[int] = set()

    def _walk(e: BaseException) -> bool:
        if id(e) in seen:  # 防循环链
            return False
        seen.add(id(e))
        for mod_name, cls_names in _DEPENDENCY_ERRORS:
            try:
                mod = __import__(mod_name, fromlist=cls_names)
            except ImportError:
                continue
            for cls_name in cls_names:
                cls = getattr(mod, cls_name, None)
                if cls is not None and isinstance(e, cls):
                    return True
        if e.__cause__ and _walk(e.__cause__):
            return True
        if e.__context__ and _walk(e.__context__):
            return True
        return False

    return _walk(exc)
