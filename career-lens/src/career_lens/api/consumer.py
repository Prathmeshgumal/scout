"""Shared response projections."""

from copy import deepcopy
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, RootModel, model_serializer

from ..consumer_contract import ENTITY_SCHEMA_VERSION, SCHEMA_VERSION

ACTIVE = {"queued", "assigned", "running"}


def sorted_fields(value: Any) -> Any:
    """Sort object keys without disturbing ranked lists or missing payloads."""
    if isinstance(value, dict):
        return {key: sorted_fields(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [sorted_fields(item) for item in value]
    return value


class QueryResponse(BaseModel):
    @model_serializer(mode="wrap")
    def serialize_sorted(self, handler):
        return sorted_fields(handler(self))


class SectionMeta(BaseModel):
    status: str
    observed_at: str | None = None
    source_url: str | None = None
    reached_end: bool | None = None
    captured_pages: int | None = None
    fallback: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def serialize_relevant(self, handler):
        result = handler(self)
        for key in (
            "source_url", "reached_end", "captured_pages", "fallback", "diagnostics"
        ):
            if result.get(key) is None or result.get(key) == {}:
                result.pop(key, None)
        return result


class MetaNode(RootModel[SectionMeta | dict[str, "MetaNode"]]):
    """A section leaf or a group mirroring the same data subtree."""


class EntityResponse(QueryResponse):
    schema_version: str = ENTITY_SCHEMA_VERSION
    entity: dict[str, Any]
    as_of: str
    observed_at: str
    images: dict[str, Any]
    meta: dict[str, MetaNode]
    data: dict[str, Any]


class SubmissionResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    request_id: str
    status: Literal[
        "queued", "assigned", "running", "satisfied", "succeeded", "failed", "cancelled"
    ]
    target: dict[str, Any]
    requested_sections: list[str] | None = None
    status_url: str
    data_url: str | None = None
    coverage: dict[str, Any] | None = None


class RequestResponse(SubmissionResponse):
    requested_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    next_attempt_at: str | None = None
    failure: dict[str, str] | None = None
    extraction_outcome: str | None = None
    wait_reason: str | None = None


class CancelResponse(RequestResponse):
    # What a consumer's cancel did: `cancelled`, the work stopped; `stopping`,
    # its worker stops it at the next page; `withdrawn`, another caller still
    # wants the work, which goes on without this caller.
    cancel: Literal["cancelled", "stopping", "withdrawn"]


class ErrorResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    detail: Any


ERROR_RESPONSES = {
    code: {"model": ErrorResponse} for code in (400, 404, 409, 422, 429, 500, 503)
}


class SearchResponse(QueryResponse):
    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    request_id: str
    status: str
    query: str
    results: list[dict[str, Any]] | None


class GoogleQueryResponse(QueryResponse):
    schema_version: str = SCHEMA_VERSION
    query: str
    top_n: int
    as_of: str
    observed_at: str
    results: list[dict[str, Any]]


class GoogleResultResponse(QueryResponse):
    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    request: dict[str, Any]
    search: dict[str, Any]
    outcome: str
    failure: dict[str, Any] | None
    serp: dict[str, Any] | None
    results: list[dict[str, Any]]


class PendingResultResponse(QueryResponse):
    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    request_id: str
    status: str
    result: None = None


class PageResponse(QueryResponse):
    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    url: str
    capture: dict[str, Any]


class PageQueryResponse(QueryResponse):
    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    url: str
    as_of: str
    observed_at: str
    result: PageResponse


def section_path(name: str, kind: str | None = None) -> str:
    if name.startswith("activity-"):
        return name.replace("activity-", "activity.", 1)
    if name == "recommendations":
        return "recommendations.received"
    if name == "recommendations-given":
        return "recommendations.given"
    if name.startswith("interests-"):
        return name.replace("interests-", "interests.categories.", 1).replace("-", "_")
    return name.replace("-", "_")


def _flat_entity_response(body: dict, *, cutoff: datetime) -> dict:
    """Move section metadata, never arbitrary domain statuses, out of payloads."""
    result = {
        key: deepcopy(body[key]) for key in ("entity", "observed_at", "images", "data")
    }
    result.update(schema_version=SCHEMA_VERSION, as_of=cutoff.isoformat(), meta={})
    metadata = result["meta"]
    old_meta = body.get("meta", {})
    for name, item in old_meta.items():
        metadata[section_path(name)] = SectionMeta(
            status=item.get("status", "parsed"),
            observed_at=item.get("observed_at"),
            fallback=item.get("fallback"),
        ).model_dump()

    def section(value: dict, path: str):
        existing = metadata.get(path, {})
        status = value.pop("status", existing.get("status", "not_captured"))
        item = SectionMeta(
            status=status,
            observed_at=existing.get("observed_at"),
            fallback=existing.get("fallback"),
        ).model_dump()
        if status in {"not_captured", "not_requested"}:
            item["observed_at"] = None
        for key in ("source_url", "reached_end", "captured_pages"):
            item[key] = value.pop(key, None)
            # Company tab documents store completeness inside their payload.
            if isinstance(value.get("data"), dict) and key in value["data"]:
                nested = value["data"].pop(key)
                if item[key] is None:
                    item[key] = nested
        metadata[path] = item

    data = result["data"]
    if body["entity"]["kind"].startswith("linkedin"):
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            if "status" in value:
                section(value, key)
            if key in {"activity", "recommendations", "interests"}:
                for child, nested in value.items():
                    if isinstance(nested, dict) and "status" in nested:
                        section(nested, f"{key}.{child}")
            if key == "interests":
                from .serialise import INTEREST_CATEGORY_ROLES

                for category, role in INTEREST_CATEGORY_ROLES.items():
                    path = f"interests.categories.{category}"
                    if path not in metadata:
                        observed = old_meta.get(role, {})
                        metadata[path] = SectionMeta(
                            status=observed.get("status", "not_captured"),
                            observed_at=observed.get("observed_at"),
                        ).model_dump()
                for child, nested in value.get("categories", {}).items():
                    if isinstance(nested, dict):
                        section(
                            nested, f"interests.categories.{child.replace('-', '_')}"
                        )
    return EntityResponse.model_validate(result).model_dump()


def entity_response(body: dict, *, cutoff: datetime) -> dict:
    """Project stored observations into identical data/metadata section trees.

    Storage and capture roles are deliberately unchanged. Null payloads mean
    no section payload was retained; metadata still explains why.
    """
    result = _flat_entity_response(body, cutoff=cutoff)
    data, flat = result["data"], result["meta"]
    metadata_type = SectionMeta

    def missing():
        return metadata_type(status="not_captured").model_dump()

    if body["entity"]["kind"].startswith("linkedin"):
        head = flat.pop("profile", None) or flat.pop("company", None) or missing()
        section_roots = {path.split(".")[0] for path in flat}
        profile = {key: data.pop(key) for key in list(data) if key not in section_roots}
        data["profile"] = profile
        flat["profile"] = head
        if "interests" in data:
            interests = data["interests"]
            categories = {
                name.replace("-", "_"): value
                for name, value in interests.pop("categories", {}).items()
            }
            data["interests"] = categories
            aggregate = flat.pop("interests", {})
            # The default interests capture is the top-voices reading, not
            # an independently exposed aggregate section.
            top_voices = flat.get("interests.categories.top_voices")
            if top_voices is not None:
                for key in ("capture_id", "fallback"):
                    if aggregate.get(key) is not None:
                        top_voices[key] = aggregate[key]
            flat = {
                path.replace("interests.categories.", "interests.", 1): value
                for path, value in flat.items()
            }

    nested = {}
    for path, value in flat.items():
        parts = path.split(".")
        target_data, target_meta = data, nested
        for part in parts[:-1]:
            target_data = target_data.setdefault(part, {})
            target_meta = target_meta.setdefault(part, {})
        target_data.setdefault(parts[-1], None)
        target_meta[parts[-1]] = value
    result.update(schema_version=ENTITY_SCHEMA_VERSION, data=data, meta=nested)
    return EntityResponse.model_validate(result).model_dump()


def data_url(kind: str, url: str | None, request_id: str) -> str | None:
    slugs = {
        "linkedin_person": "linkedin-individual",
        "linkedin_company": "linkedin-company",
        "linkedin_job": "linkedin-job",
    }
    if kind in slugs and url:
        return f"/v1/{slugs[kind]}?{urlencode({'url': url})}"
    if kind == "google_search":
        return f"/v1/google-search/{request_id}"
    if kind == "generic":
        return f"/v1/url/{request_id}"
    if kind == "linkedin_job_search":
        return f"/v1/linkedin-job-search/{request_id}"
    return None


def request_response(body: dict, *, sections: list[str] | None = None) -> dict:
    failure = body.get("failure")
    # Free-text worker exceptions can contain paths/identities. Keep the code
    # and a safe summary, while ops retains the complete diagnostic.
    safe_failure = (
        {"kind": failure["kind"], "detail": "The scrape could not complete."}
        if failure
        else None
    )
    return RequestResponse(
        request_id=body["request_id"],
        status=body["status"],
        target=body["target"],
        requested_sections=sections,
        **{
            key: body.get(key)
            for key in ("requested_at", "started_at", "finished_at", "next_attempt_at")
        },
        failure=safe_failure,
        extraction_outcome=(body.get("extraction") or {}).get("outcome"),
        coverage={
            "sections": {
                section_path(item["section"], body["kind"]): {
                    "status": item.get("status", item.get("outcome"))
                }
                for item in body.get("sections") or []
            }
        },
        wait_reason=(
            body.get("wait_reason")
            or "Waiting for eligible capacity or retry time."
            if body["status"] == "queued"
            else None
        ),
        status_url=f"/v1/requests/{body['request_id']}",
        data_url=data_url(body["kind"], body.get("url"), body["request_id"]),
    ).model_dump()


def capture_result(value: dict | None) -> dict | None:
    if value is None:
        return None
    result = {
        key: deepcopy(item)
        for key, item in value.items()
        if key
        not in {"capture_id", "request_id", "browser_profile", "extractor_version"}
    }
    if result.get("error"):
        result["error"] = safe_failure(result["error"])
    return result


def safe_failure(value: dict) -> dict:
    return {"kind": value["kind"], "detail": "The scrape could not complete."}


def detailed_result(document: dict) -> dict:
    """Known Google/generic envelope locations; never scrub arbitrary page text."""
    result = deepcopy(document)
    result["schema_version"] = SCHEMA_VERSION
    for key in ("serp", "capture"):
        if isinstance(result.get(key), dict):
            result[key] = capture_result(result[key])
    if isinstance(result.get("result"), dict) and "capture" in result["result"]:
        result["result"]["capture"] = capture_result(result["result"]["capture"])
    for item in result.get("results") or []:
        if "capture" in item:
            item["capture"] = capture_result(item["capture"])
    result.pop("extractor_version", None)
    if result.get("failure"):
        result["failure"] = safe_failure(result["failure"])
    if isinstance(result.get("search"), dict):
        for key in ("browser_locale", "browser_timezone"):
            result["search"].pop(key, None)
    return result
