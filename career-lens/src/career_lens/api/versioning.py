"""Version application-owned consumer JSON, including validation/HTTP errors."""

import json

from .consumer import SCHEMA_VERSION


class ConsumerVersionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            return await self.app(scope, receive, send)
        start = None
        chunks = []

        async def versioned(message):
            nonlocal start
            if message["type"] == "http.response.start":
                headers = dict(message["headers"])
                if b"application/json" in headers.get(b"content-type", b""):
                    start = message
                    return
            elif message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = json.loads(b"".join(chunks))
                if isinstance(body, dict):
                    body.setdefault("schema_version", SCHEMA_VERSION)
                encoded = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":")
                ).encode()
                start["headers"] = [
                    (k, v)
                    for k, v in start["headers"]
                    if k.lower() != b"content-length"
                ]
                start["headers"].append((b"content-length", str(len(encoded)).encode()))
                await send(start)
                await send({"type": "http.response.body", "body": encoded})
                return
            await send(message)

        await self.app(scope, receive, versioned)
