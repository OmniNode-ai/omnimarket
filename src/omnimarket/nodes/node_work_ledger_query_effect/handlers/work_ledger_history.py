# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, sequence-ordered history acquisition and manifest-bound pagination."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerCompleteness,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)


def _binding(request: ModelWorkLedgerQueryRequest) -> str:
    manifest = request.model_dump(
        mode="json", exclude={"correlation_id", "now", "cursor", "limit"}
    )
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def _position(request: ModelWorkLedgerQueryRequest, binding: str) -> int:
    assert request.history is not None
    if request.cursor is None:
        return request.history.first_seq - 1
    try:
        token = json.loads(
            base64.b64decode(request.cursor, altchars=b"-_", validate=True)
        )
        if set(token) != {"version", "binding", "after"}:
            raise ValueError("invalid cursor shape")
        if token["version"] != 1 or token["binding"] != binding:
            raise ValueError("cursor does not match acquisition manifest")
        after = token["after"]
        if (
            type(after) is not int
            or not request.history.first_seq <= after <= request.history.last_seq
        ):
            raise ValueError("cursor is outside acquisition range")
        return after
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid history cursor") from exc


def history_answer(
    request: ModelWorkLedgerQueryRequest,
    records: tuple[ModelWorkLedgerRowRecord, ...],
) -> dict[str, object]:
    """Check the whole source range before applying any discovery filter.

    A complete last page proves the remaining range of that cursor only. A
    consumer must seal every page and cursor from the initial request. Sequence
    bounds and source names do not discover or certify their own archive extent.
    """
    bounds = request.history
    assert bounds is not None
    assert request.until is not None
    reasons: set[str] = set()
    before_cutoff = tuple(
        row
        for row in records
        if row.ingested_at is None or row.ingested_at < request.until
    )
    source_hash = hashlib.sha256(
        json.dumps(
            [
                row.model_dump(mode="json")
                for row in sorted(
                    before_cutoff, key=lambda row: (row.ledger_seq or 0, row.row_id)
                )
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    binding = hashlib.sha256((_binding(request) + source_hash).encode()).hexdigest()
    after = _position(request, binding)
    if any(row.ledger_seq is None for row in before_cutoff):
        reasons.add("missing-sequence")
    if any(row.ingested_at is None for row in before_cutoff):
        reasons.add("missing-ingest-time")
    ranged = tuple(
        row
        for row in before_cutoff
        if row.ledger_seq is not None
        and bounds.first_seq <= row.ledger_seq <= bounds.last_seq
    )
    counts = Counter(row.ledger_seq for row in ranged)
    # Count the interval rather than allocate a potentially huge range/set.
    if len(counts) != bounds.last_seq - bounds.first_seq + 1:
        reasons.add("sequence-gap")
    if any(count > 1 for count in counts.values()):
        reasons.add("duplicate-sequence")
    sources = tuple(sorted({row.source for row in ranged}))
    if not set(bounds.sources).issubset(sources):
        reasons.add("missing-source")
    if any(row.source in {"", "unknown"} for row in ranged):
        reasons.add("missing-source")
    ordered = sorted(ranged, key=lambda row: (row.ledger_seq or 0, row.row_id))
    parsed = replay.parse_rows(ordered)
    canonical = []
    kinds = {kind.value for kind in EnumLedgerRowType}
    for row in parsed:
        if (
            row.row_type not in kinds
            or row.text != row.record.raw_row.split("\n", 1)[0]
        ):
            reasons.add("invalid-source-row")
            continue
        if row.row_type != row.record.row_type:
            reasons.add("conflicting-row-type")
        canonical.append(row)
    if len(canonical) != len(ordered):
        reasons.add("invalid-source-row")
    matched = replay.select_rows(
        canonical,
        lane=request.lane,
        pr=request.pr,
        ticket=request.ticket,
        kinds=request.kinds,
        term=request.term,
    )
    if request.lanes:
        matched = [
            row
            for row in matched
            if any(replay.select_rows([row], lane=lane) for lane in request.lanes)
        ]
    if request.run is not None:
        matched = [row for row in matched if row.token("run") == request.run]
    if request.principal is not None:
        matched = [
            row for row in matched if row.token("principal") == request.principal
        ]
    if request.row_ref is not None:
        matched = [
            row
            for row in matched
            if row.record.row_id == request.row_ref
            or any(
                re.search(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(request.row_ref)}(?![A-Za-z0-9_.-])",
                    value,
                )
                for key in ("id", "re", "closes-claim", "supersedes-claim")
                for value in row.fields.get(key, ())
            )
        ]
    remaining = [row.record for row in matched if (row.record.ledger_seq or 0) > after]
    shown = remaining[: request.limit] if request.limit else remaining
    cursor = None
    if len(shown) < len(remaining):
        reasons.add("truncated")
        # A duplicate position must never straddle pages: completeness already
        # fails and no cursor may silently skip a distinct row with the same seq.
        if "duplicate-sequence" not in reasons:
            cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "version": 1,
                        "binding": binding,
                        "after": shown[-1].ledger_seq,
                    },
                    sort_keys=True,
                ).encode()
            ).decode()
    return {
        "matched": len(matched),
        "rows": tuple(shown),
        "next_cursor": cursor,
        "completeness": ModelWorkLedgerCompleteness(
            status="incomplete" if reasons else "complete",
            reasons=tuple(sorted(reasons)),
            first_seq=bounds.first_seq,
            last_seq=bounds.last_seq,
            cutoff=request.until,
            sources=sources,
            rows_read=len(before_cutoff),
            source_hash=source_hash,
        ),
    }
