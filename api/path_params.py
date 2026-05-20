"""Shared FastAPI path / query parameter type aliases.

Centralising these so every endpoint that takes a `job_id` validates with
the exact same rules and ships the exact same example to Swagger UI.

The historical pattern was `job_id: str = Path(..., min_length=1, max_length=64)`
with no description / example / pattern. Swagger UI then defaulted the
field placeholder to a useless value, and users would try "4" (the integer
id from /templates) and hit the 404 path — see the save-as-template bug
report. Tightening to the actual format (`uuid4().hex`, 32 lowercase hex
chars) makes the schema self-documenting and rejects malformed inputs at
the framework boundary with 422 instead of a misleading 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path, Query


# Format: `uuid4().hex` produces a 32-char lowercase hexadecimal string.
# See api/main.py:440 (`job_id = uuid4().hex`).
_JOB_ID_PATTERN = r"^[0-9a-f]{32}$"

_JOB_ID_EXAMPLE = "4258164ae27247ff9ce3671ae44f3217"

_JOB_ID_DESCRIPTION = (
    "Job identifier — a 32-character lowercase hex UUID returned by "
    "`POST /generate-data-json` in the `job_id` field. **Not** the same "
    "as a template `id` (templates use small integer IDs from "
    "`GET /templates`). Pass the value from the submission response, not "
    "a list-position number."
)


JobIdPath = Annotated[
    str,
    Path(
        ...,
        title="Job ID",
        description=_JOB_ID_DESCRIPTION,
        pattern=_JOB_ID_PATTERN,
        examples=[_JOB_ID_EXAMPLE],
    ),
]


# FastAPI rejects `default=` inside a `Query(...)` that's wrapped in
# `Annotated`; the default must be set at the parameter site with `=`.
# Usage: `job_id: JobIdQuery = None`.
JobIdQuery = Annotated[
    str | None,
    Query(
        title="Job ID",
        description=_JOB_ID_DESCRIPTION,
        pattern=_JOB_ID_PATTERN,
        examples=[_JOB_ID_EXAMPLE],
    ),
]
