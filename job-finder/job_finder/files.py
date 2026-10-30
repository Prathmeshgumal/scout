"""Files that the user uploads: one current resume for all goals, and attachments of a goal.

MinIO keeps the bytes. Postgres keeps the facts of a file and its text. A file type must match its bytes. Text from a
file stays out of events, reports and API replies. The resume text goes only to the subagents that need it."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import uuid
from pathlib import PurePath
from typing import Any

from docx import Document
from psycopg_pool import AsyncConnectionPool
from pypdf import PdfReader

log = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the largest image that Claude takes
MAX_TEXT = 200_000  # characters of a file that the database keeps
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXT = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".log", ".py", ".js",
        ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp", ".rb", ".sh", ".sql", ".css", ".toml"}
PDF, DOCX = "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
META = ("id::text AS id, goal_id::text AS goal_id, role, name, content_type, kind, size_bytes, created_at, "
        "(text IS NOT NULL OR kind = 'image') AS readable")


class FileRefused(Exception):
    """An upload that the app does not take. The text is for the user."""


class FileStorageError(Exception):
    """The file storage did not answer. The text is for the user."""


def clean_name(name: str | None) -> str:
    """A file name with no folders and no control characters."""
    base = PurePath((name or "").replace("\\", "/")).name
    return re.sub(r"[\x00-\x1f\x7f]", "", base).strip()[:200] or "file"


def _is_image(data: bytes, content_type: str) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def classify(name: str, data: bytes) -> tuple[str, str]:
    """The kind and the content type of a file. A name that does not match the bytes is refused."""
    suffix = PurePath(name).suffix.lower()
    if suffix in IMAGES:
        if not _is_image(data, IMAGES[suffix]):
            raise FileRefused(f"{name} is not a real {suffix[1:].upper()} image.")
        return "image", IMAGES[suffix]
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise FileRefused(f"{name} is not a real PDF file.")
        return "pdf", PDF
    if suffix == ".docx":
        if not data.startswith(b"PK\x03\x04"):
            raise FileRefused(f"{name} is not a real Word file.")
        return "docx", DOCX
    if suffix in TEXT:
        if b"\x00" in data:
            raise FileRefused(f"{name} is not a text file.")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise FileRefused(f"{name} is not UTF-8 text.") from None
        return "text", "text/plain; charset=utf-8"
    return "other", "application/octet-stream"


def extract_text(kind: str, data: bytes) -> str | None:
    """The text of a file. None when it has no text that a program can read, for example a scanned PDF."""
    try:
        if kind == "text":
            text = data.decode("utf-8")
        elif kind == "pdf":
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                return None
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        elif kind == "docx":
            document = Document(io.BytesIO(data))
            cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
            text = "\n".join([*(p.text for p in document.paragraphs), *cells])
        else:
            return None
    except Exception as exc:  # a broken file: it stays downloadable, and the agents cannot read it
        log.warning("could not read a %s file: %s", kind, type(exc).__name__)
        return None
    text = text.replace("\x00", "").strip()
    return text[:MAX_TEXT] or None


class FileStore:
    def __init__(self, pool: AsyncConnectionPool, objects: Any | None):
        self.pool, self.objects = pool, objects

    async def _one(self, query: str, params: tuple = ()) -> dict | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute(query, params)).fetchone()

    async def _all(self, query: str, params: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            return await (await conn.execute(query, params)).fetchall()

    async def save(self, name: str | None, data: bytes, role: str = "attachment") -> dict:
        """Keep a file. A new resume replaces the old one."""
        if self.objects is None:
            raise FileRefused("File storage is not set up.")
        name = clean_name(name)
        if not data:
            raise FileRefused(f"{name} is empty.")
        if len(data) > MAX_BYTES:
            raise FileRefused(f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB.")
        kind, content_type = classify(name, data)
        text = await asyncio.to_thread(extract_text, kind, data)
        key = f"{role}/{uuid.uuid4()}"
        try:
            await self.objects.put(key, data, content_type)
        except Exception as exc:
            log.warning("could not store a file: %s", type(exc).__name__)
            raise FileStorageError("The file storage did not answer. Try again.") from exc
        old = None
        try:
            async with self.pool.connection() as conn, conn.transaction():
                if role == "resume":
                    old = await (await conn.execute(
                        "DELETE FROM files WHERE role = 'resume' RETURNING object_key")).fetchone()
                row = await (await conn.execute(
                    f"INSERT INTO files (role, name, content_type, kind, size_bytes, sha256, object_key, text) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {META}",
                    (role, name, content_type, kind, len(data), hashlib.sha256(data).hexdigest(), key, text))).fetchone()
        except BaseException:
            await self.remove_objects([key])
            raise
        if old:
            await self.remove_objects([old["object_key"]])
        return row

    async def link(self, file_ids: list[str], goal_id: str) -> list[dict]:
        """Give new attachments to a goal. An attachment of another goal stays with that goal."""
        if not file_ids:
            return []
        return await self._all(f"UPDATE files SET goal_id = %s WHERE id = ANY(%s::uuid[]) AND role = 'attachment' "
                               f"AND goal_id IS NULL RETURNING {META}", (goal_id, file_ids))

    async def resume(self) -> dict | None:
        return await self._one(f"SELECT {META} FROM files WHERE role = 'resume'")

    async def resume_text(self) -> str | None:
        row = await self._one("SELECT text FROM files WHERE role = 'resume'")
        return row["text"] if row else None

    async def of_goal(self, goal_id: str) -> list[dict]:
        return await self._all(f"SELECT {META} FROM files WHERE goal_id = %s ORDER BY created_at", (goal_id,))

    async def get(self, file_id: str) -> dict | None:
        return await self._one(f"SELECT {META} FROM files WHERE id = %s", (file_id,))

    async def text(self, file_id: str) -> str | None:
        row = await self._one("SELECT text FROM files WHERE id = %s", (file_id,))
        return row["text"] if row else None

    async def data(self, file_id: str) -> bytes:
        row = await self._one("SELECT object_key FROM files WHERE id = %s", (file_id,))
        try:
            return await self.objects.get(row["object_key"])
        except Exception as exc:
            log.warning("could not read a file: %s", type(exc).__name__)
            raise FileStorageError("The file storage did not answer. Try again.") from exc

    async def delete(self, file_id: str) -> bool:
        row = await self._one("DELETE FROM files WHERE id = %s RETURNING object_key", (file_id,))
        if row:
            await self.remove_objects([row["object_key"]])
        return row is not None

    async def keys_of(self, goal_ids: list[str]) -> list[str]:
        rows = await self._all("SELECT object_key FROM files WHERE goal_id = ANY(%s::uuid[])", (goal_ids,))
        return [row["object_key"] for row in rows]

    async def remove_objects(self, keys: list[str]) -> None:
        """Remove file bytes. A failure is logged: the facts of the file are gone already."""
        for key in keys:
            try:
                await self.objects.remove(key)
            except Exception as exc:
                log.warning("could not remove %s: %s", key, type(exc).__name__)
