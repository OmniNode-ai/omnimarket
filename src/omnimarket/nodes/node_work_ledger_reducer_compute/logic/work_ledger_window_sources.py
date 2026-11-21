# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Readers for the window replay's inputs: the ledger file and its archive rolls, the serve log, the requests (OMN-25629).

The accepted rows are the first line of every row in the ledger texts given (the file of record and its archive
rolls, oldest roll first; the command reads the files). The refused requests are the serve log's ``refused`` lines joined by request id to the recorded
append requests (one JSON ``ModelWorkLedgerAppendRequest`` per line); a refusal whose request is not in the
record cannot be replayed and is reported as unreadable, never skipped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from omnimarket.handlers.ledger_row_grammar import opens_a_row
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_window_replay import (
    ModelRefusedRequest,
)

SERVE_REFUSAL_RE = re.compile(
    r"work-ledger host (?P<host>\S+) request (?P<request_id>[0-9a-fA-F-]{36}) refused: (?P<message>.*)"
)


def accepted_rows(texts: Iterable[str]) -> tuple[str, ...]:
    """The row-opening line of every row in the ledger texts, given oldest roll first."""
    return tuple(
        line for text in texts for line in text.splitlines() if opens_a_row(line)
    )


def serve_log_refusals(log_text: str) -> dict[str, str]:
    """Request id to the refusal message's first line, for every refused request the log names."""
    found: dict[str, str] = {}
    for line in log_text.splitlines():
        match = SERVE_REFUSAL_RE.search(line)
        if match is not None:
            found[match["request_id"].lower()] = match["message"].strip()
    return found


def recorded_requests(text: str) -> dict[str, ModelWorkLedgerAppendRequest]:
    found: dict[str, ModelWorkLedgerAppendRequest] = {}
    for line in text.splitlines():
        if line.strip():
            request = ModelWorkLedgerAppendRequest.model_validate(json.loads(line))
            found[str(request.request_id).lower()] = request
    return found


def join_refusals(
    refusals: dict[str, str], requests: dict[str, ModelWorkLedgerAppendRequest]
) -> tuple[tuple[ModelRefusedRequest, ...], tuple[str, ...]]:
    joined: list[ModelRefusedRequest] = []
    unreadable: list[str] = []
    for request_id, message in refusals.items():
        request = requests.get(request_id)
        if request is None:
            unreadable.append(request_id)
            continue
        joined.append(
            ModelRefusedRequest(
                request_id=request_id,
                rows=request.rows,
                requested_at=request.requested_at,
                file_judge_message=message,
            )
        )
    return tuple(joined), tuple(unreadable)


__all__: list[str] = [
    "accepted_rows",
    "join_refusals",
    "recorded_requests",
    "serve_log_refusals",
]
