# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the session-start entry stops, a member machine without the bus, and the operator's entry.

A refused or unknown CLAIM append, a preflight blocker, a taken lane or a missing ledger command stops
startup with nothing written. A member machine with no bus writer starts ``detached``, never
``bus-member``. The operator's session runs ``--profile operator`` with ``coordination_host``
required, and reads its host mode exactly as before.
"""

from __future__ import annotations

import pytest

from tests.session_front_door_machine import (
    MEMBER_A,
    OPERATOR,
    Machine,
    operator_environment,
)

pytestmark = pytest.mark.unit


def _profile_of(call: list[str]) -> str:
    assert call.count("--profile") == 1, call
    return call[call.index("--profile") + 1]


@pytest.mark.parametrize(
    ("mode", "rule"), [("refuse", "append-refused"), ("silent", "append-unknown")]
)
def test_a_refused_or_unknown_append_stops_startup(
    machine: Machine, mode: str, rule: str
) -> None:
    machine.environment["FAKE_LEDGER_ROW"] = mode
    result = machine.start(MEMBER_A)
    assert result.status == "refused"
    assert result.rule == rule
    assert result.exit_code != 0
    assert result.session_env is None
    assert not (
        machine.omni_home
        / ".claude_scratch"
        / "sessions"
        / "sess-front"
        / "session.env"
    ).exists()


def test_a_preflight_blocker_stops_startup_before_any_ledger_call(
    machine: Machine,
) -> None:
    (machine.mirror.parent / "archive").rmdir()
    result = machine.start(MEMBER_A)
    assert result.status == "blocked"
    assert result.exit_code == 1
    assert result.rule == "ledger_is_registry_ledger"
    assert result.fix
    assert machine.ledger_calls() == []


def test_a_taken_lane_is_refused_before_preflight(machine: Machine) -> None:
    folder = machine.omni_home / ".claude_scratch" / "sessions" / "sess-front"
    folder.mkdir(parents=True)
    (folder / "session.env").write_text("SESSION_LANE=sess-front\n")
    result = machine.start(MEMBER_A)
    assert result.status == "refused"
    assert result.exit_code == 3
    assert machine.preflight_calls() == []


def test_member_without_a_bus_writer_starts_detached_not_bus_member(
    machine: Machine,
) -> None:
    # The registry ledger copy on a member machine with no bus configured: preflight passes (the
    # registry path), but nothing attaches it to the bus, so it is never bus-member.
    env = machine.environment
    for key in (
        "ONEX_LEDGER_WRITE_VIA",
        "ONEX_LEDGER_HOST_NAME",
        "ONEX_LEDGER_BUS_APPEND_COMMAND",
    ):
        env.pop(key)
    env["ONEX_LEDGER_PATH"] = str(machine.registry_ledger)
    result = machine.start(MEMBER_A)
    assert result.status == "ok"
    assert result.host_mode is not None
    assert result.host_mode.mode == "detached"
    assert machine.session_env()["HOST_MODE"] == "detached"


def test_operator_session_runs_the_operator_profile_as_today(machine: Machine) -> None:
    machine.environment = operator_environment(machine)
    result = machine.start(OPERATOR)
    assert result.status == "ok", (result.rule, result.message)
    assert result.profile == "operator"
    assert _profile_of(machine.preflight_calls()[0]) == "operator"
    assert result.assertion is not None
    assert "coordination_host" in result.assertion.required
    assert result.host_mode is not None
    assert result.host_mode.mode == "coordination"
    env = machine.session_env()
    assert env["HOST_MODE"] == "coordination"
    assert env["SESSION_PROFILE"] == "operator"
    assert env["SESSION_PRINCIPAL"] == "fixture-lab-login"


def test_operator_session_without_drain_rows_is_detached_as_today(
    machine: Machine,
) -> None:
    machine.environment = operator_environment(machine)
    machine.registry_ledger.write_text(
        "2026-10-29T08:00:00Z | STATUS | lane=quiet | x |\n"
    )
    result = machine.start(OPERATOR)
    assert result.status == "ok"
    assert result.host_mode is not None
    assert result.host_mode.mode == "detached"


def test_entry_requires_a_ledger_command(machine: Machine) -> None:
    result = machine.start(MEMBER_A, ledger_command=None)
    assert result.status == "refused"
    assert result.rule == "ledger-command"
