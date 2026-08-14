"""Milvus 连接与 Collection 单例。

- connections.connect 全局连接（pymilvus 设计为进程内单连接）
- get_collection() 返回 bid_documents collection；不存在时抛异常（P0.4 创建）
- load_collection() 启动预热：collection.load() 加载到内存，加速首次查询
"""

from pymilvus import Collection, connections, utility

from app.core.config import settings

_connected = False


def _ensure_connected() -> None:
    """幂等建立全局连接。"""
    global _connected
    if not _connected:
        connections.connect(
            alias="default",
            host=settings.milvus_host,
            port=settings.milvus_port,
        )
        _connected = True


def get_collection() -> Collection:
    """返回 bid_documents collection。不存在时抛 RuntimeError（需先跑 init_milvus.py）。"""
    _ensure_connected()
    if not utility.has_collection(settings.milvus_collection):
        raise RuntimeError(
            f"Milvus collection '{settings.milvus_collection}' 不存在，"
            f"请先执行 scripts/init_milvus.py"
        )
    return Collection(settings.milvus_collection)


def load_collection() -> None:
    """启动预热：加载 collection 到内存。不存在时静默跳过（软依赖）。"""
    _ensure_connected()
    if utility.has_collection(settings.milvus_collection):
        Collection(settings.milvus_collection).load()


async def check_connection() -> None:
    """启动时连通性校验。失败抛出异常。"""
    _ensure_connected()
    # list_collections 触发一次真实 gRPC 往返
    utility.list_collections()


def disconnect() -> None:
    """应用关闭时断开连接。"""
    global _connected
    if _connected:
        connections.disconnect("default")
        _connected = False
