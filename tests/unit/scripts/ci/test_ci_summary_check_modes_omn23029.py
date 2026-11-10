# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each external check declares advisory or blocking, as overlay data (OMN-23029).

Operator ruling 2026-11-08: a ``neutral`` conclusion from a check declared advisory does
not fail CI Summary and its findings still show; a blocking check's ``neutral`` still
fails; an undeclared check is still denied by default. The declaration is data, the
contract ``ci_summary_external_check_modes.contract.json`` plus the overlay
``ci_summary_external_check_modes.json``, so adding an advisory reviewer is one overlay
line and no code change. This replaces the named, expiring code exception that admitted
``Tessl Reviewer`` alone. The node ``node_ci_summary_registry_compute`` resolves the
declaration and the gate reads the resolved registry.

Each control below fails without the change it names: the shipped overlay is what turns
the ``Tessl Reviewer`` neutral green, and an unreadable overlay is what turns it red.

Ticket: OMN-23029
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.ci import ci_summary_gate as gate
from scripts.ci.ci_summary_gate import EXIT_FAILURE, EXIT_SUCCESS
from tests.unit.scripts.ci.registry_support import shipped_request, write_resolved

pytestmark = pytest.mark.unit

TESSL = "Tessl Reviewer"
NOW = datetime(2026, 11, 8, 5, tzinfo=UTC)
CONTRACT = {
    "contract": "ci_summary_external_check_modes",
    "version": 1,
    "modes": {"advisory": ["neutral"], "blocking": []},
    "entry_fields": ["mode", "reason"],
}


def _run(name: str, conclusion: str | None) -> dict[str, object]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-11-08T04:13:29Z",
    }


def _declare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overlay: object,
    contract: object = CONTRACT,
) -> None:
    request = shipped_request(
        check_modes_contract=contract, check_modes_overlay=overlay
    )
    monkeypatch.setattr(
        gate, "REGISTRY_FILE", write_resolved(tmp_path / "resolved.json", request)
    )


def _entry(mode: str) -> dict[str, str]:
    return {"mode": mode, "reason": "declared for this test"}


def test_shipped_overlay_declares_the_reviewer_advisory_and_the_sweep_passes_it() -> (
    None
):
    exclusions, check_modes, refusals = gate.load_resolved_registry(gate.REGISTRY_FILE)
    assert refusals == []
    assert check_modes[TESSL] == frozenset({"neutral"})
    assert TESSL not in exclusions
    assert TESSL not in gate.EXPECTED_EXTERNAL_CONTEXTS
    rows = [_run(TESSL, "neutral"), _run("Some Other Gate", "success")]
    code, report = gate.evaluate_external_sweep(rows, now=NOW)
    assert code == EXIT_SUCCESS, report
    assert f"exclusions applied: {TESSL} (neutral, declared advisory)" in report


def test_declared_advisory_neutral_passes_and_still_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {"New Reviewer": _entry("advisory")})
    code, report = gate.evaluate_external_sweep(
        [_run("New Reviewer", "neutral")], now=NOW
    )
    assert code == EXIT_SUCCESS, report
    assert "New Reviewer (neutral, declared advisory)" in report


def test_control_undeclared_neutral_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {"New Reviewer": _entry("advisory")})
    code, report = gate.evaluate_external_sweep(
        [_run("Unlisted Reviewer", "neutral")], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert "Unlisted Reviewer (neutral)" in report


def test_control_blocking_neutral_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {"Strict Check": _entry("blocking")})
    code, report = gate.evaluate_external_sweep(
        [_run("Strict Check", "neutral")], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert "Strict Check (neutral)" in report


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "action_required"])
def test_control_advisory_failure_is_still_reported_as_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    _declare(tmp_path, monkeypatch, {"New Reviewer": _entry("advisory")})
    code, report = gate.evaluate_external_sweep(
        [_run("New Reviewer", conclusion)], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert f"New Reviewer ({conclusion})" in report


def test_control_advisory_cancelled_is_still_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {"New Reviewer": _entry("advisory")})
    code, report = gate.evaluate_external_sweep(
        [_run("New Reviewer", "cancelled")], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert "New Reviewer" in report


@pytest.mark.parametrize(
    ("overlay", "needle"),
    [
        (
            {"X": {"mode": "advisory"}},
            "X: needs non-empty text mode, reason and a declared mode",
        ),
        (
            {"X": {"mode": "advisory", "reason": "  "}},
            "X: needs non-empty text mode, reason and a declared mode",
        ),
        (
            {"X": {"mode": "advisory", "reason": 7}},
            "X: needs non-empty text mode, reason and a declared mode",
        ),
        (
            {"X": {"mode": "optional", "reason": "r"}},
            "X: needs non-empty text mode, reason and a declared mode",
        ),
        (
            {"X": {"mode": ["advisory"], "reason": "r"}},
            "X: needs non-empty text mode, reason and a declared mode",
        ),
        ({"X": "advisory"}, "X: needs non-empty text mode, reason and a declared mode"),
    ],
)
def test_control_a_malformed_overlay_entry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlay: object, needle: str
) -> None:
    _declare(tmp_path, monkeypatch, overlay)
    code, report = gate.evaluate_external_sweep(
        [_run("Some Other Gate", "success")], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert "CI Summary registry check-modes: " in report
    assert needle in report


def test_control_a_missing_overlay_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = shipped_request(
        check_modes_overlay=None, check_modes_error="o.json: missing"
    )
    monkeypatch.setattr(
        gate, "REGISTRY_FILE", write_resolved(tmp_path / "resolved.json", request)
    )
    code, report = gate.evaluate_external_sweep(
        [_run("Some Other Gate", "success")], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert "CI Summary registry check-modes: " in report


def test_control_a_missing_contract_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = shipped_request(
        check_modes_contract=None, check_modes_error="c.json: missing"
    )
    monkeypatch.setattr(
        gate, "REGISTRY_FILE", write_resolved(tmp_path / "resolved.json", request)
    )
    code, report = gate.evaluate_external_sweep([], now=NOW)
    assert code == EXIT_FAILURE, report
    assert "CI Summary registry check-modes: " in report


@pytest.mark.parametrize(
    "contract",
    [
        {"entry_fields": ["mode"]},
        {"modes": ["advisory"], "entry_fields": ["mode"]},
        {"modes": {"advisory": 5}, "entry_fields": ["mode"]},
        {"modes": {"advisory": ["neutral"]}},
        [],
    ],
)
def test_control_a_malformed_contract_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: object
) -> None:
    _declare(tmp_path, monkeypatch, {}, contract=contract)
    code, report = gate.evaluate_external_sweep([], now=NOW)
    assert code == EXIT_FAILURE, report
    assert "CI Summary registry check-modes: " in report


@pytest.mark.parametrize("raw", ["{not json", "[]", "null"])
def test_control_an_unparsable_overlay_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    if raw == "{not json":
        request = shipped_request(check_modes_error="o.json: not json")
    else:
        request = shipped_request(check_modes_overlay=json.loads(raw))
    monkeypatch.setattr(
        gate, "REGISTRY_FILE", write_resolved(tmp_path / "resolved.json", request)
    )
    code, report = gate.evaluate_external_sweep([], now=NOW)
    assert code == EXIT_FAILURE, report
    assert "CI Summary registry check-modes: " in report


def test_control_the_reviewer_neutral_fails_when_the_overlay_does_not_declare_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {})
    code, report = gate.evaluate_external_sweep([_run(TESSL, "neutral")], now=NOW)
    assert code == EXIT_FAILURE, report
    assert f"{TESSL} (neutral)" in report
