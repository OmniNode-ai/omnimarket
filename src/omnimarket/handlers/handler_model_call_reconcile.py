# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join a model server's access log to ``delegation_events`` runs (OMN-20299).

The server's own log is the one record no caller can skip. Every inference
request it answered either carries a run correlation id that names a
``delegation_events`` row, or it bypassed the delegation nodes. The join is
pure: callers read the log and the table, this module classifies.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import parse_qs

from omnimarket.models.model_call_correlation import (
    SELF_HOSTED_CORRELATION_QUERY_PARAM,
)
from omnimarket.models.model_call_reconcile import (
    EnumServedRequestClass,
    ModelClassifiedRequest,
    ModelReconcileReport,
    ModelServedRequest,
)

UNATTRIBUTED_LANE: Final[str] = "unattributed"

# uvicorn access line as vLLM prints it; an optional journal timestamp leads.
_ACCESS_LINE = re.compile(
    r'^(?P<ts>\S+)?.*?"POST (?P<path>[^\s?"]+)(?:\?(?P<query>[^\s"]*))? HTTP/[\d.]+" '
    r"(?P<status>\d{3})"
)
_INFERENCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/completions",
        "/completions",
        "/completion",
        "/v1/messages",
    }
)
_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def parse_access_log_line(line: str, server: str) -> ModelServedRequest | None:
    """Return the inference request a log line records, or ``None``."""
    match = _ACCESS_LINE.match(line.strip())
    if match is None or match["path"] not in _INFERENCE_PATHS:
        return None
    query = parse_qs(match["query"] or "")
    cids = query.get(SELF_HOSTED_CORRELATION_QUERY_PARAM)
    ts = match["ts"]
    return ModelServedRequest(
        server=server,
        timestamp=ts if ts and _ISO_TS.match(ts) else None,
        path=match["path"],
        status=int(match["status"]),
        correlation_id=cids[0] if cids else None,
    )


def parse_access_log(lines: Iterable[str], server: str) -> list[ModelServedRequest]:
    return [
        request
        for line in lines
        if (request := parse_access_log_line(line, server)) is not None
    ]


def classify(
    requests: Iterable[ModelServedRequest],
    run_lanes: Mapping[str, str | None],
) -> list[ModelClassifiedRequest]:
    """Classify each request against ``run_lanes`` (correlation_id -> caller_lane)."""
    classified: list[ModelClassifiedRequest] = []
    for request in requests:
        cid = request.correlation_id
        if cid is None:
            kind, lane = EnumServedRequestClass.BYPASS, None
        elif cid in run_lanes:
            kind = EnumServedRequestClass.ATTRIBUTED
            lane = run_lanes[cid] or UNATTRIBUTED_LANE
        else:
            kind, lane = EnumServedRequestClass.ORPHAN_CORRELATION_ID, None
        classified.append(
            ModelClassifiedRequest(
                request=request, classification=kind, caller_lane=lane
            )
        )
    return classified


def build_report(
    classified: list[ModelClassifiedRequest], sample_limit: int = 20
) -> ModelReconcileReport:
    lanes: Counter[str] = Counter()
    bypass_servers: Counter[str] = Counter()
    samples: list[ModelServedRequest] = []
    orphans = 0
    for item in classified:
        if item.classification is EnumServedRequestClass.ATTRIBUTED:
            lanes[item.caller_lane or UNATTRIBUTED_LANE] += 1
        elif item.classification is EnumServedRequestClass.ORPHAN_CORRELATION_ID:
            orphans += 1
        else:
            bypass_servers[item.request.server] += 1
            if len(samples) < sample_limit:
                samples.append(item.request)
    return ModelReconcileReport(
        total=len(classified),
        attributed=sum(lanes.values()),
        orphan_correlation_id=orphans,
        bypass=sum(bypass_servers.values()),
        attributed_by_lane=dict(sorted(lanes.items())),
        bypass_by_server=dict(sorted(bypass_servers.items())),
        bypass_samples=samples,
    )
