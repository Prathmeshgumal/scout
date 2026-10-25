"""Live Google discovery snapshot, scoped to its attempt.

This is mutable progress, not captured evidence or a freshness candidate.
Final answers still use the verified canonical search document.
"""

import json

from sqlalchemy import text

from ..store.db import transaction


def save_progress(database_url, attempt_id, document):
    # Keep only the live index; page bodies remain in their immutable captures.
    document = {
        "search": document["search"],
        "results": [{**item, "capture": (
            {key: item["capture"].get(key) for key in ("capture_id", "status", "error")}
            if item.get("capture") else None)} for item in document["results"]],
    }
    with transaction(database_url) as connection:
        connection.execute(text("""
            INSERT INTO google.search_progress (attempt_id, document)
            VALUES (:attempt, CAST(:document AS jsonb))
            ON CONFLICT (attempt_id) DO UPDATE SET document=EXCLUDED.document
        """), {"attempt": attempt_id,
               "document": json.dumps(document)})


def read_progress(connection, request_id):
    native = connection.execute(text("""
        SELECT p.document FROM google.search_progress p
        WHERE EXISTS (SELECT 1 FROM scrape_request r WHERE r.id=:request
                      AND r.status NOT IN ('queued', 'assigned'))
          AND p.attempt_id=(SELECT id FROM scrape_attempt WHERE request_id=:request
                           ORDER BY attempt_no DESC LIMIT 1)
    """), {"request": request_id}).scalar_one_or_none()
    return native


