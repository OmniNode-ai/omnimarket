# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check runner closes a commitment when its check passes and reopens it when the check later fails (OMN-23013 AC1).

Every test runs the handler over an in-memory ledger and an in-memory executor that answers each check from a table.
The failing controls: a check that exits non-zero never verifies, a check outside the allowlist never executes, and the
named injected breaks (a runner that reads any exit as a pass, one that never reopens) turn the same assertions red.
"""

from __future__ import annotations

import sys
from datetime import timedelta

import pytest

from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    InMemoryCheckExecutor,
    SubprocessCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.handler_commitment_check_runner import (
    HandlerCommitmentCheckRunner,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCheckExecution,
    ModelCommitmentCheckCommand,
)
from omnimarket.nodes.node_commitment_ledger_compute.handlers.commitment_rows import (
    fold_commitments,
)
from omnimarket.nodes.node_commitment_ledger_compute.models import (
    EnumCommitmentStatus,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_commitment_check_runner_effect.support import (
    CHECK,
    RUN_AT,
    commitment_id,
    ledger_rules,
    open_commitment_line,
    reading_line,
    runner_rules,
)

pytestmark = pytest.mark.unit

PASS = ModelCheckExecution(exit_code=0)
FAIL = ModelCheckExecution(exit_code=1, output_tail="expected nvme-root, read sata")


def _run(
    lines: list[str],
    executor: InMemoryCheckExecutor,
    *,
    handler_type: type[HandlerCommitmentCheckRunner] = HandlerCommitmentCheckRunner,
    **rule_changes: object,
):
    ledger = InMemoryLedger(lines)
    handler = handler_type(
        rules=runner_rules(**rule_changes),
        commitment_rules=ledger_rules(),
        source=ledger,
        appender=ledger,
        executor=executor,
        clock=lambda: RUN_AT,
    )
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="run-1"))
    return result, ledger


def _status_in(ledger: InMemoryLedger) -> EnumCommitmentStatus:
    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        evidence_row_from_text,
    )

    rows = tuple(r for line in ledger.lines if (r := evidence_row_from_text(line)))
    commitments, malformed = fold_commitments(rows, ledger_rules())
    assert malformed == []
    (commitment,) = commitments
    return commitment.status


def test_open_commitment_whose_check_passes_is_verified() -> None:
    executor = InMemoryCheckExecutor({CHECK: PASS})
    result, ledger = _run([open_commitment_line()], executor)
    assert result.status == "completed"
    (outcome,) = result.outcomes
    assert outcome.result == "verified"
    assert outcome.exit_code == 0
    assert executor.calls == [tuple(CHECK.split())]
    assert _status_in(ledger) is EnumCommitmentStatus.VERIFIED
    # The reading row was appended as a plain STATUS row the grammar admits.
    assert len(ledger.lines) == 2
    assert "status=verified" in ledger.lines[1]


def test_failing_control_open_commitment_whose_check_fails_stays_open() -> None:
    executor = InMemoryCheckExecutor({CHECK: FAIL})
    result, ledger = _run([open_commitment_line()], executor)
    (outcome,) = result.outcomes
    assert outcome.result == "still-open"
    assert outcome.exit_code == 1
    assert len(ledger.lines) == 1, "a failing check writes no reading row"
    assert _status_in(ledger) is EnumCommitmentStatus.OPEN


def test_a_check_that_timed_out_did_not_pass() -> None:
    executor = InMemoryCheckExecutor(
        {CHECK: ModelCheckExecution(exit_code=None, timed_out=True)}
    )
    result, ledger = _run([open_commitment_line()], executor)
    assert result.outcomes[0].result == "still-open"
    assert _status_in(ledger) is EnumCommitmentStatus.OPEN


def test_verified_commitment_whose_check_later_fails_is_regressed() -> None:
    lines = [open_commitment_line(), reading_line("verified", 0, minutes_ago=600)]
    executor = InMemoryCheckExecutor({CHECK: FAIL})
    result, ledger = _run(lines, executor)
    (outcome,) = result.outcomes
    assert outcome.result == "regressed"
    assert outcome.status_before is EnumCommitmentStatus.VERIFIED
    assert _status_in(ledger) is EnumCommitmentStatus.REGRESSED


def test_regressed_commitment_whose_check_passes_again_is_verified() -> None:
    lines = [
        open_commitment_line(),
        reading_line("verified", 0, minutes_ago=900),
        reading_line("regressed", 1, minutes_ago=60),
    ]
    executor = InMemoryCheckExecutor({CHECK: PASS})
    result, ledger = _run(lines, executor)
    assert result.outcomes[0].result == "reverified"
    assert _status_in(ledger) is EnumCommitmentStatus.VERIFIED


def test_verified_commitment_inside_the_recheck_interval_is_not_run() -> None:
    lines = [open_commitment_line(), reading_line("verified", 0, minutes_ago=30)]
    executor = InMemoryCheckExecutor({CHECK: FAIL})
    result, ledger = _run(lines, executor)
    assert result.outcomes == ()
    assert executor.calls == []
    assert len(ledger.lines) == 2


def test_verified_commitment_past_the_interval_and_still_passing_gets_a_fresh_reading() -> (
    None
):
    lines = [open_commitment_line(), reading_line("verified", 0, minutes_ago=600)]
    executor = InMemoryCheckExecutor({CHECK: PASS})
    result, ledger = _run(lines, executor)
    assert result.outcomes[0].result == "refreshed"
    assert len(ledger.lines) == 3


def test_a_withdrawn_commitment_is_never_checked() -> None:
    cid = commitment_id()
    withdrawn = (
        f"{(RUN_AT - timedelta(minutes=30)):%Y-%m-%dT%H:%M:%SZ} | STATUS | lane=me | kind=commitment | "
        f"commitment={cid} | status=withdrawn | the ask was withdrawn"
    )
    executor = InMemoryCheckExecutor({CHECK: PASS})
    result, ledger = _run([open_commitment_line(), withdrawn], executor)
    assert result.outcomes == ()
    assert executor.calls == []
    assert len(ledger.lines) == 2


def test_a_check_outside_the_allowlist_is_refused_and_never_executed() -> None:
    line = open_commitment_line(check="rm -rf /tmp/x")
    executor = InMemoryCheckExecutor({"rm -rf /tmp/x": PASS})
    result, ledger = _run([line], executor)
    (outcome,) = result.outcomes
    assert outcome.result == "refused:executable-not-allowed"
    assert outcome.ran is False
    assert executor.calls == []
    assert len(ledger.lines) == 1


def test_widening_the_allowlist_is_the_overlays_decision_not_the_handlers() -> None:
    line = open_commitment_line(check="rm -rf /tmp/x")
    executor = InMemoryCheckExecutor({"rm -rf /tmp/x": PASS})
    result, _ = _run([line], executor, allowed_executables=["rm"])
    assert result.outcomes[0].result == "verified"


def test_a_ledger_that_cannot_be_read_fails_the_run_and_runs_no_check() -> None:
    ledger = InMemoryLedger([open_commitment_line()])
    ledger.read_error = "ledger-route-down"
    executor = InMemoryCheckExecutor({CHECK: PASS})
    handler = HandlerCommitmentCheckRunner(
        rules=runner_rules(),
        commitment_rules=ledger_rules(),
        source=ledger,
        appender=ledger,
        executor=executor,
        clock=lambda: RUN_AT,
    )
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="run-1"))
    assert result.status == "failed"
    assert result.failure == "ledger-unreadable:ledger-route-down"
    assert executor.calls == []


def test_a_refused_append_is_not_reported_as_closed() -> None:
    ledger = InMemoryLedger([open_commitment_line()])
    ledger.append_status = "refused"
    executor = InMemoryCheckExecutor({CHECK: PASS})
    handler = HandlerCommitmentCheckRunner(
        rules=runner_rules(),
        commitment_rules=ledger_rules(),
        source=ledger,
        appender=ledger,
        executor=executor,
        clock=lambda: RUN_AT,
    )
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="run-1"))
    (outcome,) = result.outcomes
    assert outcome.result.startswith("append-failed:refused")
    assert outcome.status_after is EnumCommitmentStatus.OPEN
    assert len(ledger.lines) == 1


def test_one_run_starts_at_most_the_overlays_cap_of_checks() -> None:
    lines = [open_commitment_line(minutes_ago=m) for m in (300, 240, 180)]
    executor = InMemoryCheckExecutor({CHECK: PASS})
    result, _ = _run(lines, executor, max_checks_per_run=2)
    assert len(result.outcomes) == 2
    assert len(executor.calls) == 2
    # The oldest ask goes first.
    assert result.outcomes[0].commitment_id == commitment_id(300)


def test_malformed_commitment_rows_are_carried_in_the_result() -> None:
    bad = '2026-11-07T14:00:00Z | STATUS | lane=x | kind=commitment | commitment=2026-11-07T14:00:00Z-x | status=open | owner=y | ask="no check" | t'
    result, _ = _run([bad], InMemoryCheckExecutor())
    assert result.outcomes == ()
    assert len(result.malformed) == 1


def test_the_same_run_twice_writes_one_row() -> None:
    ledger = InMemoryLedger([open_commitment_line()])
    executor = InMemoryCheckExecutor({CHECK: PASS})
    command = ModelCommitmentCheckCommand(correlation_id="run-1")
    for _ in range(2):
        HandlerCommitmentCheckRunner(
            rules=runner_rules(),
            commitment_rules=ledger_rules(),
            source=ledger,
            appender=ledger,
            executor=executor,
            clock=lambda: RUN_AT,
        ).handle(command)
    assert sum("status=verified" in line for line in ledger.lines) == 1


class _AlwaysPasses(HandlerCommitmentCheckRunner):
    """Injected break: any check, whatever its exit, reads as a pass."""

    def _passed(self, execution: ModelCheckExecution) -> bool:
        return True


class _NeverReopens(HandlerCommitmentCheckRunner):
    """Injected break: a failing check never moves a verified commitment."""

    def _next_status(self, before, passed):  # type: ignore[no-untyped-def]
        if before is EnumCommitmentStatus.VERIFIED and not passed:
            return before
        return super()._next_status(before, passed)


def test_injected_break_always_passes_verifies_a_failing_check() -> None:
    executor = InMemoryCheckExecutor({CHECK: FAIL})
    result, ledger = _run(
        [open_commitment_line()], executor, handler_type=_AlwaysPasses
    )
    assert result.outcomes[0].result == "verified"
    with pytest.raises(AssertionError):
        # The control assertion of test_failing_control_... turns red under the break.
        assert _status_in(ledger) is EnumCommitmentStatus.OPEN


def test_injected_break_never_reopens_hides_a_regression() -> None:
    lines = [open_commitment_line(), reading_line("verified", 0, minutes_ago=600)]
    executor = InMemoryCheckExecutor({CHECK: FAIL})
    _, ledger = _run(lines, executor, handler_type=_NeverReopens)
    with pytest.raises(AssertionError):
        assert _status_in(ledger) is EnumCommitmentStatus.REGRESSED


def test_the_real_subprocess_executor_reads_real_exit_codes() -> None:
    py = sys.executable
    for code, expected in (
        (0, EnumCommitmentStatus.VERIFIED),
        (3, EnumCommitmentStatus.OPEN),
    ):
        check = f'{py} -c "raise SystemExit({code})"'
        ledger = InMemoryLedger([open_commitment_line(check=check)])
        handler = HandlerCommitmentCheckRunner(
            rules=runner_rules(allowed_executables=[py]),
            commitment_rules=ledger_rules(),
            source=ledger,
            appender=ledger,
            executor=SubprocessCheckExecutor(output_tail_chars=200),
            clock=lambda: RUN_AT,
        )
        result = handler.handle(ModelCommitmentCheckCommand(correlation_id="real"))
        assert result.outcomes[0].exit_code == code
        assert _status_in(ledger) is expected


def test_the_real_subprocess_executor_times_out_a_slow_check() -> None:
    py = sys.executable
    check = f'{py} -c "import time; time.sleep(30)"'
    ledger = InMemoryLedger([open_commitment_line(check=check)])
    handler = HandlerCommitmentCheckRunner(
        rules=runner_rules(allowed_executables=[py], check_timeout_seconds=1),
        commitment_rules=ledger_rules(),
        source=ledger,
        appender=ledger,
        executor=SubprocessCheckExecutor(output_tail_chars=200),
        clock=lambda: RUN_AT,
    )
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="slow"))
    assert result.outcomes[0].result == "still-open"
    assert result.outcomes[0].exit_code is None
