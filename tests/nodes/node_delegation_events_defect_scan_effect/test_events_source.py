# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ssh and postgres reader of delegation_events for the defect scan (OMN-23331): what it asks, what it parses, how it fails."""

from __future__ import annotations

import subprocess
from decimal import Decimal

import pytest

from omnimarket.nodes.node_delegation_events_defect_scan_effect.adapters.events_source import (
    SshPostgresDefectEventSource,
    parse_rows,
)
from tests.nodes.node_delegation_events_defect_scan_effect.defect_scan_run_support import (
    FakeRunner,
    answer,
    db_row,
    effect_overlay,
)

pytestmark = pytest.mark.unit


def source(
    runner: FakeRunner, env: dict[str, str] | None = None
) -> SshPostgresDefectEventSource:
    return SshPostgresDefectEventSource(
        effect_overlay().events_source,
        {"TEST_RUNTIME_SSH_HOST": "user@host.example"} if env is None else env,
        runner,
    )


def test_delegation_events_defect_scan_it_reads_the_day_in_a_read_only_transaction() -> (
    None
):
    runner = FakeRunner(answer([db_row(), db_row(lane=None)]))
    read = source(runner).read(86400)
    assert read.error is None
    assert len(read.events) == 2
    assert read.now is not None
    assert read.now.isoformat() == "2026-11-09T06:15:00+00:00"
    ((argv, script, timeout),) = runner.calls
    assert argv[-2:] == ["user@host.example", "bash -s"]
    assert "BatchMode=yes" in argv
    assert timeout == 30
    assert "BEGIN READ ONLY;" in script
    assert "com.docker.compose.project=test-project" in script
    assert "-U test_user -d test_analytics" in script
    assert "interval '86400 seconds'" in script
    for column in ("cost_usd", "cost_measurement_source", "caller_lane", "terminal_ok"):
        assert column in script
    for leaked in ("prompt_text", "response_text", "correlation_id"):
        assert leaked not in script
    for write_word in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER"):
        assert write_word not in script.upper()


def test_delegation_events_defect_scan_a_null_column_reads_as_no_value_never_as_a_pass() -> (
    None
):
    read = parse_rows(answer([db_row(ok=None, cost=None, cms=None, lane=None)]))
    (event,) = read.events
    assert event.terminal_ok is None
    assert event.cost_usd is None
    assert event.cost_measurement_source is None
    assert event.caller_lane is None


def test_delegation_events_defect_scan_a_zero_cost_keeps_its_exact_value() -> None:
    (event,) = parse_rows(answer([db_row(cost="0.000000")])).events
    assert event.cost_usd == Decimal(0)


def test_failing_control_an_unset_host_variable_is_a_typed_error_and_runs_nothing() -> (
    None
):
    runner = FakeRunner(answer([]))
    read = source(runner, env={}).read(86400)
    assert read.error == "ssh-host-variable-unset"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("runner", "error"),
    [
        (FakeRunner(returncode=255), "rows-read-exit-255"),
        (FakeRunner(raises=subprocess.TimeoutExpired("ssh", 30)), "rows-read-timeout"),
        (
            FakeRunner(raises=FileNotFoundError("ssh")),
            "rows-read-unstartable-FileNotFoundError",
        ),
        (FakeRunner(""), "rows-answer-empty"),
        (FakeRunner("not json\n"), "rows-answer-malformed"),
        (
            FakeRunner(answer([{"t": "2026-11-09T04:00:00.000000+00:00"}])),
            "rows-answer-malformed",
        ),
        (FakeRunner(answer([db_row(t="yesterday")])), "rows-answer-malformed"),
        (FakeRunner(answer([db_row(cost="free")])), "rows-answer-malformed"),
        (
            FakeRunner(answer([db_row(t="2026-11-09T04:00:00")])),
            "rows-answer-malformed",
        ),
        (FakeRunner(answer([], now="2026-11-09T06:15:00")), "rows-clock-without-zone"),
    ],
)
def test_failing_control_every_unusable_answer_is_a_typed_error_never_empty_rows(
    runner: FakeRunner, error: str
) -> None:
    read = source(runner).read(86400)
    assert read.error == error
    assert read.events == ()
