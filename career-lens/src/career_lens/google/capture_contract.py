"""One destination outcome policy, independent of capture transport."""

from ..capture import detect
from ..capture.failures import classify_failure


def destination_failure(status: int | None, challenge) -> dict[str, str] | None:
    if detect.is_blocking(challenge):
        return {
            "kind": classify_failure(challenge=challenge, error=None),
            "detail": f"{challenge.kind}: {challenge.evidence}",
        }
    if status is None or status < 400:
        return None
    if status == 404:
        kind = "not_found"
    elif status in {401, 403, 429, 999}:
        kind = "blocked"
    else:
        kind = "browser_error"
    return {"kind": kind, "detail": f"destination returned HTTP {status}"}
