"""The JSON service and its one-page console."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from ..consumer_contract import SCHEMA_VERSION
from ..store.config import ConfigError
from ..store.db import DatabaseUnavailable
from . import console, entities, generic, google, jobs, operator, operator_auth, search, v1
from .security import require_key
from .versioning import ConsumerVersionMiddleware


def create_app() -> FastAPI:
    app = FastAPI(
        title="career-lens",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ConsumerVersionMiddleware)

    @app.exception_handler(Exception)
    def unexpected(request: Request, exc: Exception) -> JSONResponse:
        body = {"detail": "Internal service error"}
        if request.url.path.startswith("/v1/"):
            body["schema_version"] = SCHEMA_VERSION
        return JSONResponse(body, status_code=500)

    @app.exception_handler(DatabaseUnavailable)
    def unavailable(request: Request,
                    exc: DatabaseUnavailable) -> JSONResponse:
        return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)

    @app.exception_handler(ConfigError)
    def misconfigured(request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": "Service configuration error"}, status_code=500)

    @app.exception_handler(RequestValidationError)
    def invalid_request(request: Request,
                        exc: RequestValidationError) -> JSONResponse:
        # Pydantic includes the rejected value in each error by default. That
        # is useful for ordinary forms but can echo write-only credentials.
        # Keep the standard detail/loc/msg/type contract while never returning
        # submitted values from any API validation boundary.
        def without_inputs(value):
            if isinstance(value, dict):
                return {
                    key: without_inputs(item)
                    for key, item in value.items() if key != "input"
                }
            if isinstance(value, list):
                return [without_inputs(item) for item in value]
            return value

        errors = without_inputs(jsonable_encoder(exc.errors()))
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get(
        "/openapi.json", include_in_schema=False,
    )
    def openapi_document() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get(
        "/docs", include_in_schema=False,
    )
    def swagger_documentation() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title="career_lens - Swagger UI",
        )

    @app.get(
        "/redoc", include_in_schema=False,
    )
    def redoc_documentation() -> HTMLResponse:
        return get_redoc_html(
            openapi_url="/openapi.json", title="career_lens - ReDoc",
        )

    v1_auth = [Depends(require_key)]
    app.include_router(v1.router, dependencies=v1_auth)
    app.include_router(entities.router, dependencies=v1_auth)
    app.include_router(generic.router, dependencies=v1_auth)
    app.include_router(google.router, dependencies=v1_auth)
    app.include_router(search.router, dependencies=v1_auth)
    app.include_router(jobs.router, dependencies=v1_auth)
    app.include_router(console.page_router)
    app.include_router(operator_auth.router)
    app.include_router(console.router)
    app.include_router(operator.router)
    return app


app = create_app()
