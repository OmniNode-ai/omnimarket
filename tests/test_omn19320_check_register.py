# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check register's class field (OMN-19320 AC1, unified plan row G1).

Every check in the register declares its class, gate or range (section 2b), and
a range carries either a full acceptance line or an explicit NOT SET status. A
check with no class fails the register's check, which names it. The committed
register is itself checked here, so the check runs in CI as well as in the
pre-commit hook: a gate that ran only on a laptop would be a silent gate.

The register check aggregates over the entries it discovers, so its known-bad
half carries the zero-member class (section 2d): an empty register is refused
rather than reported clean.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.ranges import DEFAULT_CHECK_REGISTER_PATH, validate_check_register

pytestmark = pytest.mark.unit

_VALID_LINE: dict[str, object] = {
    "check_id": "example.range",
    "case_set": "every example case",
    "floor": 0.8,
    "window": "the last 88 runs",
    "method": {
        "sample_size": 88,
        "confidence": 0.95,
        "power": 0.8,
        "margin": 0.1,
        "incomplete_run_treatment": "count_as_failure",
    },
}


def _register(*checks: dict[str, object]) -> dict[str, object]:
    return {"schema_version": "check_register.v1", "checks": list(checks)}


def _gate(check_id: str = "example.gate") -> dict[str, object]:
    return {"check_id": check_id, "check_class": "gate", "blocks": ["a surface"]}


def _cli(
    tmp_path: Path, document: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "check_register.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "omnimarket.ranges", "check-register", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestEveryCheckCarriesItsClassAC1:
    def test_a_check_with_no_class_fails_naming_it(self, tmp_path: Path) -> None:
        document = _register(_gate(), {"check_id": "unclassified.check"})
        errors = validate_check_register(document)
        assert any("unclassified.check" in error for error in errors), errors

        completed = _cli(tmp_path, document)
        assert completed.returncode != 0
        assert "unclassified.check" in completed.stdout + completed.stderr

    def test_an_unknown_class_fails_naming_it(self) -> None:
        document = _register({"check_id": "odd.check", "check_class": "advisory"})
        errors = validate_check_register(document)
        assert any("odd.check" in error for error in errors), errors

    def test_a_classified_register_passes(self, tmp_path: Path) -> None:
        document = _register(
            _gate(),
            {
                "check_id": "example.not_set",
                "check_class": "range",
                "range_status": "not_set",
            },
            {
                "check_id": "example.range",
                "check_class": "range",
                "range_status": "declared",
                "acceptance_line": _VALID_LINE,
            },
        )
        assert validate_check_register(document) == []
        completed = _cli(tmp_path, document)
        assert completed.returncode == 0, completed.stdout + completed.stderr


class TestARangeCarriesItsLineAndMethod:
    def test_a_range_with_neither_line_nor_status_fails(self) -> None:
        errors = validate_check_register(
            _register({"check_id": "bare.range", "check_class": "range"})
        )
        assert any("bare.range" in error for error in errors), errors

    def test_a_declared_range_missing_a_method_part_fails(self) -> None:
        line = {**_VALID_LINE, "method": dict(_VALID_LINE["method"])}  # type: ignore[call-overload]
        del line["method"]["power"]  # type: ignore[attr-defined]
        errors = validate_check_register(
            _register(
                {
                    "check_id": "example.range",
                    "check_class": "range",
                    "range_status": "declared",
                    "acceptance_line": line,
                }
            )
        )
        assert any("example.range" in e and "power" in e for e in errors), errors

    def test_a_declared_range_whose_n_was_not_power_analysed_fails(self) -> None:
        line = {**_VALID_LINE, "method": {**_VALID_LINE["method"], "sample_size": 20}}  # type: ignore[dict-item]
        errors = validate_check_register(
            _register(
                {
                    "check_id": "example.range",
                    "check_class": "range",
                    "range_status": "declared",
                    "acceptance_line": line,
                }
            )
        )
        assert any("required n=88" in error for error in errors), errors

    def test_a_gate_may_not_carry_a_range_line(self) -> None:
        errors = validate_check_register(
            _register({**_gate(), "acceptance_line": _VALID_LINE})
        )
        assert any("example.gate" in error for error in errors), errors


class TestTheAggregateRefusesItsZeroMemberCases:
    def test_an_empty_register_is_refused(self, tmp_path: Path) -> None:
        errors = validate_check_register(_register())
        assert errors
        completed = _cli(tmp_path, _register())
        assert completed.returncode != 0

    def test_a_duplicate_check_id_is_refused(self) -> None:
        errors = validate_check_register(_register(_gate("same"), _gate("same")))
        assert any("same" in error for error in errors), errors

    def test_an_entry_with_no_id_is_refused(self) -> None:
        errors = validate_check_register(_register({"check_class": "gate"}))
        assert errors

    def test_a_wrong_schema_version_is_refused(self) -> None:
        errors = validate_check_register(
            {"schema_version": "check_register.v0", "checks": [_gate()]}
        )
        assert errors


class TestTheCommittedRegister:
    def test_the_committed_register_passes_its_own_check(self) -> None:
        """The CI half of the gate: the real register, not a fixture."""
        document = yaml.safe_load(
            Path(DEFAULT_CHECK_REGISTER_PATH).read_text(encoding="utf-8")
        )
        assert validate_check_register(document) == []

    def test_the_committed_register_is_not_empty(self) -> None:
        """Positive control, so the check above cannot pass on nothing."""
        document = yaml.safe_load(
            Path(DEFAULT_CHECK_REGISTER_PATH).read_text(encoding="utf-8")
        )
        assert len(document["checks"]) >= 5

    def test_the_cli_default_path_is_the_committed_register(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "omnimarket.ranges", "check-register"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
