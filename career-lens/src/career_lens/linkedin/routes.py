"""Organization route semantics shared by native and browser workers."""

from urllib.parse import urlsplit


def is_company_home(here: str, root: str) -> bool:
    """Recognize the root and explicit Home routes, never other descendants."""
    try:
        location = urlsplit(here)
        target = urlsplit(root).path.rstrip("/")
        if location.netloc:
            host = location.hostname or ""
            if host != "linkedin.com" and not host.endswith(".linkedin.com"):
                return False
        segments = target.strip("/").split("/")
        if len(segments) != 2 or segments[0] not in {"company", "school", "showcase"}:
            return False
        return location.path.rstrip("/") in {target, f"{target}/home"}
    except ValueError:
        return False
