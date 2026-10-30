"""File bytes in MinIO. Postgres keeps the facts of each file: see files.py."""

from __future__ import annotations

import asyncio
import io
from urllib.parse import urlsplit

from minio import Minio

from .config import Settings


class MinioObjects:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str):
        url = urlsplit(endpoint)
        self.bucket = bucket
        self.client = Minio(url.netloc, access_key=access_key, secret_key=secret_key, secure=url.scheme == "https")
        self._ready = False

    def _put(self, key: str, data: bytes, content_type: str) -> None:
        if not self._ready:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            self._ready = True
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)

    def _get(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._put, key, data, content_type)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, key)

    async def remove(self, key: str) -> None:
        await asyncio.to_thread(self.client.remove_object, self.bucket, key)


def open_objects(settings: Settings) -> MinioObjects | None:
    """The file storage. None when the settings do not give one: uploads then say so."""
    if not (settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key):
        return None
    return MinioObjects(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_bucket)
