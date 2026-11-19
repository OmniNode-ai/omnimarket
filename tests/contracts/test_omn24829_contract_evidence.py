# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence checks of the OMN-24829 and OMN-23518 contracts for omnimarket PR 5737.

The ledger read of ``contracts/OMN-24829.yaml`` is scoped ``local_done_gate``: the
ledger is not on the hosted runner, and a bound ``local_done_gate`` item is refused by
the verifier. The contract reads STATUS rows through the ledger query node
(``onex ledger query --stdin``) and asserts on the answer. So AC3 is bound to a check
that runs that same read as the contract spells it: the query the contract prints is
answered by the real query handler over an in-memory ledger this test builds, and the
contract's own assertion is run on that answer. An empty ledger must be refused, and so
must a ledger whose rows do not match. The criterion is then bound to a real STATUS-row
read, not to the contract's own text.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_THIS = "tests/contracts/test_omn24829_contract_evidence.py"
_NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)


def _contract(ticket: str) -> dict[str, Any]:
    text = (_REPO / "contracts" / f"{ticket}.yaml").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _items_binding(contract: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [i for i in contract["dod_evidence"] if label in i.get("binds_ac", [])]


def _ledger_commands(contract: dict[str, Any]) -> list[str]:
    """Every check the contract scopes local_done_gate that reads STATUS rows through the ledger query node."""
    return [
        c["check_value"]
        for item in contract["dod_evidence"]
        if item.get("execution_scope") == "local_done_gate"
        for c in item["checks"]
        if "onex ledger query" in c["check_value"] and "STATUS" in c["check_value"]
    ]


def _query_of(command: str) -> dict[str, Any]:
    """The JSON query the command prints into the query node, with the clock fixed."""
    printed = re.search(r"printf '(\{.*?\})'", command)
    assert printed is not None, command
    query: dict[str, Any] = json.loads(printed.group(1).replace("%s", _NOW.isoformat()))
    return query


def _assertion_of(command: str) -> str:
    """The Python the command pipes the query node's answer into."""
    script = re.search(r"python3 -c '(.*)'\s*$", command, re.DOTALL)
    assert script is not None, command
    return script.group(1)


def _answer(command: str, raw_rows: list[str]) -> str:
    """The query node's answer to the command's own query over a ledger holding ``raw_rows``."""
    reader = InMemoryWorkLedgerQueryReader()
    for number, raw_row in enumerate(raw_rows):
        reader.ingest(
            {"row_id": f"row-{number}", "row_type": "STATUS", "raw_row": raw_row}
        )
    request = ModelWorkLedgerQueryRequest(correlation_id=uuid4(), **_query_of(command))
    result = HandlerWorkLedgerQuery(reader=reader, now=lambda: _NOW).handle(request)
    return result.model_dump_json()


def _run(command: str, raw_rows: list[str]) -> int:
    """The exit code of the contract's own assertion on the query node's answer."""
    return subprocess.run(
        [sys.executable, "-c", _assertion_of(command)],
        input=_answer(command, raw_rows),
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def _status_row(*, pr: str, check: str, shared: str) -> str:
    return (
        "2026-11-16T19:03:41Z | STATUS | lane=fixture | ticket=OMN-24960 | "
        f"pr={pr} | check={check} | shared={shared} | fixture row"
    )


def test_omn24829_ledger_status_commands_refuse_an_empty_ledger() -> None:
    commands = _ledger_commands(_contract("OMN-24829"))
    assert commands, "the contract carries no ledger read"
    for command in commands:
        assert _run(command, []) != 0, command


def test_omn24829_ledger_status_commands_read_the_matching_row() -> None:
    for command in _ledger_commands(_contract("OMN-24829")):
        named = re.search(r"check=([a-z-]+)", command)
        assert named is not None, command
        check = named.group(1)
        pr = _query_of(command)["pr"]
        assert pr == "omnimarket#5737", command
        hit = _status_row(pr=pr, check=check, shared="yes")
        assert _run(command, [hit]) == 0, command
        for label, row in {
            "another pull request": _status_row(
                pr="omnimarket#5738", check=check, shared="yes"
            ),
            "not shared": _status_row(pr=pr, check=check, shared="no"),
            "another check": _status_row(pr=pr, check="lint", shared="yes"),
        }.items():
            assert _run(command, [row]) != 0, (label, command)


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
