# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host mode and the kept required-check assertion, for a member's team receipt and the operator's.

``bus-member`` is printed only when the current team-profile receipt passes the assertion and the bus
configuration holds: the bound lane login, a declared host, an executable append command, and a
verified ledger mirror with its archive. It is a startup verdict, never attachment proof. The
assertion (once ``LEDGER_CHECKS``) requires, under ``team``, the four ledger checks without
``coordination_host`` and validates profile, overlay hash, per-check success and the overall verdict;
under ``operator`` it requires all five as today.
"""

from __future__ import annotations

import os
import time

import pytest

from tests.session_front_door_machine import (
    MEMBER_A,
    OPERATOR,
    OPERATOR_REQUIRED,
    TEAM_REQUIRED,
    Machine,
    operator_environment,
)

pytestmark = pytest.mark.unit


def _host_mode(
    machine: Machine, claims: list[dict[str, str]], receipt: object
) -> tuple[str, tuple[str, ...]]:
    result = machine.run("host_mode", claims, receipt=str(receipt))
    assert result.status == "ok", result.message
    assert result.host_mode is not None
    assert result.lines == (result.host_mode.mode,)
    return result.host_mode.mode, result.host_mode.reasons


# --------------------------------------------------------------------------- (b) bus-member


def test_member_with_bus_and_current_team_receipt_is_bus_member(
    machine: Machine,
) -> None:
    mode, reasons = _host_mode(machine, MEMBER_A, machine.receipt("team"))
    assert mode == "bus-member", reasons
    assert reasons == ()


BROKEN_BUS = {
    "no-declared-host": ("ONEX_LEDGER_HOST_NAME", None),
    "append-command-not-executable": (
        "ONEX_LEDGER_BUS_APPEND_COMMAND",
        "no-such-append-binary --x",
    ),
    "append-command-empty": ("ONEX_LEDGER_BUS_APPEND_COMMAND", None),
    "transport-not-bus": ("ONEX_LEDGER_WRITE_VIA", "file"),
    "transport-unset": ("ONEX_LEDGER_WRITE_VIA", None),
    "principal-is-someone-else": ("ONEX_WORK_LEDGER_PRINCIPAL", "fixture-member-b-dev"),
    "no-ledger-path": ("ONEX_LEDGER_PATH", None),
}


@pytest.mark.parametrize("case", sorted(BROKEN_BUS))
def test_each_bus_condition_is_required(machine: Machine, case: str) -> None:
    key, value = BROKEN_BUS[case]
    receipt = machine.receipt("team")
    if value is None:
        machine.environment.pop(key)
    else:
        machine.environment[key] = value
    mode, reasons = _host_mode(machine, MEMBER_A, receipt)
    assert mode != "bus-member"
    assert reasons


def test_no_bound_lane_login_is_never_bus_member(machine: Machine) -> None:
    github_only = [{"source": "github_auth", "value": "fixture-member-a-gh"}]
    mode, reasons = _host_mode(machine, github_only, machine.receipt("team"))
    assert mode != "bus-member"
    assert any("bound lane login" in r for r in reasons)


def test_a_bound_login_of_another_entry_is_never_bus_member(machine: Machine) -> None:
    mixed = [
        {"source": "github_auth", "value": "fixture-member-a-gh"},
        {"source": "broker_binding", "value": "fixture-member-b-dev"},
    ]
    mode, _ = _host_mode(machine, mixed, machine.receipt("team"))
    assert mode != "bus-member"


@pytest.mark.parametrize(
    "overrides",
    [
        {"failed": ("ledger_is_registry_ledger",)},
        {"dropped": ("ledger_grammar",)},
        {"verdict": "BLOCKED"},
        {"profile": "operator"},
    ],
    ids=["failed-check", "stale-overlay", "verdict-blocked", "operator-receipt"],
)
def test_a_failing_team_receipt_is_never_bus_member(
    machine: Machine, overrides: dict[str, object]
) -> None:
    mode, reasons = _host_mode(machine, MEMBER_A, machine.receipt("team", **overrides))
    assert mode != "bus-member"
    assert reasons


def test_an_old_receipt_is_not_current(machine: Machine) -> None:
    receipt = machine.receipt("team")
    old = time.time() - 2 * 3600
    os.utime(receipt, (old, old))
    mode, reasons = _host_mode(machine, MEMBER_A, receipt)
    assert mode != "bus-member"
    assert any("current" in r for r in reasons)


def test_an_unreadable_receipt_is_refused(machine: Machine) -> None:
    result = machine.run(
        "host_mode", MEMBER_A, receipt=str(machine.root / "missing.json")
    )
    assert result.status == "refused"
    assert result.exit_code == 2


# --------------------------------------------------------------------------- (d) the member mirror


def test_member_mirror_outside_the_registry_path_with_remote_lane_unset_passes(
    machine: Machine,
) -> None:
    assert "ONEX_REMOTE_LANE" not in machine.environment
    assert machine.mirror.resolve() != machine.registry_ledger.resolve()
    run = machine.start(MEMBER_A)
    assert run.status == "ok", (run.rule, run.message)
    assert run.assertion is not None
    assert run.assertion.ok
    assert "ledger_is_registry_ledger" in run.assertion.ran
    assert run.host_mode is not None
    assert run.host_mode.mode == "bus-member"


def test_member_mirror_with_a_bad_transport_fails(machine: Machine) -> None:
    machine.environment["ONEX_LEDGER_WRITE_VIA"] = "file"
    run = machine.start(MEMBER_A)
    assert run.status == "blocked"
    assert run.rule == "ledger_is_registry_ledger"


def test_member_mirror_without_its_archive_fails(machine: Machine) -> None:
    (machine.mirror.parent / "archive").rmdir()
    mode, reasons = _host_mode(machine, MEMBER_A, machine.receipt("team"))
    assert mode != "bus-member"
    assert any("archive" in r for r in reasons)
    assert machine.start(MEMBER_A).status == "blocked"


# --------------------------------------------------------------------------- (c) the assertion


def _assert(
    machine: Machine, claims: list[dict[str, str]], receipt: object, **fields: object
) -> tuple[bool, tuple[str, ...]]:
    result = machine.run("assert_receipt", claims, receipt=str(receipt), **fields)
    assert result.assertion is not None
    assert result.status == ("ok" if result.assertion.ok else "refused")
    return result.assertion.ok, result.assertion.findings


def test_team_assertion_requires_the_four_ledger_checks_without_coordination_host(
    machine: Machine,
) -> None:
    result = machine.run(
        "assert_receipt", MEMBER_A, receipt=str(machine.receipt("team"))
    )
    assert result.assertion is not None
    assert result.assertion.ok, result.assertion
    assert result.assertion.profile == "team"
    assert set(result.assertion.required) == set(TEAM_REQUIRED)
    assert "coordination_host" not in result.assertion.required
    assert "coordination_host" not in result.assertion.ran


@pytest.mark.parametrize("check_id", TEAM_REQUIRED)
def test_team_assertion_rejects_a_stale_overlay(
    machine: Machine, check_id: str
) -> None:
    ok, findings = _assert(
        machine, MEMBER_A, machine.receipt("team", dropped=(check_id,))
    )
    assert not ok
    assert any(check_id in f for f in findings)


@pytest.mark.parametrize("check_id", TEAM_REQUIRED)
def test_team_assertion_requires_each_check_to_pass(
    machine: Machine, check_id: str
) -> None:
    ok, findings = _assert(
        machine, MEMBER_A, machine.receipt("team", failed=(check_id,))
    )
    assert not ok
    assert any(check_id in f for f in findings)


def test_team_assertion_requires_overall_ok(machine: Machine) -> None:
    ok, findings = _assert(
        machine, MEMBER_A, machine.receipt("team", verdict="BLOCKED")
    )
    assert not ok
    assert any("verdict" in f for f in findings)


@pytest.mark.parametrize("profile", ["operator", None])
def test_team_assertion_validates_the_profile(
    machine: Machine, profile: str | None
) -> None:
    ok, findings = _assert(machine, MEMBER_A, machine.receipt("team", profile=profile))
    assert not ok
    assert any("profile" in f for f in findings)


def test_team_assertion_rejects_an_overlay_changed_since_the_run(
    machine: Machine,
) -> None:
    receipt = machine.receipt("team")
    machine.overlay.write_text(machine.overlay.read_text() + "# edited after the run\n")
    ok, findings = _assert(machine, MEMBER_A, receipt)
    assert not ok
    assert any("overlay" in f for f in findings)


@pytest.mark.parametrize("digest", ["", "not-a-hash"])
def test_team_assertion_rejects_a_missing_overlay_hash(
    machine: Machine, digest: str
) -> None:
    ok, findings = _assert(
        machine, MEMBER_A, machine.receipt("team", overlay_sha256=digest)
    )
    assert not ok
    assert any("overlay" in f for f in findings)


def test_team_assertion_checks_the_expected_overlay_hash(machine: Machine) -> None:
    ok, findings = _assert(
        machine, MEMBER_A, machine.receipt("team"), expected_overlay_sha256="0" * 64
    )
    assert not ok
    assert any("overlay" in f for f in findings)


# --------------------------------------------------------------------------- (e) the operator, unchanged


def test_operator_assertion_still_requires_coordination_host(machine: Machine) -> None:
    machine.environment = operator_environment(machine)
    result = machine.run(
        "assert_receipt", OPERATOR, receipt=str(machine.receipt("operator"))
    )
    assert result.assertion is not None
    assert result.assertion.ok
    assert set(result.assertion.required) == set(OPERATOR_REQUIRED)
    ok, findings = _assert(
        machine, OPERATOR, machine.receipt("operator", dropped=("coordination_host",))
    )
    assert not ok
    assert any("coordination_host" in f for f in findings)


def test_operator_assertion_reads_a_receipt_from_before_profiles(
    machine: Machine,
) -> None:
    machine.environment = operator_environment(machine)
    legacy = machine.receipt("operator", profile=None)
    ok, _ = _assert(machine, OPERATOR, legacy)
    assert ok


def test_operator_assertion_refuses_a_team_receipt(machine: Machine) -> None:
    ok, findings = _assert(machine, OPERATOR, machine.receipt("team"))
    assert not ok
    assert any("profile" in f for f in findings)


def test_operator_assertion_only_asks_that_the_checks_ran_as_today(
    machine: Machine,
) -> None:
    # A failed coordination_host is a warning: the operator's assertion asks that it ran.
    receipt = machine.receipt("operator", failed=("coordination_host",))
    ok, _ = _assert(machine, OPERATOR, receipt)
    assert ok


@pytest.mark.parametrize(
    ("failed", "expected"),
    [((), "coordination"), (("coordination_host",), "detached")],
)
def test_operator_host_mode_is_unchanged(
    machine: Machine, failed: tuple[str, ...], expected: str
) -> None:
    machine.environment = operator_environment(machine)
    mode, _ = _host_mode(machine, OPERATOR, machine.receipt("operator", failed=failed))
    assert mode == expected


def test_operator_host_mode_without_coordination_host_is_unknown(
    machine: Machine,
) -> None:
    machine.environment = operator_environment(machine)
    receipt = machine.receipt("operator", dropped=("coordination_host",))
    mode, _ = _host_mode(machine, OPERATOR, receipt)
    assert mode == "unknown"


def test_operator_is_never_bus_member(machine: Machine) -> None:
    # Even on a fully configured bus machine the operator's verdict comes from coordination_host.
    mode, _ = _host_mode(machine, OPERATOR, machine.receipt("operator"))
    assert mode in ("coordination", "detached")
