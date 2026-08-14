"""合成标书正文强化 + Milvus 入库（P6.4 评审工作台三栏数据底座，幂等可重跑）。

背景：合成标书正文早期是 faker 乱词且从未入库 Milvus，AI 检索无依据 → 评审
工作台标书预览/证据溯源/AI 评分全部落空。本脚本一次补齐：
1. 对 file_url 为空（合成）的标书，用改造后的 render_bid_content 重生成
   章节模板化真实正文 → 覆盖 data/synthetic/bid_content/{bid_id}.txt
2. P2.1 规则提取结构化字段 → UPDATE bid_document（报价/工期/团队/资质/质保，
   仅内容字段，标书状态不变，不影响已推进的评审数据）
3. 切块 + BGE-M3 向量化 → Milvus 先删后插（幂等，重跑安全）

依赖：bge-m3 容器（.env BGE_M3_ENDPOINT）在线；sp-mysql 在线。
"""

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
# generators 依赖同目录 common 模块，将 synthetic 目录加入 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parent / "synthetic"))

import generators  # noqa: E402

from faker import Faker  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from app.ai.rag.chunker import SmartDocumentChunker  # noqa: E402
from app.ai.rag.embedder import get_embedder  # noqa: E402
from app.core.database import session_factory  # noqa: E402
from app.models.bid_document import BidDocument  # noqa: E402
from app.models.project import Lot, Project  # noqa: E402
from app.models.supplier import Supplier  # noqa: E402
from app.tasks.document_ingest import _extract_structured_fields, _insert_milvus, _now  # noqa: E402

OUT_DIR = Path("data/synthetic/bid_content")
# 固定种子 → 每次运行正文一致（幂等可复现）
FAKE = Faker("zh_CN")
FAKE.seed_instance(20260812)


async def main() -> None:
    async with session_factory() as s:
        bids = (
            await s.execute(
                select(BidDocument).where(BidDocument.file_url.in_([None, ""]))
            )
        ).scalars().all()
        suppliers = {x.supplier_id: x for x in (await s.scalars(select(Supplier))).all()}
        lots = {x.lot_id: x for x in (await s.scalars(select(Lot))).all()}
        projects = {x.project_id: x for x in (await s.scalars(select(Project))).all()}
    print(f"[1/3] 合成标书待强化: {len(bids)} 份")

    chunker = SmartDocumentChunker()
    embedder = get_embedder()
    ok = 0
    for bid in bids:
        sup = suppliers.get(bid.supplier_id)
        lot = lots.get(bid.lot_id)
        if not sup or not lot:
            print(f"  跳过（缺供应商/标段上下文）: {bid.bid_id}")
            continue
        proj = projects.get(lot.project_id)
        sd = bid.structured_data or {}
        # 场景3 围串标组（SUP-012/SUP-013）用 shared_seed 保持段落一致的高相似特征；
        # 其余标书用 bid_seed 差异化取样（P5.2 回归：避免共享句子池导致全库误报）。
        # 注意：场景3 第三家 SUP-008 是正常标书，走 bid_seed 分支。
        is_scene3 = lot.lot_id == "LOT-007" and bid.supplier_id in ("SUP-012", "SUP-013")
        text = generators.render_bid_content(
            FAKE,
            sup.name,
            proj.name if proj else lot.name,
            lot.name,
            sup.industry,
            bid_amount=int(bid.bid_amount) if bid.bid_amount else None,
            duration=bid.duration,
            team_size=bid.team_size,
            quality_cert=sd.get("quality_cert"),
            warranty_months=sd.get("warranty_months"),
            shared_seed=generators.SCENE3_SHARED_SEED if is_scene3 else None,
            bid_seed=None if is_scene3 else int(bid.bid_id.split("-")[1]),
        )

        # 覆盖 txt（合成数据可再生，非破坏性变更）
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"{bid.bid_id}.txt").write_text(text, encoding="utf-8")

        # 结构化字段：规则提取优先，未命中保留 DB 原值
        fields = _extract_structured_fields(text)
        new_sd = {**sd, **fields["structured_data"]}
        new_amount = fields["bid_amount"] or bid.bid_amount
        new_dur = fields["duration"] or bid.duration
        new_team = fields["team_size"] or bid.team_size
        async with session_factory() as s:
            await s.execute(
                update(BidDocument)
                .where(BidDocument.bid_id == bid.bid_id)
                .values(
                    bid_amount=new_amount,
                    duration=new_dur,
                    team_size=new_team,
                    structured_data=new_sd,
                    updated_at=_now(),
                )
            )
            await s.commit()

        # 切块 + 向量化 + Milvus 入库（幂等）
        chunks = chunker.chunk(text, bid_id=bid.bid_id, lot_id=bid.lot_id, source_file="synthetic")
        if chunks:
            vectors = await embedder.embed([c.content for c in chunks])
            if len(vectors) != len(chunks):
                raise ValueError(f"向量数不匹配: {bid.bid_id} {len(vectors)} vs {len(chunks)}")
            await asyncio.to_thread(_insert_milvus, chunks, vectors, bid.bid_id)
        ok += 1
        print(
            f"  ✓ {bid.bid_id} [{bid.lot_id}] 字数={len(text)} chunks={len(chunks)} "
            f"报价={new_amount} 工期={new_dur} 团队={new_team} sd={new_sd}"
        )
    print(f"[2/3] 强化完成: {ok} 份，txt 已覆盖、DB 字段已更新、Milvus 已入库")
    print("[3/3] 完成")


asyncio.run(main())
