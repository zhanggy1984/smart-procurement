"""Neo4j 异步 driver 单例。

连接池参数按 solution.md 风险清单 R4：max_connection_lifetime=3600, max_connection_pool_size=50。
"""

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.core.config import settings

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    """返回全局单例 driver，首次调用时创建。"""
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_lifetime=3600,
            max_connection_pool_size=50,
            connection_timeout=10,
        )
    return _driver


async def check_connection() -> None:
    """启动时连通性校验：执行一次 RETURN 1。失败抛出异常。"""
    driver = get_driver()
    async with driver.session() as session:
        await session.run("RETURN 1")


async def close_driver() -> None:
    """应用关闭时释放 driver。"""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None
