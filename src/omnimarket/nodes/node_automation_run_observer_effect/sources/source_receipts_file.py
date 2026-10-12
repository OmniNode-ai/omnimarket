# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-run receipts: a JSON-lines file with one object per run record.

A record carries started_at and, once the run ended, finished_at,
exit_code and optionally outcome, run_id and the work and demand
fields the overlay entry names. A record without finished_at is a start; a
later record with the same run_id completes it. Only complete lines are
consumed, so a half-written record is read on the next poll.
"""

import json
from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    DEMAND_NONE,
    EnumAutomationEvidenceSource,
    EnumAutomationRunOutcome,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.field_readers import (
    count_work,
    demand_field_name,
    outcome_for_exit,
    parse_instant,
    read_complete_lines,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.protocol_run_evidence_source import (
    ModelEvidenceRead,
    ModelEvidenceReadContext,
    ModelObservedRun,
)


class SourceReceiptsFile:
    @property
    def source(self) -> EnumAutomationEvidenceSource:
        return EnumAutomationEvidenceSource.RECEIPTS_FILE

    def read(self, context: ModelEvidenceReadContext) -> ModelEvidenceRead:
        entry = context.entry
        evidence = entry.evidence
        if evidence is None:
            return ModelEvidenceRead(
                cursor=context.cursor, unreadable="entry names no evidence"
            )
        path = Path(evidence.locator)
        try:
            lines, end = read_complete_lines(path, context.cursor.offset)
        except OSError as exc:
            return ModelEvidenceRead(cursor=context.cursor, unreadable=f"{path}: {exc}")
        runs: list[ModelObservedRun] = []
        malformed = 0
        for line_no, line in lines:
            if not line.strip():
                continue
            run = _parse_record(
                line,
                entry.process_id,
                entry.real_work,
                entry.demand,
                f"{path}#L{line_no}",
            )
            if run is None:
                malformed += 1
            else:
                runs.append(run)
        cursor = context.cursor.model_copy(update={"offset": end})
        return ModelEvidenceRead(runs=tuple(runs), cursor=cursor, malformed=malformed)


def _int_fields(record: dict[str, object]) -> dict[str, int]:
    return {
        key: value
        for key, value in record.items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }


def _parse_record(
    line: bytes, process_id: str, real_work: str, demand: str, ref: str
) -> ModelObservedRun | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    started_text = record.get("started_at")
    started = parse_instant(started_text) if isinstance(started_text, str) else None
    if started is None:
        return None
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        run_id = f"{process_id}:{started.isoformat()}"
    finished_text = record.get("finished_at")
    finished = parse_instant(finished_text) if isinstance(finished_text, str) else None
    if finished is None and finished_text is not None:
        return None
    if finished is None:
        return ModelObservedRun(run_id=run_id, started_at=started, evidence_ref=ref)
    if finished < started:
        return None
    exit_code = record.get("exit_code")
    exit_value = (
        exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else None
    )
    outcome: EnumAutomationRunOutcome | None = None
    outcome_text = record.get("outcome")
    if isinstance(outcome_text, str):
        try:
            outcome = EnumAutomationRunOutcome(outcome_text)
        except ValueError:
            return None
    if outcome is None:
        if exit_value is None:
            return None
        outcome = outcome_for_exit(exit_value)
    fields = _int_fields(record)
    demand_name = demand_field_name(demand)
    return ModelObservedRun(
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        outcome=outcome,
        exit_code=exit_value,
        did_work_count=count_work(real_work, fields),
        demand_count=(
            fields.get(demand_name)
            if demand_name is not None and demand != DEMAND_NONE
            else None
        ),
        evidence_ref=ref,
    )
