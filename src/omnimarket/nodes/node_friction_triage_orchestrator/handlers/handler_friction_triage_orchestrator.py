# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_friction_triage_orchestrator [OMN-12205].

ORCHESTRATOR node. Reads the friction registry (NDJSON), aggregates events
by skill:surface over a rolling window, deduplicates against open Linear tickets
via stable surface-key markers, and creates new tickets for surfaces crossing
threshold (count >= 3 OR severity_score >= 9).

Live ticket creation requires an injected adapter; dry-run is deterministic.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from omnimarket.nodes.node_friction_triage_orchestrator.models.model_friction_triage_request import (
    ModelFrictionTriageRequest,
    ModelFrictionTriageResult,
)


class ProtocolFrictionLinearAdapter(Protocol):
    """Adapter boundary for Linear friction-ticket checks and creation."""

    def open_surface_keys(self) -> set[str]: ...

    def create_ticket(self, payload: dict[str, Any]) -> str: ...


class HandlerFrictionTriageOrchestrator:
    """Aggregate friction registry rows and produce tickets for threshold crossings."""

    def __init__(self, adapter: ProtocolFrictionLinearAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(self, request: ModelFrictionTriageRequest) -> ModelFrictionTriageResult:
        rows = _read_registry(Path(request.friction_registry_path))
        today = date.today()
        window_start = today - timedelta(days=request.window_days - 1)
        aggregates = _aggregate_rows(rows, window_start)
        crossings = {
            key: aggregate
            for key, aggregate in aggregates.items()
            if aggregate["count"] >= request.threshold_count
            or aggregate["severity_score"] >= request.threshold_score
        }

        existing_keys = self._adapter.open_surface_keys() if self._adapter else set()
        create_keys = sorted(key for key in crossings if key not in existing_keys)
        skipped = len(crossings) - len(create_keys)
        created_ids: list[str] = []

        if not request.dry_run and create_keys:
            if self._adapter is None:
                raise RuntimeError("linear adapter required when dry_run is false")
            for key in create_keys:
                created_ids.append(
                    self._adapter.create_ticket(
                        _ticket_payload(key, crossings[key], request)
                    )
                )

        return ModelFrictionTriageResult(
            surfaces_tracked=len(aggregates),
            threshold_crossings=len(crossings),
            tickets_created=0 if request.dry_run else len(created_ids),
            tickets_skipped=skipped,
            created_ticket_ids=tuple(created_ids),
            dry_run=request.dry_run,
            summary=(
                f"{len(crossings)} of {len(aggregates)} surfaces crossed threshold; "
                f"{skipped} skipped due to existing open tickets."
            ),
        )


def _read_registry(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _aggregate_rows(
    rows: list[dict[str, Any]], window_start: date
) -> dict[str, dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "severity_score": 0, "examples": []}
    )
    for row in rows:
        occurred_at = _row_date(row)
        if occurred_at is not None and occurred_at < window_start:
            continue
        key = _surface_key(row)
        aggregate = aggregates[key]
        aggregate["count"] += 1
        aggregate["severity_score"] += int(
            row.get("severity_score", row.get("score", 1)) or 1
        )
        if len(aggregate["examples"]) < 3:
            aggregate["examples"].append(
                str(row.get("message") or row.get("summary") or "")
            )
    return dict(aggregates)


def _surface_key(row: dict[str, Any]) -> str:
    skill = str(row.get("skill") or row.get("skill_name") or "unknown").strip()
    surface = str(
        row.get("surface") or row.get("surface_key") or row.get("file") or "unknown"
    ).strip()
    return f"{skill}:{surface}"


def _row_date(row: dict[str, Any]) -> date | None:
    value = row.get("timestamp") or row.get("occurred_at") or row.get("created_at")
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def _ticket_payload(
    key: str, aggregate: dict[str, Any], request: ModelFrictionTriageRequest
) -> dict[str, Any]:
    return {
        "title": f"Friction threshold crossed: {key}",
        "description": (
            f"Surface key: {key}\n"
            f"Events: {aggregate['count']}\n"
            f"Severity score: {aggregate['severity_score']}\n"
            f"Examples: {aggregate['examples']}"
        ),
        "team": request.linear_team,
        "project": request.linear_project,
        "surface_key": key,
    }
