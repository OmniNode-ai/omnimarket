# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence checks of the OMN-24829 and OMN-23518 contracts for omnimarket PR 5737.

The ledger reads of ``contracts/OMN-24829.yaml`` are scoped ``local_done_gate``: the
ledger is not on the hosted runner, and a bound ``local_done_gate`` item is refused by
the verifier. So AC3 is bound to a check that runs those same ledger commands, as the
contract spells them, against ledgers this test builds: an empty ledger must be refused,
and so must a ledger whose rows do not match. The criterion is then bound to a real
STATUS-row read, not to the contract's own text.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_THIS = "tests/contracts/test_omn24829_contract_evidence.py"


def _contract(ticket: str) -> dict[str, Any]:
    text = (_REPO / "contracts" / f"{ticket}.yaml").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _items_binding(contract: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [i for i in contract["dod_evidence"] if label in i.get("binds_ac", [])]


def _ledger_commands(contract: dict[str, Any]) -> list[str]:
    """Every check the contract scopes local_done_gate that reads STATUS rows from a ledger file."""
    return [
        c["check_value"]
        for item in contract["dod_evidence"]
        if item.get("execution_scope") == "local_done_gate"
        for c in item["checks"]
        if "| STATUS |" in c["check_value"]
    ]


def _path_variable(command: str) -> str:
    """The variable a ledger command reads the ledger file path from, taken from the command."""
    named = re.search(r'"\$([A-Z][A-Z0-9_]*)"', command)
    assert named is not None, command
    return named.group(1)


def _run(command: str, ledger: Path) -> int:
    return subprocess.run(
        ["bash", "-c", command],
        cwd=_REPO,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            _path_variable(command): str(ledger),
        },
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def _status_row(*, pr: str, check: str, shared: str) -> str:
    return (
        "2026-11-16T19:03:41Z | STATUS | lane=fixture | ticket=OMN-24960 | "
        f"pr={pr} | check={check} | shared={shared} | fixture row\n"
    )


def test_omn24829_ledger_status_commands_refuse_an_empty_ledger(tmp_path: Path) -> None:
    commands = _ledger_commands(_contract("OMN-24829"))
    assert commands, "the contract carries no ledger read"
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    for command in commands:
        assert _run(command, empty) != 0, command


def test_omn24829_ledger_status_commands_read_the_matching_row(tmp_path: Path) -> None:
    for command in _ledger_commands(_contract("OMN-24829")):
        named = re.search(r"check=([a-z-]+)", command)
        assert named is not None, command
        check = named.group(1)
        hit = tmp_path / "hit.md"
        hit.write_text(
            _status_row(pr="omnimarket#5737", check=check, shared="yes"),
            encoding="utf-8",
        )
        assert _run(command, hit) == 0, command
        for label, row in {
            "another pull request": _status_row(
                pr="omnimarket#5738", check=check, shared="yes"
            ),
            "not shared": _status_row(pr="omnimarket#5737", check=check, shared="no"),
            "another check": _status_row(
                pr="omnimarket#5737", check="lint", shared="yes"
            ),
        }.items():
            miss = tmp_path / "miss.md"
            miss.write_text(row, encoding="utf-8")
            assert _run(command, miss) != 0, (label, command)


def test_omn24829_ac3_bound_checks_exercise_the_ledger_commands() -> None:
    """AC3's own bound check values run the ledger tests, and none of them is a floors check."""
    values = [
        c["check_value"]
        for item in _items_binding(_contract("OMN-24829"), "AC3")
        for c in item["checks"]
    ]
    this_module = sys.modules[__name__]
    exercised = [v for v in values if _THIS in v and "-k" in v and "ledger_status" in v]
    assert exercised, values
    assert any(
        "ledger_status" in name for name in dir(this_module) if name.startswith("test_")
    )
    assert not [v for v in values if "omnibase-spi>=" in v or "floors" in v], values


def test_omn24829_ac1_is_the_green_criterion_and_no_pypi_row_is_shared() -> None:
    """The red this change causes is held, not logged as shared, and AC1 asks for green."""
    contract = _contract("OMN-24829")
    statements = {
        a["id"]: a["statement"]
        for r in contract["requirements"]
        for a in r["acceptance"]
    }
    assert "required checks read green" in statements["AC1"], statements["AC1"]
    assert "pypi-pin-resolvability" in statements["AC1"], statements["AC1"]
    ledger_reads = _ledger_commands(contract)
    assert ledger_reads
    assert not [v for v in ledger_reads if "pypi-pin-resolvability" in v], ledger_reads


def test_omn24829_ac1_evidence_carries_the_ancestry_check() -> None:
    values = [
        c["check_value"]
        for item in _items_binding(_contract("OMN-24829"), "AC1")
        for c in item["checks"]
    ]
    assert "git merge-base --is-ancestor origin/dev HEAD" in values, values


def test_omn23518_contract_claims_only_what_the_notes_and_diff_show() -> None:
    text = (_REPO / "contracts" / "OMN-23518.yaml").read_text(encoding="utf-8")
    for claim in (
        "lock names 0.23.14",
        "moves the omnibase-spi dev merge tag",
        "re-pinned",
        "first to carry",
    ):
        assert claim not in text, claim
    assert "v0.23.15 release notes" in text
    assert "CHANGELOG v0.23.15" in (_REPO / "pyproject.toml").read_text(
        encoding="utf-8"
    )
