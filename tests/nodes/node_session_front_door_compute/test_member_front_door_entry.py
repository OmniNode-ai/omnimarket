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


def test_unbound_team_session_records_no_principal(machine: Machine) -> None:
    result = machine.start([{"source": "environment", "value": "fixture-member-a-dev"}])
    assert result.status == "ok"
    env = machine.session_env()
    assert "SESSION_PRINCIPAL" not in env
    assert env["SESSION_PROFILE"] == "team"
    # No bound lane login: never bus-member.
    assert env["HOST_MODE"] == "detached"


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
