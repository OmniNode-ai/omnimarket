# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B friction rollup decisions over source texts (OMN-18008)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from omnimarket.nodes.node_friction_rollup_compute.handlers.friction_rollup_core import (
    LedgerRow,
    SourceSummary,
    _is_friction,
    _parse_aliases,
    build_report,
    parse_iso8601,
    parse_jsonl_rows,
    parse_markdown_rows,
    render_markdown,
)
from omnimarket.nodes.node_friction_rollup_compute.models.model_friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupResult,
    ModelFrictionRollupSource,
)


def _load_rows(
    sources: Sequence[ModelFrictionRollupSource], since: datetime, until: datetime
) -> tuple[list[LedgerRow], list[SourceSummary], int, int]:
    """Select, summarize, de-duplicate and sort source rows without reading paths."""
    rows: list[LedgerRow] = []
    # The old loader emitted an absent archive summary before parsing any inputs.
    summaries = [
        SourceSummary(path=source.display, status="missing")
        for source in sources
        if source.kind == "missing"
    ]
    jsonl_malformed = 0
    for source in sources:
        if source.kind == "missing":
            continue
        assert source.text is not None
        summary = SourceSummary(path=source.display)
        if source.kind == "jsonl":
            parsed, malformed = parse_jsonl_rows(source.text, source.display)
            summary.malformed_lines = malformed
            jsonl_malformed += malformed
        else:
            parsed = parse_markdown_rows(source.text, source.display)
        summary.rows_parsed = len(parsed)
        selected = [row for row in parsed if since <= row.ts < until]
        summary.rows_in_window = len(selected)
        summary.friction_rows_in_window = sum(_is_friction(row) for row in selected)
        summaries.append(summary)
        rows.extend(selected)
    deduplicated: list[LedgerRow] = []
    seen: set[str] = set()
    duplicates = 0
    for row in rows:
        if row.text in seen:
            duplicates += 1
            continue
        seen.add(row.text)
        deduplicated.append(row)
    deduplicated.sort(key=lambda row: (row.ts, row.source, row.line))
    return deduplicated, summaries, duplicates, jsonl_malformed


class HandlerFrictionRollup:
    """Return the ledger rollup's decisions without IO or a clock."""

    def handle(self, request: ModelFrictionRollupRequest) -> ModelFrictionRollupResult:
        since, until = parse_iso8601(request.since), parse_iso8601(request.until)
        rows, sources, duplicates, malformed = _load_rows(request.sources, since, until)
        report = build_report(
            rows,
            sources,
            since=since,
            until=until,
            generated_at=parse_iso8601(request.generated_at),
            bucket_hours=request.bucket_hours,
            umbrella_tickets=frozenset(
                ticket.upper() for ticket in request.umbrella_tickets
            ),
            aliases=_parse_aliases(request.aliases),
            duplicates_dropped=duplicates,
            jsonl_provided=any(source.kind == "jsonl" for source in request.sources),
            jsonl_malformed_lines=malformed,
        )
        return ModelFrictionRollupResult(
            report=report,
            markdown=render_markdown(report),
            json_text=json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        )
