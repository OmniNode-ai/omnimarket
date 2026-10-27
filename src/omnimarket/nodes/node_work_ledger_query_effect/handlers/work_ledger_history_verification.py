#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare exported runtime answers with archive files, without database writes.

Supply the exact unfiltered history request, its full terminal payload, and a
second terminal payload for that request with a smaller limit. Repeat --archive
SOURCE=PATH in file order. Source labels and sequence bounds must come from the
operator's acquisition manifest; this verifier never invents provenance.

Only counts and hashes are printed. Ledger text and connection details remain
in the supplied private files. A fixture replay proves only that fixture, not
an installed runtime or the authority of a historical backfill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify(
    request: ModelWorkLedgerQueryRequest,
    archives: Mapping[str, Path],
    full: ModelWorkLedgerQueryResult,
    truncated: ModelWorkLedgerQueryResult,
) -> dict[str, object]:
    """Fail closed on any row, authority, range or truncation discrepancy."""
    bounds = request.history
    _require(bounds is not None, "an explicit history manifest is required")
    assert bounds is not None
    assert request.until is not None
    _require(request.query is EnumWorkLedgerQueryKind.ROWS, "query must be rows")
    _require(
        request.limit is None and request.cursor is None,
        "full request must be unpaginated",
    )
    _require(
        not any(
            (
                request.lane,
                request.lanes,
                request.ticket,
                request.pr,
                request.kinds,
                request.row_ref,
                request.term,
                request.principal,
                request.run,
            )
        ),
        "archive proof must be unfiltered",
    )
    _require(
        tuple(archives) == bounds.sources, "archive sources must match manifest order"
    )
    expected: list[tuple[str, str]] = []
    hashes = {}
    for source, path in archives.items():
        data = path.read_bytes()
        rows = split_ledger_rows(data.decode("utf-8"))
        _require(bool(rows), "an archive contains no rows")
        expected.extend((source, row) for row in rows)
        hashes[source] = hashlib.sha256(data).hexdigest()
    _require(
        len(expected) == bounds.last_seq - bounds.first_seq + 1,
        "manifest extent differs from archive files",
    )
    for answer in (full, truncated):
        _require(answer.error is None, "runtime query failed")
        _require(
            answer.query == request.query
            and answer.correlation_id == request.correlation_id,
            "answer belongs to another request",
        )
        _require(
            answer.window_until == request.until and answer.window_since is None,
            "answer cutoff differs from request",
        )
        coverage = answer.completeness
        _require(coverage is not None, "answer has no completeness evidence")
        assert coverage is not None
        _require(
            (coverage.first_seq, coverage.last_seq, coverage.cutoff)
            == (bounds.first_seq, bounds.last_seq, request.until),
            "answer range differs from manifest",
        )
        _require(
            set(coverage.sources) == set(bounds.sources),
            "answer source coverage differs",
        )
        _require(
            coverage.rows_read == len(expected) and answer.matched == len(expected),
            "answer did not acquire every archive row",
        )
    assert full.completeness is not None
    assert truncated.completeness is not None
    _require(
        full.completeness.status == "complete" and not full.completeness.reasons,
        "full range is incomplete",
    )
    _require(
        full.next_cursor is None and len(full.rows) == len(expected),
        "full answer is truncated",
    )
    for sequence, (row, (source, raw)) in enumerate(
        zip(full.rows, expected, strict=True), start=bounds.first_seq
    ):
        _require(
            row.raw_row == raw and row.source == source,
            f"archive row mismatch at sequence {sequence}",
        )
        _require(
            row.ledger_seq == sequence, f"sequence mismatch at position {sequence}"
        )
        _require(
            row.ingested_at is not None and row.ingested_at < request.until,
            f"missing or out-of-range ingest authority at sequence {sequence}",
        )
    _require(
        truncated.completeness.status == "incomplete"
        and truncated.completeness.reasons == ("truncated",),
        "truncated answer must report incomplete",
    )
    _require(
        0 < len(truncated.rows) < len(full.rows) and truncated.next_cursor is not None,
        "truncated answer needs a partial page and cursor",
    )
    _require(
        truncated.rows == full.rows[: len(truncated.rows)],
        "truncated rows differ from full answer prefix",
    )
    _require(
        truncated.completeness.source_hash == full.completeness.source_hash,
        "answers belong to different source snapshots",
    )
    return {
        "rows": len(expected),
        "sources": len(archives),
        "truncated_rows": len(truncated.rows),
        "archive_sha256": hashes,
        "source_hash": full.completeness.source_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--truncated-response", type=Path, required=True)
    parser.add_argument(
        "--archive", action="append", required=True, metavar="SOURCE=PATH"
    )
    args = parser.parse_args()
    archives = {}
    for item in args.archive:
        source, separator, path = item.partition("=")
        if not separator or not source or not path or source in archives:
            parser.error("each --archive must have a unique SOURCE=PATH")
        archives[source] = Path(path)
    try:
        result = verify(
            ModelWorkLedgerQueryRequest.model_validate_json(args.request.read_text()),
            archives,
            ModelWorkLedgerQueryResult.model_validate_json(args.response.read_text()),
            ModelWorkLedgerQueryResult.model_validate_json(
                args.truncated_response.read_text()
            ),
        )
    except (ValueError, OSError):
        # Validation errors may include raw ledger text; do not echo inputs.
        sys.stderr.write(
            "Ledger history verification failed; inspect the private inputs.\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
