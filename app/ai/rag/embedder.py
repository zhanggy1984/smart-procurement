"""BGE-M3 文本向量化（P2.1 Step 4 / P2.2 embedder）。

双后端，由 `settings.bge_m3_endpoint` 切换：
- prod（非空）：HTTP 调独立 bge-m3 容器（docker/bge-m3），主应用零 torch 依赖。
  接口契约见 docker/bge-m3/main.py：POST /embed {"texts":[...], "normalize":true}
  → {"vectors":[[...]], "dim":1024}。
- dev（空）：sentence-transformers 直连，模型延迟加载（首次调用才 import/load），
  CPU/GPU 密集推理通过 `asyncio.to_thread()` 卸载出事件循环（task.md P2.1 Step 4 要求）。

异常语义：embedding 失败抛原始异常，由调用方（解析流水线）重试/降级。
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger(__name__)

# 单次 HTTP 请求的文本上限（bge-m3 容器按批推理，避免单请求超大）
BATCH_SIZE = 32


class BGE3Embedder:
    """BGE-M3 向量化器。模块级单例（get_embedder()），进程内复用模型/连接。"""

    def __init__(self) -> None:
        # dev 直连模式的模型句柄（延迟加载）
        self._model = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """文本 → 归一化向量列表（1024 维）。空输入返回空列表。"""
        if not texts:
            return []
        if settings.bge_m3_endpoint:
            return await self._embed_via_http(texts)
        return await self._embed_via_local(texts)

    # ==================== prod：HTTP 容器 ====================

    async def _embed_via_http(self, texts: list[str]) -> list[list[float]]:
        """调 bge-m3 容器，分批避免单请求过大。失败抛 httpx/连接异常。"""
        endpoint = settings.bge_m3_endpoint.rstrip("/")
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=180.0) as client:
            for i in range(0, len(texts), BATCH_SIZE):
                batch = texts[i : i + BATCH_SIZE]
                r = await client.post(f"{endpoint}/embed", json={"texts": batch, "normalize": True})
                r.raise_for_status()
                vectors.extend(r.json()["vectors"])
        logger.debug("embed.http_ok", count=len(texts), dim=len(vectors[0]) if vectors else 0)
        return vectors

    # ==================== dev：sentence-transformers 直连 ====================

    async def _embed_via_local(self, texts: list[str]) -> list[list[float]]:
        """直连 BGE-M3 模型。模型加载 + 推理都卸载到线程池（阻塞调用）。"""
        if self._model is None:
            # 延迟 import：sentence-transformers 仅在 bge-m3 extra 中声明，
            # 未装时给出可操作的报错（提示 poetry install -E bge-m3）。
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                raise RuntimeError(
                    "BGE-M3 dev 模式需要 sentence-transformers，请执行 `poetry install -E bge-m3`"
                ) from e
            logger.info("embed.local_model_load", model=settings.bge_m3_model)
            self._model = await asyncio.to_thread(SentenceTransformer, settings.bge_m3_model)

        embeddings = await asyncio.to_thread(
            self._model.encode,
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embeddings.tolist()


_embedder: BGE3Embedder | None = None


def get_embedder() -> BGE3Embedder:
    """模块级单例：进程内复用模型/HTTP 连接。"""
    global _embedder
    if _embedder is None:
        _embedder = BGE3Embedder()
    return _embedder
