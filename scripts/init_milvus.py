"""Milvus 初始化脚本 — 创建 bid_documents Collection + IVF_FLAT 索引。

设计依据：solution.md 1.6 节 / 5.3 节 schema 定义。
幂等：Collection 已存在则跳过创建；索引缺失才创建。

P8.2 元数据升级（参考 good-question）：page_no → page_range（VARCHAR，
"1" 单页 / "1-2" 跨页 / "0" 无页码），新增 heading_level / source_type /
token_count。存量 collection 不含新字段（静态 schema 无动态字段），需
--force drop+重建 + 全量重解析入库（重建前确认，见 README）。

用法:
  poetry run python scripts/init_milvus.py            # 幂等创建
  poetry run python scripts/init_milvus.py --force    # drop+重建（清空数据）
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
    """按 solution.md + P8.2 定义 bid_documents schema。

    字段顺序与 app/tasks/document_ingest.py `_insert_milvus` 的 data 列一一对应。
    """
    fields = [
        FieldSchema("chunk_id", DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema("bid_id", DataType.VARCHAR, max_length=64),
        FieldSchema("lot_id", DataType.VARCHAR, max_length=64),
        FieldSchema("content", DataType.VARCHAR, max_length=65535),
        FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=1024),
        FieldSchema("chapter_title", DataType.VARCHAR, max_length=256),
        # page_range: "1" 单页 / "1-2" 跨页 / "0" 无页码（VARCHAR 比 ARRAY 兼容性好）
        FieldSchema("page_range", DataType.VARCHAR, max_length=32),
        FieldSchema("chunk_index", DataType.INT32),
        FieldSchema("source_file", DataType.VARCHAR, max_length=512),
        FieldSchema("heading_level", DataType.INT32),
        FieldSchema("source_type", DataType.VARCHAR, max_length=16),
        FieldSchema("token_count", DataType.INT32),
    ]
    return CollectionSchema(fields, description="标书文档分块向量库")


def init_milvus(force: bool = False) -> None:
    """连接 Milvus 并创建 Collection + 索引（默认幂等；force 时 drop+重建）。"""
    connections.connect(
        alias="default",
        host=settings.milvus_host,
        port=settings.milvus_port,
    )
    collection_name = settings.milvus_collection

    rebuilt = force and utility.has_collection(collection_name)

    if rebuilt:
        collection = Collection(collection_name)
        collection.drop()
        print(f"Collection '{collection_name}' 已 drop（--force 重建）")

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

    if rebuilt:
        # ST1 评分缓存失效：--force 重建后 chunks 变化，旧缓存评分依据过期 → 全量失效。
        # 放重建+load 之后执行：即便 flush 失败（如导入/Redis 异常），collection 也已处于
        # 可用状态，缓存仅靠 TTL 24h 兜底，不影响本脚本主流程。
        try:
            import asyncio
            from app.services.review_service import flush_score_cache

            asyncio.run(flush_score_cache())
            print("评分语义缓存已全量失效")
        except Exception as e:  # noqa: BLE001
            print(f"评分缓存失效失败（TTL 24h 兜底）: {e}")

    collections = utility.list_collections()
    print(f"当前 collections: {collections}")
    connections.disconnect("default")


if __name__ == "__main__":
    init_milvus(force="--force" in sys.argv)
