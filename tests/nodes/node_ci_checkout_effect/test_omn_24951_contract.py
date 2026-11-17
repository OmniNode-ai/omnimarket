# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket contract of the ci-checkout serve-only change binds its criteria to a reading that can fail.

The first cut of ``contracts/OMN-24951.yaml`` asked for a ledger row for a request "answered on the dev
runtime on h201 itself", a reading the change makes impossible (the dev runtime wires no consumer), and bound
AC2 and AC3 only to a pytest run that stays green when a unit is hand-edited. These tests read the contract
file and fail on that shape.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-24951.yaml"
PYTEST_CHECK = re.compile(r"\bpytest\b")


def _load() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _acceptance(contract: dict[str, Any]) -> dict[str, str]:
    return {
        a["id"]: a["statement"]
        for req in contract["requirements"]
        for a in req["acceptance"]
    }


def _evidence_for(contract: dict[str, Any], ac: str) -> list[dict[str, Any]]:
    return [e for e in contract["dod_evidence"] if ac in e.get("binds_ac", [])]


def _ledger_checks(contract: dict[str, Any], ac: str) -> list[str]:
    return [
        c["check_value"]
        for e in _evidence_for(contract, ac)
        if e.get("execution_scope") == "local_done_gate"
        for c in e["checks"]
        if c["check_type"] == "command"
        and re.search(r"\$[A-Z_]*LEDGER", c["check_value"])
    ]


def _run_ledger_check(check: str, ledger: Path) -> int:
    """Run one contract check against a scratch ledger, handing it the path under the variable the check names."""
    named = re.search(r"\$\{?([A-Z_]+)\}?", check)
    assert named is not None, check
    return subprocess.run(
        ["bash", "-c", check],
        env={named.group(1): str(ledger), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    ).returncode


GOOD_ROW = (
    "2026-11-17T16:00:00Z | STATUS | lane=probe | pr=omnimarket#6062 | installed=h051:abc1234 | "
    "observed=ci-checkout h051 OmniNode-ai/omnimarket@9c8f6b3c3757 status=served | "
    "read_at=2026-11-17T16:00:00Z | "
    "receipt=installed onex-ci-checkout-serve.service on h051 | text"
)


def test_omn_24951_contract_ac1_names_the_h051_serve_not_the_h201_runtime() -> None:
    ac1 = _acceptance(_load())["AC1"]
    assert "installed=h051:" in ac1
    assert "on h051 itself" in ac1
    # The reading the change makes impossible: a request answered by the dev runtime on h201.
    assert "dev runtime on h201 itself" not in ac1
    assert "installed=<host>" not in ac1


def test_omn_24951_contract_ac1_ledger_check_fails_without_the_row_and_passes_with_it(
    tmp_path: Path,
) -> None:
    checks = _ledger_checks(_load(), "AC1")
    assert len(checks) == 1
    ledger = tmp_path / "ledger.md"
    ledger.write_text("2026-11-17T15:00:00Z | STATUS | lane=other | text\n")
    assert _run_ledger_check(checks[0], ledger) != 0
    # A row from another host does not satisfy the per-host serve.
    ledger.write_text(GOOD_ROW.replace("installed=h051:", "installed=h201:") + "\n")
    assert _run_ledger_check(checks[0], ledger) != 0
    ledger.write_text(GOOD_ROW + "\n")
    assert _run_ledger_check(checks[0], ledger) == 0


def test_omn_24951_contract_ac2_ledger_check_wants_an_answer_or_a_cause_and_an_actor(
    tmp_path: Path,
) -> None:
    checks = _ledger_checks(_load(), "AC2")
    assert len(checks) == 1
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-11-17T16:00:00Z | STATUS | lane=probe | pr=omnimarket#6062 | text\n"
    )
    assert _run_ledger_check(checks[0], ledger) != 0
    ledger.write_text(GOOD_ROW + "\n")
    assert _run_ledger_check(checks[0], ledger) == 0
    ledger.write_text(
        "2026-11-17T16:00:00Z | STATUS | lane=probe | pr=omnimarket#6062 | "
        "cause=the dev deploy agent has not rolled the merge | actor=dev deploy agent | text\n"
    )
    assert _run_ledger_check(checks[0], ledger) == 0


def test_omn_24951_contract_ac3_receipt_check_fails_for_a_row_that_cites_no_receipt(
    tmp_path: Path,
) -> None:
    checks = _ledger_checks(_load(), "AC3")
    assert len(checks) == 1
    ledger = tmp_path / "ledger.md"
    without_receipt = GOOD_ROW.replace(
        "receipt=installed onex-ci-checkout-serve.service on h051", "text"
    )
    ledger.write_text(without_receipt + "\n")
    assert _run_ledger_check(checks[0], ledger) != 0
    ledger.write_text(GOOD_ROW + "\n")
    assert _run_ledger_check(checks[0], ledger) == 0


@pytest.mark.parametrize("ac", ["AC1", "AC2", "AC3"])
def test_omn_24951_contract_every_criterion_has_a_ledger_or_receipt_check_beside_its_pytest_proxy(
    ac: str,
) -> None:
    contract = _load()
    evidence = _evidence_for(contract, ac)
    scoped = [e for e in evidence if e.get("execution_scope") == "local_done_gate"]
    hosted = [e for e in evidence if e.get("execution_scope") != "local_done_gate"]
    assert scoped, f"{ac} is bound only to hosted checks"
    assert _ledger_checks(contract, ac), f"{ac} has no ledger check"
    # The pytest proxy stays for hosted CI, but it is never the only binding.
    assert all(
        PYTEST_CHECK.search(c["check_value"])
        for e in hosted
        for c in e["checks"]
        if c["check_type"] == "test_passes"
    )
