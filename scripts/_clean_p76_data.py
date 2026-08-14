"""P7.6 压测前数据清理（一次性）。

清理历史验收残留标书（Milvus chunks + MySQL bid_document + MinIO 对象），
保留 52 份合成标书（file_url IS NULL/空）+ BID-BENCH-01..05 基准标书。
目的：压测环境干净，避免历史数据干扰检索性能/深度检测质量。

用法: poetry run python scripts/_clean_p76_data.py
"""

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.milvus import get_collection  # noqa: E402
from app.core.minio_client import get_minio_client, remove_prefix  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402


async def main() -> None:
    # ---- 1) 保留集合 ----
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        keep = set(
            (await conn.execute(text(
                "SELECT DISTINCT bid_id FROM bid_document "
                "WHERE file_url IS NULL OR file_url = ''"
            ))).all())
    keep = {r[0] for r in keep} | {"BID-BENCH-01", "BID-BENCH-02", "BID-BENCH-03",
                                   "BID-BENCH-04", "BID-BENCH-05"}
    print(f"[保留] {len(keep)} 个 bid_id（52 合成 + 5 基准）")

    # ---- 2) MySQL 历史残留标书行 ----
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT bid_id, lot_id, supplier_id, file_url FROM bid_document WHERE file_url LIKE 'bids/%'"
        ))).all()
    print(f"[MySQL] 待删 file_url LIKE 'bids/%' 行 = {len(rows)}")
    for r in rows[:10]:
        print(f"    {r.bid_id} {r.lot_id} {r.supplier_id} {r.file_url}")
    if rows:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM bid_document WHERE file_url LIKE 'bids/%'"))
        print(f"    已删除 {len(rows)} 行")

    # ---- 3) Milvus 残留 chunks ----
    def _scan_milvus():
        collection = get_collection()
        try:
            res = collection.query(expr='bid_id != ""', output_fields=["bid_id"], limit=16384)
        except Exception as e:  # noqa: BLE001  集合空/异常
            print(f"    Milvus query 异常: {e}")
            return []
        bid_ids = sorted({r["bid_id"] for r in res})
        return bid_ids

    milvus_bids = await asyncio.to_thread(_scan_milvus)
    print(f"[Milvus] 现存 bid_id = {len(milvus_bids)}")
    to_del = [b for b in milvus_bids if b not in keep]
    print(f"[Milvus] 待删 bid_id = {len(to_del)}: {to_del[:15]}{'...' if len(to_del) > 15 else ''}")

    def _del_milvus(to_del_bids: list[str]) -> None:
        collection = get_collection()
        # 分块删除，避免 expr 过长
        for i in range(0, len(to_del_bids), 50):
            batch = to_del_bids[i:i + 50]
            ids = ",".join(f'"{b}"' for b in batch)
            try:
                collection.delete(f"bid_id in [{ids}]")
            except Exception as e:  # noqa: BLE001
                print(f"    Milvus 删除批次失败: {e}")
        collection.flush()

    if to_del:
        await asyncio.to_thread(_del_milvus, to_del)
        print(f"    已删除 {len(to_del)} 个 bid_id 的 chunks")

    # ---- 4) MinIO 历史 PDF ----
    client = get_minio_client()
    remove_prefix(client, "bids/")
    print("[MinIO] bids/ 前缀已清空")

    await engine.dispose()


asyncio.run(main())
