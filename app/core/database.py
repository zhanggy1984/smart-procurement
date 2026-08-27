"""SQLAlchemy async engine / session 单例。

连接池参数按 solution.md 风险清单 R4 配置：
pool_size=20, max_overflow=10, pool_pre_ping=True。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.debug,
    # P8 异常兜底：连接/读取超时（asyncmy 原生 connect_timeout/read_timeout，
    # 覆盖默认无限等待）+ 连接池等待超时（池耗尽不无限阻塞）
    connect_args={
        "connect_timeout": settings.mysql_connect_timeout,
        "read_timeout": settings.mysql_read_timeout,
    },
    pool_timeout=10,
)

# session 工厂：业务代码通过 `async with session_factory() as session` 使用
session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：每请求一个 session，请求结束自动关闭。"""
    async with session_factory() as session:
        yield session


async def check_connection() -> None:
    """启动时连通性校验：执行一次 SELECT 1。失败抛出异常。"""
    async with engine.connect() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))


async def dispose_engine() -> None:
    """应用关闭时释放连接池。"""
    await engine.dispose()
