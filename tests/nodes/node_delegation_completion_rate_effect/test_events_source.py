# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ssh and postgres reader of delegation_events (OMN-23108): what it asks, what it parses, how it fails."""

from __future__ import annotations

import subprocess

import pytest

from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.events_source import (
    SshPostgresEventSource,
    parse_rows,
)
from tests.nodes.node_delegation_completion_rate_effect.rate_run_support import (
    FakeRunner,
    answer,
    db_row,
    effect_overlay,
)

pytestmark = pytest.mark.unit


def source(
    runner: FakeRunner, env: dict[str, str] | None = None
) -> SshPostgresEventSource:
    return SshPostgresEventSource(
        effect_overlay().events_source,
        {"TEST_RUNTIME_SSH_HOST": "user@host.example"} if env is None else env,
        runner,
    )


def test_it_reads_the_window_in_a_read_only_transaction_in_the_overlays_container() -> (
    None
):
    runner = FakeRunner(answer([db_row(), db_row(backend="harness:claude-glm")]))
    read = source(runner).read(3600)
    assert read.error is None
    assert len(read.events) == 2
    assert read.now is not None
    assert read.now.isoformat() == "2026-11-08T07:00:00+00:00"
    ((argv, script, timeout),) = runner.calls
    assert argv[-2:] == ["user@host.example", "bash -s"]
    assert "BatchMode=yes" in argv
    assert timeout == 30
    assert "BEGIN READ ONLY;" in script
    assert "com.docker.compose.project=test-project" in script
    assert "-U test_user -d test_analytics" in script
    assert "interval '3600 seconds'" in script
    for write_word in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER"):
        assert write_word not in script.upper()


def test_a_null_column_reads_as_no_value_never_as_a_pass() -> None:
    row = db_row(ok=None, gate=None, backend=None)
    read = parse_rows(answer([row]))
    (event,) = read.events
    assert (event.terminal_ok, event.quality_gate_passed, event.answering_backend) == (
        None,
        None,
        None,
    )


def test_failing_control_an_unset_host_variable_is_a_typed_error_and_runs_nothing() -> (
    None
):
    runner = FakeRunner(answer([]))
    read = source(runner, env={}).read(3600)
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
            FakeRunner(answer([{"t": "2026-11-08T06:30:00.000000+00:00"}])),
            "rows-answer-malformed",
        ),
        (FakeRunner(answer([db_row(t="yesterday")])), "rows-answer-malformed"),
        (
            FakeRunner(answer([db_row(t="2026-11-08T06:30:00")])),
            "rows-answer-malformed",
        ),
        (FakeRunner(answer([], now="2026-11-08T07:00:00")), "rows-clock-without-zone"),
    ],
)
def test_failing_control_every_unusable_answer_is_a_typed_error_never_empty_rows(
    runner: FakeRunner, error: str
) -> None:
    read = source(runner).read(3600)
    assert read.error == error
    assert read.events == ()
