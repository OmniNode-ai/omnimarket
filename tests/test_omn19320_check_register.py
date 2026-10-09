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

from omnimarket.delegation.content_grounding import (
    resolve_claim_grounding_policy,
    resolve_numeric_grounding_policy,
)
from omnimarket.delegation.identifier_grounding import (
    resolve_identifier_grounding_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _HEURISTIC_SIMPLE_CHECKS,
    _UNEVALUATED_DETERMINISTIC_CHECKS,
    SUPPORTED_DETERMINISTIC_CHECKS,
)
from omnimarket.ranges import (
    DEFAULT_CHECK_REGISTER_PATH,
    FALSE_PASS_ID_PREFIX,
    FALSE_REFUSAL_ID_PREFIX,
    GATE_ID_PREFIX,
    REQUIRED_FALSE_PASS_CLASSES,
    REQUIRED_GATE_CHECKS,
    required_check_ids,
    required_sample_size,
    validate_check_register,
)

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
        committed = yaml.safe_load(
            DEFAULT_CHECK_REGISTER_PATH.read_text(encoding="utf-8")
        )
        committed["checks"].extend(document["checks"])
        completed = _cli(tmp_path, committed)
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


class TestRequiredDelegationAcceptanceEntries:
    def test_every_missing_required_entry_is_named(self, tmp_path: Path) -> None:
        present_id = GATE_ID_PREFIX + "accurate"
        document = _register(_gate(present_id))
        required = required_check_ids()
        errors = validate_check_register(document, required=required)
        assert errors == [
            f"{check_id}: required delegation-acceptance check has no register entry"
            for check_id in required
            if check_id != present_id
        ]

        completed = _cli(tmp_path, document)
        assert completed.returncode != 0
        output = completed.stdout + completed.stderr
        assert "delegation.acceptance.false_pass.test" in output
        assert "delegation.acceptance.false_refusal.test" in output
        assert "delegation.acceptance.gate.numbers_grounded" in output

    @pytest.mark.parametrize("prefix", [FALSE_PASS_ID_PREFIX, FALSE_REFUSAL_ID_PREFIX])
    @pytest.mark.parametrize("class_name", REQUIRED_FALSE_PASS_CLASSES)
    def test_a_required_false_pass_range_may_not_be_not_set(
        self, prefix: str, class_name: str
    ) -> None:
        check_id = prefix + class_name
        document = _register(
            {
                "check_id": check_id,
                "check_class": "range",
                "range_status": "not_set",
            }
        )
        assert validate_check_register(document) == []
        errors = validate_check_register(document, required=(check_id,))
        assert any(check_id in error and "declared range" in error for error in errors)

    def test_a_required_gate_may_not_be_a_range(self) -> None:
        check_id = GATE_ID_PREFIX + "accurate"
        document = _register(
            {
                "check_id": check_id,
                "check_class": "range",
                "range_status": "not_set",
            }
        )
        assert validate_check_register(document) == []
        errors = validate_check_register(document, required=(check_id,))
        assert any(check_id in error and "must be a gate" in error for error in errors)

    @pytest.mark.parametrize("prefix", [FALSE_PASS_ID_PREFIX, FALSE_REFUSAL_ID_PREFIX])
    def test_a_required_false_pass_entry_may_not_be_a_gate(self, prefix: str) -> None:
        check_id = prefix + "test"
        document = _register(_gate(check_id))
        assert validate_check_register(document) == []
        errors = validate_check_register(document, required=(check_id,))
        assert any(check_id in error and "declared range" in error for error in errors)

    def test_required_ids_are_gates_then_false_pass_ranges(self) -> None:
        assert required_check_ids() == tuple(
            GATE_ID_PREFIX + name for name in REQUIRED_GATE_CHECKS
        ) + tuple(
            prefix + name
            for prefix in (FALSE_PASS_ID_PREFIX, FALSE_REFUSAL_ID_PREFIX)
            for name in REQUIRED_FALSE_PASS_CLASSES
        )
        assert len(required_check_ids()) == 49

    def test_the_committed_register_passes_with_all_required_entries(self) -> None:
        document = yaml.safe_load(
            DEFAULT_CHECK_REGISTER_PATH.read_text(encoding="utf-8")
        )
        assert validate_check_register(document, required=required_check_ids()) == []

    def test_the_committed_false_pass_ranges_have_the_full_declared_line(self) -> None:
        document = yaml.safe_load(
            DEFAULT_CHECK_REGISTER_PATH.read_text(encoding="utf-8")
        )
        entries = {
            entry["check_id"]: entry
            for entry in document["checks"]
            if entry["check_id"].startswith(FALSE_PASS_ID_PREFIX)
        }
        assert set(entries) == {
            FALSE_PASS_ID_PREFIX + name for name in REQUIRED_FALSE_PASS_CLASSES
        }
        for check_id, entry in entries.items():
            assert entry["check_class"] == "range", check_id
            assert entry["range_status"] == "declared", check_id
            line = entry["acceptance_line"]
            assert line["check_id"] == check_id
            assert line["floor"] == 0.90, check_id
            assert line["window"] == "trailing 30 days", check_id
            assert line["method"] == {
                "sample_size": 100,
                "confidence": 0.95,
                "power": 0.8,
                "margin": 0.07,
                "incomplete_run_treatment": "count_as_failure",
            }, check_id

    def test_the_committed_false_refusal_ranges_have_the_full_declared_line(
        self,
    ) -> None:
        document = yaml.safe_load(
            DEFAULT_CHECK_REGISTER_PATH.read_text(encoding="utf-8")
        )
        entries = {
            entry["check_id"]: entry
            for entry in document["checks"]
            if entry["check_id"].startswith(FALSE_REFUSAL_ID_PREFIX)
        }
        assert set(entries) == {
            FALSE_REFUSAL_ID_PREFIX + name for name in REQUIRED_FALSE_PASS_CLASSES
        }
        for check_id, entry in entries.items():
            assert entry["check_class"] == "range", check_id
            assert entry["range_status"] == "declared", check_id
            line = entry["acceptance_line"]
            assert line["check_id"] == check_id
            assert line["floor"] == 0.80, check_id
            assert line["window"] == "trailing 30 days", check_id
            assert "refused delegation gate decisions" in line["case_set"], check_id
            assert "excluding ruled no-target refusals" in line["case_set"], check_id
            assert entry["blocks"] == [
                f"routing read of the {check_id.removeprefix(FALSE_REFUSAL_ID_PREFIX)} acceptance eval"
            ]
            assert line["method"] == {
                "sample_size": required_sample_size(
                    floor=0.80, confidence=0.95, power=0.8, margin=0.12
                ),
                "confidence": 0.95,
                "power": 0.8,
                "margin": 0.12,
                "incomplete_run_treatment": "count_as_failure",
            }, check_id

    @pytest.mark.parametrize("check_id", required_check_ids())
    def test_removing_any_required_entry_is_refused(self, check_id: str) -> None:
        document = yaml.safe_load(
            DEFAULT_CHECK_REGISTER_PATH.read_text(encoding="utf-8")
        )
        document["checks"] = [
            entry for entry in document["checks"] if entry["check_id"] != check_id
        ]
        assert validate_check_register(document, required=required_check_ids()) == [
            f"{check_id}: required delegation-acceptance check has no register entry"
        ]

    def test_underpowered_acceptance_ranges_fail_naming_every_check(
        self, tmp_path: Path
    ) -> None:
        """Synthetic mutant of the register; no lab content enters the fixture."""
        document = yaml.safe_load(
            DEFAULT_CHECK_REGISTER_PATH.read_text(encoding="utf-8")
        )
        expected_errors = []
        for entry in document["checks"]:
            check_id = entry["check_id"]
            if not check_id.startswith((FALSE_PASS_ID_PREFIX, FALSE_REFUSAL_ID_PREFIX)):
                continue
            line = entry["acceptance_line"]
            method = line["method"]
            required_n = required_sample_size(
                floor=line["floor"],
                margin=method["margin"],
                confidence=method["confidence"],
                power=method["power"],
            )
            method["sample_size"] = required_n - 1
            expected_errors.append(
                f"{check_id}: declared n={required_n - 1} was not sized by "
                f"the power analysis: required n={required_n}"
            )
        assert len(expected_errors) == 14
        assert validate_check_register(document, required=required_check_ids()) == (
            expected_errors
        )
        completed = _cli(tmp_path, document)
        assert completed.returncode != 0
        output = completed.stdout + completed.stderr
        for error in expected_errors:
            assert error in output


class TestRequiredDelegationAcceptanceInventory:
    def test_gate_checks_match_the_quality_gate_inventory(self) -> None:
        gate_checks = (
            set(SUPPORTED_DETERMINISTIC_CHECKS)
            | set(_UNEVALUATED_DETERMINISTIC_CHECKS)
            | set(_HEURISTIC_SIMPLE_CHECKS)
            | set(_HEURISTIC_CONTAINS_ANY_CHECKS)
            | {
                resolve_numeric_grounding_policy().check_name,
                resolve_claim_grounding_policy().check_name,
                resolve_identifier_grounding_policy().check_name,
            }
        )
        assert gate_checks
        assert len(gate_checks) == 35
        assert tuple(sorted(gate_checks)) == REQUIRED_GATE_CHECKS

    def test_false_pass_classes_match_the_seven_acceptance_classes(self) -> None:
        assert list(REQUIRED_FALSE_PASS_CLASSES) == [
            "code_generation",
            "document",
            "planning",
            "reasoning",
            "research",
            "summarization",
            "test",
        ]
