"""Prefixed, time-ordered ids for the rows that appear outside this system.

Rows addressed by callers or operators carry text keys: requests, captures,
extractions, LinkedIn accounts, account assignments,
supervisor instances, and worker operations. Everywhere else an integer is
better -- narrower in every foreign key, and never seen by anyone.

Time-ordered so that inserts stay at the right-hand edge of the index and a
sorted listing is chronological. Random in the low bytes so that neither
throughput nor row count can be read off an id.
"""

from __future__ import annotations

import base64
import os
import time

REQUEST = "rq"
CAPTURE = "cp"
EXTRACTION = "ex"
LINKEDIN_ACCOUNT = "la"
ASSIGNMENT = "as"
WORKER_OPERATION = "op"
# One run of a supervisor process, minted at startup. Instances recorded
# before this constant existed carry `wi-`, from when the process was called
# a worker; nothing reads the prefix, and rewriting stored ids would break
# the attempts attributed to them.
SUPERVISOR = "sv"
WORKER = "wk"
SCHEDULE = "sc"
API_KEY = "ak"


def new_id(prefix: str) -> str:
    """`<prefix>-<26 base32 characters>`, e.g. `rq-aebcgtsmaaaaaaaaaaaaaaaaay`.

    Six bytes of millisecond timestamp then ten random, which is the ULID
    layout without the dependency. Lowercase because these get read aloud and
    pasted into shells.

    base32**hex**, not plain base32: RFC 4648's ordinary alphabet runs A-Z then
    2-7, so its digits carry higher values than its letters while sorting below
    them in ASCII. That inverts roughly one pair in five. The extended-hex
    alphabet is 0-9 then A-V, which is monotonic with value and stays monotonic
    lowercased -- which is the only reason any of this is worth doing over a
    random UUID.
    """
    stamp = int(time.time() * 1000).to_bytes(6, "big")
    raw = base64.b32hexencode(stamp + os.urandom(10)).decode("ascii")
    return f"{prefix}-{raw.rstrip('=').lower()}"
