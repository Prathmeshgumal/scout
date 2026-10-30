"""The real MinIO of docker-compose.yml. The test skips when MinIO does not run."""

import socket
import uuid

import pytest

from job_finder.objects import MinioObjects, open_objects


def minio_runs() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", 9004)) == 0


@pytest.mark.skipif(not minio_runs(), reason="MinIO does not run")
async def test_minio_keeps_and_removes_a_file():
    objects = MinioObjects("http://127.0.0.1:9004", "jobfinder", "jobfinder-dev-secret", f"test-{uuid.uuid4()}")
    await objects.put("a/b", b"hello", "text/plain")
    assert await objects.get("a/b") == b"hello"
    await objects.remove("a/b")
    with pytest.raises(Exception, match="NoSuchKey|does not exist"):
        await objects.get("a/b")
    objects.client.remove_bucket(objects.bucket)


def test_no_settings_give_no_storage(app_settings):
    assert open_objects(app_settings) is None
