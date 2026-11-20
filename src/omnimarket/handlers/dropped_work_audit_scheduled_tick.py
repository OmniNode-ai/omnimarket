# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the dropped-work audit: read the ledger, run the audit, raise what it finds (OMN-26205).

The scheduled node reaches the compute node through this shared module, so no node imports another node's private
models. The rules are the compute node's overlay (``ONEX_DROPPED_WORK_AUDIT_OVERLAY`` pins the file, else
``<rules_overlay_id>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``). Rows are read through the schedule overlay's
ledger route; the merged pull requests are read from the pull request watcher's state file the overlay names by
variable; the admission refusals are read from the command the overlay names (the goal scope admission node's typed
completed event, ``not_admitted``). A source that cannot be read fails the run by name: an unreadable source is never
an empty answer.

For each item with no live owner: one owner-missing event through the publisher the caller binds (its event id is the item id),
and, once the alarm delay has
passed, one STATUS alarm row appended under a request id derived from the item id. Items already alarmed on the ledger
are passed to the audit, so a rerun writes no second row. With ``dry_run`` nothing is published or appended.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from collections.abc import Callable, Mapping, MutableSet
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Protocol

from pydantic import TypeAdapter, ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.dropped_work_audit import (
    EnumDroppedWorkKind,
    ModelAdmissionRefusal,
    ModelDroppedWorkAuditRequest,
    ModelDroppedWorkAuditResult,
    ModelDroppedWorkAuditRules,
    ModelDroppedWorkAuditScheduleOverlay,
    ModelOwnerMissingEvent,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_dropped_work_audit_compute.handlers.handler_dropped_work_audit import (
    HandlerDroppedWorkAudit,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
    build_row_source,
    read_queries,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

RULES_FILE_ENV = "ONEX_DROPPED_WORK_AUDIT_OVERLAY"


class DroppedWorkAuditSourceError(RuntimeError):
    """A source the overlay names could not be read or is not bound; the message names it."""


class ModelDroppedWorkAuditTickOutcome(NamedTuple):
    """How one scheduled run went: failed, the detail the run event carries, and the audit's result."""

    failed: bool
    detail: str
    result: ModelDroppedWorkAuditResult | None = None


class ProtocolOwnerMissingPublisher(Protocol):
    def publish(self, event: ModelOwnerMissingEvent) -> bool: ...


def _parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def read_merged_prs(
    schedule: ModelDroppedWorkAuditScheduleOverlay, environ: Mapping[str, str]
) -> dict[str, datetime]:
    """The merged pull requests of the watcher's state file: ``repo#n`` (lower case) to when it merged."""
    path = environ.get(schedule.pr_state_file_env, "")
    if not path:
        raise DroppedWorkAuditSourceError(
            f"{schedule.pr_state_file_env} is not set: the merged pull requests cannot be read"
        )
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        merged = state[schedule.merged_prs_key]
        return {
            str(key).lower(): _parse_stamp(str(value[schedule.merged_at_key]))
            for key, value in merged.items()
            if isinstance(value, dict) and value.get(schedule.merged_at_key)
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DroppedWorkAuditSourceError(
            f"the pull request state file is unreadable: {type(exc).__name__}"
        ) from exc


def read_admission_refusals(
    schedule: ModelDroppedWorkAuditScheduleOverlay,
    rules: ModelDroppedWorkAuditRules,
    environ: Mapping[str, str],
) -> tuple[ModelAdmissionRefusal, ...]:
    """The refusals the goal scope admission node reported, from the overlay's command (a JSON list on stdout)."""
    if EnumDroppedWorkKind.ADMISSION_REFUSAL not in rules.kinds:
        return ()
    route = schedule.admission_refusal_read
    if route is None:
        raise DroppedWorkAuditSourceError(
            "the rules list the admission_refusal kind but the schedule overlay has no admission_refusal_read"
        )
    command = environ.get(route.command_env, "")
    if not command:
        raise DroppedWorkAuditSourceError(
            f"{route.command_env} is not set: the admission refusals cannot be read"
        )
    try:
        done = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=route.timeout_seconds,
            check=True,
        )
        return tuple(
            TypeAdapter(tuple[ModelAdmissionRefusal, ...]).validate_json(done.stdout)
        )
    except (OSError, subprocess.SubprocessError, ValidationError) as exc:
        raise DroppedWorkAuditSourceError(
            f"the admission refusal read failed: {type(exc).__name__}"
        ) from exc


def _read_rows(
    source: ProtocolLedgerRowSource,
    rules: ModelDroppedWorkAuditRules,
    now: datetime,
    correlation_id: str,
) -> tuple[tuple[ModelLedgerEvidenceRow, ...], frozenset[str]]:
    """Every row the audit judges, and the item ids already alarmed (the audit lane's own alarm rows)."""
    until = now + timedelta(seconds=1)
    long_since = now - timedelta(seconds=rules.lookback_seconds)
    ruling_since = now - timedelta(seconds=rules.standing_ruling_lookback_seconds)
    window_since = now - timedelta(seconds=rules.claim_window_seconds)
    wanted: dict[str, datetime] = {}
    for row_type in (
        rules.claim_row_type,
        rules.terminal_row_type,
        rules.hold_row_type,
        rules.promise_row_type,
        *rules.closing_row_types,
        *rules.mechanism_row_types,
        *rules.promise_followup_row_types,
    ):
        wanted[row_type] = min(wanted.get(row_type, long_since), long_since)
    for row_type in rules.ruling_row_types:
        wanted[row_type] = min(wanted.get(row_type, ruling_since), ruling_since)
    for row_type in rules.activity_row_types:
        wanted.setdefault(row_type, window_since)
    queries = [
        ModelLedgerQuery(
            since=since, until=until, row_type=row_type, correlation_id=correlation_id
        )
        for row_type, since in sorted(wanted.items())
    ]
    # the audit lane's own alarm rows
    queries.append(
        ModelLedgerQuery(
            since=long_since,
            until=until,
            row_type=rules.alarm_row_type,
            lane=rules.audit_lane,
            correlation_id=correlation_id,
        )
    )
    answers = read_queries(source, queries)
    rows: dict[str, ModelLedgerEvidenceRow] = {}
    for query, answer in zip(queries, answers, strict=True):
        if answer.error is not None:
            raise DroppedWorkAuditSourceError(
                f"the ledger read of {query.row_type} failed: {answer.error}"
            )
        for row in answer.rows:
            rows[row.raw] = row
    alarmed = frozenset(
        row.cells["item"]
        for row in rows.values()
        if row.lane == rules.audit_lane
        and row.cells.get("alarm") == rules.alarm_value
        and row.cells.get("item")
    )
    return tuple(rows.values()), alarmed


def run_dropped_work_audit_tick(
    *,
    schedule: ModelDroppedWorkAuditScheduleOverlay,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    evented: MutableSet[str],
    source: ProtocolLedgerRowSource | None = None,
    appender: ProtocolReadingAppender | None = None,
    publisher: ProtocolOwnerMissingPublisher | None = None,
    dry_run: bool = False,
) -> ModelDroppedWorkAuditTickOutcome:
    """Run the audit once, the way the runtime tick does, or read-only with ``dry_run``."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    rules = load_structured_overlay(
        schedule.rules_overlay_id,
        ModelDroppedWorkAuditRules,
        env,
        file_env=RULES_FILE_ENV,
    )
    now = clock().astimezone(UTC)
    row_source = (
        source if source is not None else build_row_source(schedule.ledger_read, env)
    )
    try:
        rows, alarmed = _read_rows(row_source, rules, now, uuid.uuid4().hex)
        merged = read_merged_prs(schedule, env)
        refusals = read_admission_refusals(schedule, rules, env)
    except DroppedWorkAuditSourceError as exc:
        return ModelDroppedWorkAuditTickOutcome(True, str(exc))
    result = HandlerDroppedWorkAudit().handle(
        ModelDroppedWorkAuditRequest(
            now=now,
            rules=rules,
            rows=rows,
            merged_prs=merged,
            admission_refusals=refusals,
            alarmed_items=alarmed,
            evented_items=frozenset(evented | alarmed),
        )
    )
    summary = (
        f"items={len(result.items)} unowned={result.unowned} "
        f"events={len(result.events)} alarms={len(result.alarms)} "
        f"kinds={json.dumps(result.counts_by_kind, sort_keys=True)}"
    )
    if dry_run:
        return ModelDroppedWorkAuditTickOutcome(False, f"dry-run {summary}", result)
    sink = (
        appender
        if appender is not None
        else build_appender(schedule.ledger_append, rules.audit_lane, env)
    )
    if publisher is None:
        raise DroppedWorkAuditSourceError(
            "no owner-missing publisher is bound: the events cannot be published"
        )
    unpublished = 0
    for event in result.events:
        if publisher.publish(event):
            evented.add(event.event_id)
        else:
            unpublished += 1
    refused = 0
    for alarm in result.alarms:
        if not sink.append(alarm.row, alarm.request_id).ok:
            refused += 1
    if unpublished or refused:
        return ModelDroppedWorkAuditTickOutcome(
            True,
            f"{summary}; {unpublished} event(s) not published, {refused} alarm row(s) not accepted",
            result,
        )
    return ModelDroppedWorkAuditTickOutcome(False, summary, result)


__all__: list[str] = [
    "DroppedWorkAuditSourceError",
    "LedgerRouteError",
    "ModelDroppedWorkAuditTickOutcome",
    "ProtocolOwnerMissingPublisher",
    "read_admission_refusals",
    "read_merged_prs",
    "run_dropped_work_audit_tick",
]
