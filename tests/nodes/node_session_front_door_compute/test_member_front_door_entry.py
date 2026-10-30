# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session-start entry on a member's bus-attached machine, and the operator's, unchanged.

A session whose credential-bound ledger principal the team identity declaration does not positively
match as the operator's runs the preflight with ``--profile team``; only a positive operator match
runs ``--profile operator``. The host mode is decided from the receipt before the first CLAIM, a
refused or unknown CLAIM append stops startup with nothing written, and ``session.env`` records the
session's principal and profile.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.session_front_door_machine import (
    MEMBER_A,
    OPERATOR,
    Machine,
    operator_environment,
)

pytestmark = pytest.mark.unit

UNMATCHED = {
    "unknown-broker-login": [{"source": "broker_binding", "value": "nobody-dev"}],
    "environment-only-operator": [
        {"source": "environment", "value": "fixture-lab-login"}
    ],
    "no-identity": [],
    "member-a": MEMBER_A,
}


def _profile_of(call: list[str]) -> str:
    assert call.count("--profile") == 1, call
    return call[call.index("--profile") + 1]


@pytest.mark.parametrize("who", sorted(UNMATCHED))
def test_a_session_not_matched_as_operator_runs_the_team_profile(
    machine: Machine, who: str
) -> None:
    result = machine.start(UNMATCHED[who])
    assert result.profile == "team"
    assert result.scope.privileges == "team"
    calls = machine.preflight_calls()
    assert len(calls) == 1
    assert _profile_of(calls[0]) == "team"
    assert _profile_of(list(result.preflight_argv)) == "team"


def test_a_missing_identity_declaration_runs_the_team_profile(machine: Machine) -> None:
    shutil.rmtree(machine.roots / "team_identity")
    result = machine.start(OPERATOR)
    assert result.profile == "team"
    assert _profile_of(machine.preflight_calls()[0]) == "team"


def test_member_session_starts_as_bus_member_under_its_own_principal(
    machine: Machine,
) -> None:
    result = machine.start(MEMBER_A)
    assert result.status == "ok", (result.rule, result.message)
    assert result.exit_code == 0
    assert result.host_mode is not None
    assert result.host_mode.mode == "bus-member"
    assert result.assertion is not None
    assert result.assertion.ok
    env = machine.session_env()
    assert env["HOST_MODE"] == "bus-member"
    assert env["SESSION_PROFILE"] == "team"
    assert env["SESSION_PRINCIPAL"] == "fixture-member-a-dev"
    assert env["CLAIM_TS"] == "2026-10-29T10:00:00Z"
    # The first row lands on the member's mirror under the member's principal.
    claims = [c for c in machine.ledger_calls() if c["argv"][:2] == ["row", "CLAIM"]]
    assert len(claims) == 1
    assert claims[0]["principal"] == "fixture-member-a-dev"
    assert "principal=fixture-member-a-dev" in machine.mirror.read_text()
    assert "lane=sess-front" not in machine.registry_ledger.read_text()
    assert result.lines
    assert "host=bus-member" in result.lines[-1]


def test_host_mode_is_decided_before_the_claim(machine: Machine) -> None:
    machine.environment["FAKE_LEDGER_ROW"] = "refuse"
    result = machine.start(MEMBER_A)
    # The verdict was reached from the receipt before any append was attempted.
    assert result.host_mode is not None
    assert result.host_mode.mode == "bus-member"
    assert result.claim_ts is None


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


def test_unbound_team_session_records_no_principal(machine: Machine) -> None:
    result = machine.start([{"source": "environment", "value": "fixture-member-a-dev"}])
    assert result.status == "ok"
    env = machine.session_env()
    assert "SESSION_PRINCIPAL" not in env
    assert env["SESSION_PROFILE"] == "team"
    # No bound lane login: never bus-member.
    assert env["HOST_MODE"] == "detached"


def test_entry_requires_a_ledger_command(machine: Machine) -> None:
    result = machine.start(MEMBER_A, ledger_command=None)
    assert result.status == "refused"
    assert result.rule == "ledger-command"


def test_entry_resolves_the_installed_onex_preflight(
    machine: Machine, tmp_path: Path
) -> None:
    import json
    import sys

    plugin = tmp_path / "plugins" / "onex"
    (plugin / "scripts").mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "session_front_door"
        / "fake_session_preflight.py",
        plugin / "scripts" / "session_preflight.py",
    )
    registry = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"plugins": {"onex@omninode-tools": [{"installPath": str(plugin)}]}})
    )
    result = machine.start(MEMBER_A, preflight_script=None, python=sys.executable)
    assert result.status == "ok", (result.rule, result.message)
    assert str(plugin / "scripts" / "session_preflight.py") in result.preflight_argv
