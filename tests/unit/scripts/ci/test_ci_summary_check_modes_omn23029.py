# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each external check declares advisory or blocking, as overlay data (OMN-23029).

Operator ruling 2026-11-08: a ``neutral`` conclusion from a check declared advisory does
not fail CI Summary and its findings still show; a blocking check's ``neutral`` still
fails; an undeclared check is still denied by default. The declaration is data, the
contract ``ci_summary_external_check_modes.contract.json`` plus the overlay
``ci_summary_external_check_modes.json``, so adding an advisory reviewer is one overlay
line and no code change. This replaces the named, expiring code exception that admitted
``Tessl Reviewer`` alone.

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
    contract_file, overlay_file = tmp_path / "c.json", tmp_path / "o.json"
    contract_file.write_text(json.dumps(contract), encoding="utf-8")
    overlay_file.write_text(json.dumps(overlay), encoding="utf-8")
    monkeypatch.setattr(gate, "CHECK_MODES_CONTRACT_FILE", contract_file)
    monkeypatch.setattr(gate, "CHECK_MODES_OVERLAY_FILE", overlay_file)


def _entry(mode: str) -> dict[str, str]:
    return {"mode": mode, "reason": "declared for this test"}


def test_shipped_overlay_declares_the_reviewer_advisory_and_the_sweep_passes_it() -> (
    None
):
    admitted, refusals = gate.load_check_modes(
        gate.CHECK_MODES_CONTRACT_FILE, gate.CHECK_MODES_OVERLAY_FILE
    )
    assert refusals == []
    assert admitted[TESSL] == frozenset({"neutral"})
    assert TESSL not in gate.EXTERNAL_SWEEP_EXCLUSIONS
    assert TESSL not in gate.EXPECTED_EXTERNAL_CONTEXTS
    rows = [_run(TESSL, "neutral"), _run("Some Other Gate", "success")]
    code, report = gate.evaluate_external_sweep(rows, now=NOW)
    assert code == EXIT_SUCCESS, report
    assert (
        f"advisory findings (declared advisory, not blocking): {TESSL} (neutral)"
        in report
    )


def test_declared_advisory_neutral_passes_and_still_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {"New Reviewer": _entry("advisory")})
    code, report = gate.evaluate_external_sweep(
        [_run("New Reviewer", "neutral")], now=NOW
    )
    assert code == EXIT_SUCCESS, report
    assert "New Reviewer (neutral)" in report


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
        ({"X": {"mode": "advisory"}}, "X: missing or non-text reason"),
        ({"X": {"mode": "advisory", "reason": "  "}}, "X: missing or non-text reason"),
        ({"X": {"mode": "advisory", "reason": 7}}, "X: missing or non-text reason"),
        ({"X": {"mode": "optional", "reason": "r"}}, "mode 'optional' is not declared"),
        ({"X": {"mode": ["advisory"], "reason": "r"}}, "missing or non-text mode"),
        ({"X": "advisory"}, "X: missing or non-text mode, reason"),
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
    assert "check-mode registry REFUSED" in report
    assert needle in report


def test_control_a_missing_overlay_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {})
    monkeypatch.setattr(gate, "CHECK_MODES_OVERLAY_FILE", tmp_path / "absent.json")
    code, report = gate.evaluate_external_sweep(
        [_run("Some Other Gate", "success")], now=NOW
    )
    assert code == EXIT_FAILURE, report
    assert "check-mode registry REFUSED" in report


def test_control_a_missing_contract_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {})
    monkeypatch.setattr(gate, "CHECK_MODES_CONTRACT_FILE", tmp_path / "absent.json")
    code, report = gate.evaluate_external_sweep([], now=NOW)
    assert code == EXIT_FAILURE, report
    assert "check-mode registry REFUSED" in report


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
    assert "check-mode registry REFUSED" in report


@pytest.mark.parametrize("raw", ["{not json", "[]", "null"])
def test_control_an_unparsable_overlay_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _declare(tmp_path, monkeypatch, {})
    (tmp_path / "o.json").write_text(raw, encoding="utf-8")
    code, report = gate.evaluate_external_sweep([], now=NOW)
    assert code == EXIT_FAILURE, report
    assert "check-mode registry REFUSED" in report


def test_control_the_reviewer_neutral_fails_when_the_overlay_does_not_declare_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch, {})
    code, report = gate.evaluate_external_sweep([_run(TESSL, "neutral")], now=NOW)
    assert code == EXIT_FAILURE, report
    assert f"{TESSL} (neutral)" in report
