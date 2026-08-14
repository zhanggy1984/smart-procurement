"""MinIO 客户端封装（P1.5）。

单例客户端 + bucket 确保 + 上传 + 预签名下载 URL。minio-py 为同步阻塞 IO，
业务侧统一用 `asyncio.to_thread` 卸载到线程池，避免阻塞事件循环。
object_name 约定：`bids/{lot_id}/{bid_id}/{filename}`（file_url 列存该路径）。
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from io import BytesIO

from minio import Minio

from app.core.config import settings


@lru_cache
def get_minio_client() -> Minio:
    """MinIO 客户端单例（lru_cache 复用，连接复用）。

    endpoint 传 host:port（minio-py 7.x 不接受带 scheme 的 endpoint，scheme
    由 secure 标志决定——实测 `Minio("http://...")` 抛 "path in endpoint"）。
    """
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,  # 本地/内网 HTTP；生产走 HTTPS 时改 secure=True
    )


def ensure_bucket(client: Minio) -> None:
    """确保 bucket 存在（幂等，首次创建）。"""
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)


def upload_bytes(client: Minio, object_name: str, data: bytes, content_type: str) -> None:
    """上传字节到 bucket。object_name 含完整路径。"""
    ensure_bucket(client)
    client.put_object(
        settings.minio_bucket,
        object_name,
        BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def download_object(client: Minio, object_name: str) -> bytes:
    """读取对象全部字节（P2.1 解析流水线取原文件用）。不存在抛 S3Error。"""
    resp = client.get_object(settings.minio_bucket, object_name)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def presign_url(client: Minio, object_name: str) -> str:
    """生成预签名 GET URL（有效期 settings.minio_presign_expiry_seconds）。"""
    return client.presigned_get_object(
        settings.minio_bucket,
        object_name,
        expires=timedelta(seconds=settings.minio_presign_expiry_seconds),
    )


def remove_object(client: Minio, object_name: str) -> None:
    """删除对象（幂等，对象不存在不报错；清理用，失败不阻断）。"""
    try:
        client.remove_object(settings.minio_bucket, object_name)
    except Exception:  # noqa: BLE001
        pass


def remove_prefix(client: Minio, prefix: str) -> None:
    """按前缀删除对象（验收/清理用，幂等）。bucket 不存在直接返回。"""
    if not client.bucket_exists(settings.minio_bucket):
        return
    for obj in client.list_objects(settings.minio_bucket, prefix=prefix, recursive=True):
        remove_object(client, obj.object_name)
