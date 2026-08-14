"""BGE-M3 Embedding 独立服务（骨架先行）。

- GET  /health   : 存活探针，不加载模型，容器立即可 healthy
- POST /embed    : 文本向量化，首次调用懒加载模型（~2GB，加载较慢）
  body: {"texts": ["...", "..."], "normalize": true}
  resp: {"vectors": [[...]], "dim": 1024}

模型懒加载：启动时不加载，第一次 /embed 请求时自动下载+加载。
P2 阶段文档解析流水线第一次调用时自然触发。
"""

import asyncio
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="BGE-M3 Embedding Service", version="0.1.0")

_model = None
_model_lock = threading.Lock()


class EmbedRequest(BaseModel):
    texts: list[str]
    normalize: bool = True  # BGE-M3 归一化后 IP 度量等效 Cosine


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    dim: int


def _load_model() -> None:
    """懒加载模型，线程锁保证只加载一次。"""
    global _model
    if _model is not None:
        return
    with _model_lock:
        if _model is not None:
            return
        from sentence_transformers import SentenceTransformer

        # 首次调用会从 HuggingFace 下载 BAAI/bge-m3（约 2GB），缓存到 /root/.cache
        _model = SentenceTransformer("BAAI/bge-m3")


@app.get("/health", tags=["health"])
async def health() -> dict:
    """存活探针：不加载模型，容器立即可通过健康检查。"""
    return {"status": "ok", "model_loaded": _model is not None}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    """文本向量化。首次调用触发模型加载（可能耗时数分钟）。"""
    if not req.texts:
        raise HTTPException(status_code=422, detail="texts 不能为空")
    try:
        # 模型加载和推理是 CPU/GPU 密集操作，放到线程池避免阻塞事件循环
        vectors = await asyncio.to_thread(_encode, req.texts, req.normalize)
        return EmbedResponse(vectors=vectors, dim=len(vectors[0]))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"embedding 失败: {e}")


def _encode(texts: list[str], normalize: bool) -> list[list[float]]:
    """同步推理实现。"""
    _load_model()
    embeddings = _model.encode(
        texts,
        normalize_embeddings=normalize,
        convert_to_numpy=True,
    )
    return embeddings.tolist()
