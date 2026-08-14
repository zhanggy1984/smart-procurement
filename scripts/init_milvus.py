"""Milvus 初始化脚本 — 创建 bid_documents Collection + IVF_FLAT 索引。

设计依据：solution.md 1.6 节 / 5.3 节 schema 定义。
幂等：Collection 已存在则跳过创建；索引缺失才创建。

用法: poetry run python scripts/init_milvus.py
"""

import sys

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

sys.path.insert(0, ".")
from app.core.config import settings  # noqa: E402


def build_schema() -> CollectionSchema:
    """按 solution.md 定义 bid_documents schema。"""
    fields = [
        FieldSchema("chunk_id", DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema("bid_id", DataType.VARCHAR, max_length=64),
        FieldSchema("lot_id", DataType.VARCHAR, max_length=64),
        FieldSchema("content", DataType.VARCHAR, max_length=65535),
        FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=1024),
        FieldSchema("chapter_title", DataType.VARCHAR, max_length=256),
        FieldSchema("page_no", DataType.INT32),
        FieldSchema("chunk_index", DataType.INT32),
        FieldSchema("source_file", DataType.VARCHAR, max_length=512),
    ]
    return CollectionSchema(fields, description="标书文档分块向量库")


def init_milvus() -> None:
    """连接 Milvus 并创建 Collection + 索引（幂等）。"""
    connections.connect(
        alias="default",
        host=settings.milvus_host,
        port=settings.milvus_port,
    )
    collection_name = settings.milvus_collection

    if utility.has_collection(collection_name):
        print(f"Collection '{collection_name}' 已存在，跳过创建")
    else:
        schema = build_schema()
        collection = Collection(collection_name, schema=schema)
        print(f"Collection '{collection_name}' 创建成功")

    collection = Collection(collection_name)
    try:
        # pymilvus 2.4: 索引不存在时 collection.index() 抛 IndexNotExistException，
        # 而非返回 None。用异常判断是否已建，保证幂等。
        collection.index()
        print("索引已存在，跳过创建")
    except Exception as e:  # noqa: BLE001  IndexNotExistException
        print(f"索引不存在，创建新索引: {type(e).__name__}")
        collection.create_index(
            "embedding",
            {
                "metric_type": "IP",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
            },
        )
        print("IVF_FLAT(IP) 索引创建成功")

    collection.load()
    print("Collection 已 load() 预热")

    collections = utility.list_collections()
    print(f"当前 collections: {collections}")
    connections.disconnect("default")


if __name__ == "__main__":
    init_milvus()
