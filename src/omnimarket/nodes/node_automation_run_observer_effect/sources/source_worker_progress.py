# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A daemon's worker-loop progress: a JSON state file the worker loop rewrites.

The file carries ``progress_counter`` (counted by the worker loop each time it
applies a unit of work), optionally ``last_progress_at`` and ``started_at``
(ISO-8601 instants with an offset), and the demand field the entry names. A
``pid`` or an advertiser timestamp in the same file is deliberately not read:
a wedged worker behind a live process and a live advertiser must report no
progress, so only the counter can move progress.
"""

import json
from datetime import datetime
from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEvidenceSource,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.field_readers import (
    demand_field_name,
    parse_instant,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.protocol_run_evidence_source import (
    ModelDaemonProgress,
    ModelEvidenceRead,
    ModelEvidenceReadContext,
)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class SourceWorkerProgress:
    @property
    def source(self) -> EnumAutomationEvidenceSource:
        return EnumAutomationEvidenceSource.STATE_FILE

    def read(self, context: ModelEvidenceReadContext) -> ModelEvidenceRead:
        evidence = context.entry.evidence
        if evidence is None:
            return ModelEvidenceRead(
                cursor=context.cursor, unreadable="entry names no evidence"
            )
        path = Path(evidence.locator)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return ModelEvidenceRead(cursor=context.cursor, unreadable=f"{path}: {exc}")
        if not isinstance(record, dict):
            return ModelEvidenceRead(
                cursor=context.cursor, unreadable=f"{path}: not a JSON object"
            )
        counter = _count(record.get("progress_counter"))
        if counter is None:
            return ModelEvidenceRead(
                cursor=context.cursor,
                unreadable=f"{path}: no non-negative integer progress_counter",
            )
        instants: dict[str, datetime | None] = {}
        for name in ("last_progress_at", "started_at"):
            text = record.get(name)
            if text is None:
                instants[name] = None
                continue
            parsed = parse_instant(text) if isinstance(text, str) else None
            if parsed is None:
                return ModelEvidenceRead(
                    cursor=context.cursor,
                    unreadable=f"{path}: {name} is not an ISO-8601 instant with offset",
                )
            instants[name] = parsed
        demand_name = demand_field_name(context.entry.demand)
        return ModelEvidenceRead(
            progress=ModelDaemonProgress(
                progress_counter=counter,
                last_progress_at=instants["last_progress_at"],
                process_started_at=instants["started_at"],
                demand_count=(
                    _count(record.get(demand_name)) if demand_name is not None else None
                ),
            ),
            cursor=context.cursor,
        )
