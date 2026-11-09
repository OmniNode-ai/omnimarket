# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_commitment_check_runner_effect: run each due commitment's check, then close it or reopen it on the ledger.

``handle(ModelCommitmentCheckCommand) -> ModelCommitmentCheckResult``. The overlay's rules, the ledger row source and
appender, the check executor and the clock are constructor parameters, so the in-memory profile and the real backend
run the same handler. One run: read the commitment rows, fold them (node_commitment_ledger_compute), pick the due ones
(open and regressed every run, verified once its newest reading is the overlay's interval old), run each check argv with
no shell, and append one reading row where the commitment changed state or a verified one was re-read:

    open       + check passes -> verified        open      + check fails -> stays open (no row)
    regressed  + check passes -> verified        regressed + check fails -> stays regressed (no row)
    verified   + check passes -> verified (fresh reading)
    verified   + check fails  -> regressed

A check whose argv[0] the overlay does not allow is refused and never executed. A ledger that cannot be read fails the
run with no check executed; a reading row the ledger did not accept leaves the commitment where it was.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.commitment_rows import (
    fold_commitments,
    result_row,
)
from omnimarket.models.commitment import (
    EnumCommitmentStatus,
    ModelCommitment,
    ModelCommitmentRules,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCheckExecution,
    ModelCommitmentCheckCommand,
    ModelCommitmentCheckOutcome,
    ModelCommitmentCheckResult,
    ModelCommitmentCheckRules,
)
from omnimarket.nodes.node_commitment_check_runner_effect.protocols import (
    ProtocolCheckExecutor,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_TOKEN = re.compile(r"[^A-Za-z0-9_.:-]+")

_RESULT_BY_TRANSITION = {
    (EnumCommitmentStatus.OPEN, True): "verified",
    (EnumCommitmentStatus.REGRESSED, True): "reverified",
    (EnumCommitmentStatus.VERIFIED, True): "refreshed",
    (EnumCommitmentStatus.VERIFIED, False): "regressed",
    (EnumCommitmentStatus.OPEN, False): "still-open",
    (EnumCommitmentStatus.REGRESSED, False): "still-regressed",
}


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract; none is spelled here."""
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["topics"])


def _token(text: str) -> str:
    return _TOKEN.sub("-", text).strip("-")[:80] or "none"


class HandlerCommitmentCheckRunner:
    """Run every due commitment's check and write the reading each one earns."""

    def __init__(
        self,
        *,
        rules: ModelCommitmentCheckRules,
        commitment_rules: ModelCommitmentRules,
        source: ProtocolLedgerRowSource,
        appender: ProtocolReadingAppender,
        executor: ProtocolCheckExecutor,
        clock: Callable[[], datetime],
        run_budget_seconds: int | None = None,
    ) -> None:
        self._rules = rules
        self._commitment_rules = commitment_rules
        self._source = source
        self._appender = appender
        self._executor = executor
        self._clock = clock
        # The whole-run deadline of the service that runs the handler; None means the caller sets none.
        self._run_budget_seconds = run_budget_seconds

    def _passed(self, execution: ModelCheckExecution) -> bool:
        return execution.passed

    def _next_status(
        self, before: EnumCommitmentStatus, passed: bool
    ) -> EnumCommitmentStatus:
        if passed:
            return EnumCommitmentStatus.VERIFIED
        if before is EnumCommitmentStatus.VERIFIED:
            return EnumCommitmentStatus.REGRESSED
        return before

    def _due(
        self, commitments: list[ModelCommitment], now: datetime
    ) -> list[ModelCommitment]:
        """Every commitment whose check is due, in the order this run takes them.

        Failed open and regressed checks append no row, so a fixed oldest-first order would hand the same few
        commitments every slot of every run. The order is a hash of the commitment id and the run's second: stateless,
        different on each run, so every due commitment is reached across runs.
        """
        interval = timedelta(seconds=self._rules.recheck_interval_seconds)
        due = [
            c
            for c in commitments
            if c.status is not EnumCommitmentStatus.WITHDRAWN
            and (
                c.status is not EnumCommitmentStatus.VERIFIED
                or c.last_checked_at is None
                or now - c.last_checked_at >= interval
            )
        ]
        second = int(now.timestamp())
        return sorted(
            due,
            key=lambda c: hashlib.sha256(
                f"{c.commitment_id}|{second}".encode()
            ).hexdigest(),
        )

    def handle(
        self, command: ModelCommitmentCheckCommand
    ) -> ModelCommitmentCheckResult:
        now = self._clock()
        read = self._source.read(
            ModelLedgerQuery(
                since=now - timedelta(hours=self._rules.ledger_lookback_hours),
                until=now + timedelta(days=1),
                row_type=self._commitment_rules.row_type,
                term=f"{self._commitment_rules.kind_cell}={self._commitment_rules.kind_value}",
            )
        )
        if read.error is not None:
            return ModelCommitmentCheckResult(
                correlation_id=command.correlation_id,
                status="failed",
                failure=f"ledger-unreadable:{_token(read.error)}",
                ran_at=now,
            )
        commitments, malformed = fold_commitments(read.rows, self._commitment_rules)
        due = self._due(commitments, now)
        outcomes: list[ModelCommitmentCheckOutcome] = []
        started = 0
        deferred = 0
        for position, commitment in enumerate(due):
            # A refused check is never executed, so it does not use one of the run's check slots.
            if started >= self._rules.max_checks_per_run or self._past_budget(now):
                deferred = len(due) - position
                break
            outcome = self._check_one(commitment, now, command.correlation_id)
            outcomes.append(outcome)
            started += 1 if outcome.ran else 0
        return ModelCommitmentCheckResult(
            correlation_id=command.correlation_id,
            status="completed",
            ran_at=now,
            outcomes=tuple(outcomes),
            malformed=tuple(malformed),
            deferred=deferred,
        )

    def _past_budget(self, began: datetime) -> bool:
        """Whether another check could outlive the service deadline: it waits for the next run instead."""
        if self._run_budget_seconds is None:
            return False
        elapsed = (self._clock() - began).total_seconds()
        return elapsed + self._rules.check_timeout_seconds > self._run_budget_seconds

    def _check_one(
        self, commitment: ModelCommitment, now: datetime, correlation_id: str
    ) -> ModelCommitmentCheckOutcome:
        before = commitment.status

        def outcome(
            result: str,
            *,
            ran: bool,
            exit_code: int | None = None,
            after: EnumCommitmentStatus | None = None,
            row: str | None = None,
        ) -> ModelCommitmentCheckOutcome:
            return ModelCommitmentCheckOutcome(
                commitment_id=commitment.commitment_id,
                status_before=before,
                status_after=after or before,
                ran=ran,
                exit_code=exit_code,
                result=result,
                row=row,
            )

        try:
            argv = shlex.split(commitment.check)
        except ValueError:
            argv = []
        if not argv:
            return outcome("refused:no-argv", ran=False)
        if argv[0] not in self._rules.allowed_executables:
            return outcome("refused:executable-not-allowed", ran=False)
        execution = self._executor.run(argv, self._rules.check_timeout_seconds)
        passed = self._passed(execution)
        after = self._next_status(before, passed)
        result = _RESULT_BY_TRANSITION[(before, passed)]
        if after is before and before is not EnumCommitmentStatus.VERIFIED:
            return outcome(result, ran=True, exit_code=execution.exit_code)
        row = result_row(
            self._commitment_rules,
            now,
            self._rules.runner_lane,
            commitment.commitment_id,
            after,
            execution.exit_code if execution.exit_code is not None else -1,
        )
        appended = self._appender.append(
            row, f"commitment-reading:{commitment.commitment_id}:{now.isoformat()}"
        )
        if not appended.ok:
            why = f":{_token(appended.message)}" if appended.message else ""
            return outcome(
                f"append-failed:{appended.status}{why}",
                ran=True,
                exit_code=execution.exit_code,
                row=row,
            )
        return outcome(
            result, ran=True, exit_code=execution.exit_code, after=after, row=row
        )


def events_of(
    result: ModelCommitmentCheckResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events one run produces, in order, all under its correlation id.

    A commitment that closed is ``commitment_verified`` and one that reopened is ``commitment_regressed``. A check that
    could not be run or recorded (refused, append failed) is ``commitment_check_failed``. The run ends in
    ``commitment_check_completed``, or in ``commitment_check_run_failed`` when the ledger could not be read.
    """
    if result.status == "failed":
        return [
            (
                topics["commitment_check_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    events: list[tuple[str, dict[str, object]]] = []
    for o in result.outcomes:
        if o.result.startswith(("refused:", "append-failed:")):
            role = "commitment_check_failed"
        elif o.result in ("verified", "reverified"):
            role = "commitment_verified"
        elif o.result == "regressed":
            role = "commitment_regressed"
        else:
            continue
        events.append(
            (
                topics[role],
                {
                    "correlation_id": result.correlation_id,
                    "commitment_id": o.commitment_id,
                    "result": o.result,
                    "exit_code": o.exit_code,
                },
            )
        )
    events.append(
        (
            topics["commitment_check_completed"],
            {
                "correlation_id": result.correlation_id,
                "checked": sum(1 for o in result.outcomes if o.ran),
                "refused": sum(1 for o in result.outcomes if not o.ran),
                "malformed": len(result.malformed),
            },
        )
    )
    return events


def answer_command(
    payload: bytes, handler: HandlerCommitmentCheckRunner, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a check command is answered with a typed ``commitment_check_run_failed`` event, never dropped.
    """
    try:
        command = ModelCommitmentCheckCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["commitment_check_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerCommitmentCheckRunner",
    "answer_command",
    "events_of",
    "load_topics",
]
