# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cron completion evidence: a per-run log line carrying the run's exit.

Each log line is ``<ISO-8601 instant with offset> <message>``. The entry's
``evidence.completion_record`` marks a completion: a regular expression with
an ``exit`` group, or a literal followed by the exit code (``run finished
exit=`` matches ``run finished exit=0``). The start line is the same text with
``finished`` replaced by ``started`` and cut there (``run started``); an entry
whose completion record has no ``finished`` has no start line, so only its
completions are seen. A start with no completion is an open run; the handler
reports it OVERRUN once it passes ``max_runtime_seconds``. A work or demand
field the entry names is read as ``name=<n>`` from the completion line.
"""

import re
from datetime import datetime
from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    DEMAND_NONE,
    EnumAutomationEvidenceSource,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.field_readers import (
    demand_field_name,
    outcome_for_exit,
    parse_instant,
    read_complete_lines,
    work_field_names,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.protocol_run_evidence_source import (
    ModelEvidenceRead,
    ModelEvidenceReadContext,
    ModelObservedRun,
)

_EXIT_GROUP = "(?P<exit>"


def completion_pattern(completion_record: str) -> re.Pattern[str]:
    if _EXIT_GROUP in completion_record:
        return re.compile(completion_record)
    return re.compile(re.escape(completion_record) + r"(?P<exit>-?\d+)")


def start_marker(completion_record: str) -> str | None:
    head, found, _ = completion_record.partition("finished")
    return head + "started" if found else None


def _named_count(name: str, message: str) -> int | None:
    found = re.search(rf"\b{re.escape(name)}=(\d+)", message)
    return int(found.group(1)) if found else None


class SourceLogLine:
    @property
    def source(self) -> EnumAutomationEvidenceSource:
        return EnumAutomationEvidenceSource.LOG_LINE

    def read(self, context: ModelEvidenceReadContext) -> ModelEvidenceRead:
        entry = context.entry
        evidence = entry.evidence
        if evidence is None or evidence.completion_record is None:
            return ModelEvidenceRead(
                cursor=context.cursor,
                unreadable="entry names no completion record to read",
            )
        path = Path(evidence.locator)
        try:
            lines, end = read_complete_lines(path, context.cursor.offset)
        except OSError as exc:
            return ModelEvidenceRead(cursor=context.cursor, unreadable=f"{path}: {exc}")
        completion = completion_pattern(evidence.completion_record)
        start = start_marker(evidence.completion_record)
        open_runs: dict[str, datetime] = dict(context.cursor.open_runs)
        runs: list[ModelObservedRun] = []
        malformed = 0
        for line_no, raw in lines:
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            stamp, _, message = text.partition(" ")
            at = parse_instant(stamp)
            if at is None:
                malformed += 1
                continue
            ref = f"{path}#L{line_no}"
            found = completion.search(message)
            if found is not None:
                run_id = (
                    _closest_open(open_runs, at)
                    or f"{entry.process_id}:{at.isoformat()}"
                )
                started = open_runs.pop(run_id, at)
                code = int(found.group("exit"))
                work = sum(
                    _named_count(name, message) or 0
                    for name in work_field_names(entry.real_work)
                )
                demand_name = demand_field_name(entry.demand)
                runs.append(
                    ModelObservedRun(
                        run_id=run_id,
                        started_at=started,
                        finished_at=max(at, started),
                        outcome=outcome_for_exit(code),
                        exit_code=code,
                        did_work_count=work,
                        demand_count=(
                            _named_count(demand_name, message)
                            if demand_name is not None and entry.demand != DEMAND_NONE
                            else None
                        ),
                        evidence_ref=ref,
                    )
                )
            elif start is not None and start in message:
                run_id = f"{entry.process_id}:{at.isoformat()}"
                open_runs[run_id] = at
                runs.append(
                    ModelObservedRun(run_id=run_id, started_at=at, evidence_ref=ref)
                )
        cursor = context.cursor.model_copy(update={"offset": end})
        return ModelEvidenceRead(runs=tuple(runs), cursor=cursor, malformed=malformed)


def _closest_open(open_runs: dict[str, datetime], at: datetime) -> str | None:
    """The open run that started most recently before the completion."""
    candidates = [
        (started, run_id) for run_id, started in open_runs.items() if started <= at
    ]
    return max(candidates)[1] if candidates else None
