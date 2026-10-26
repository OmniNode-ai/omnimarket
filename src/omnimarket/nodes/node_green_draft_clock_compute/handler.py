"""Compute the green PR draft clock for the throughput tick (OMN-19998).

A PR of a private data repository was green as a draft at 2026-10-18T18:12Z, marked ready about 00:46Z, listed "green for 401m"
by the tick of about 00:53Z, and merged at 01:10:58Z. Its unlanded clock starts no earlier than the last tick that
read it a draft. Pure: this handler reads nothing and writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .models import ModelGreenDraftClockRequest, ModelGreenDraftClockResult


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def green_since(
    pr: Mapping[str, Any], floor: datetime | None = None
) -> datetime | None:
    ci: Mapping[str, Any] = pr["ci"] if isinstance(pr.get("ci"), Mapping) else {}
    stamps = [
        _parse(run[3])
        for run in ci.get("runs") or ()
        if isinstance(run, (list, tuple)) and len(run) > 3
    ]
    found = [s for s in stamps if s is not None]
    since = max(found) if found else _parse(pr.get("first_seen"))
    return max(since, floor) if since is not None and floor is not None else since


class HandlerGreenDraftClock:
    def handle(
        self, request: ModelGreenDraftClockRequest
    ) -> ModelGreenDraftClockResult:
        previous = request.previous if isinstance(request.previous, Mapping) else {}
        drafts = {
            key: request.now.strftime("%Y-%m-%dT%H:%M:%SZ") if draft else previous[key]
            for key, draft in request.drafts_now.items()
            if draft or key in previous
        }
        ready_since = {
            key: at
            for key, stamp in drafts.items()
            if (at := _parse(stamp)) is not None
        }
        return ModelGreenDraftClockResult(drafts=drafts, ready_since=ready_since)
