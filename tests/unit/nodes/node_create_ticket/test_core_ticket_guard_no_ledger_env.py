# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The core ticket guard takes its ledger only as an argument, never from the environment."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omnimarket.nodes.node_create_ticket.handlers import core_ticket_guard
from omnimarket.nodes.node_create_ticket.handlers.core_ticket_guard import (
    CoreTicketGuard,
)

_LEDGER_NAME = "ROLLING_WORK_LEDGER.md"
_REMOVED_MODULE = "omnibase_core.cli.cli_work_ledger_render"
_ENV_NAME = "ONEX_LEDGER_PATH"

_CRITERION = (
    "- [ ] AC1: the check works -- falsifier: uv run pytest tests/unit/test_x.py -q\n"
    "- [ ] AC2: integrated: the check runs on the dev lane -- falsifier: one request on the dev lane and its reply read back"
)


def _defect_payload(citation: str) -> dict[str, object]:
    return {
        "title": "Fix the broken check",
        "description": f"Gate: C7\n\nDiagnosis: {citation}\n\n## Acceptance criteria\n{_CRITERION}",
        "parentId": "OMN-22847",
        "state": "Backlog",
    }


def _ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / _LEDGER_NAME
    ledger.write_text("first\nsecond\nthird\n", encoding="utf-8")
    return ledger


@pytest.mark.unit
def test_guard_module_imports_no_removed_core_module_and_reads_no_ledger_env() -> None:
    source = Path(core_ticket_guard.__file__).read_text(encoding="utf-8")
    assert _REMOVED_MODULE not in source
    assert _ENV_NAME not in source
    assert "MD_LEDGER_PATH_ENV" not in source
    assert "os.environ" not in source
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert _REMOVED_MODULE not in imported


@pytest.mark.unit
def test_explicit_ledger_path_resolves_a_citation_in_that_file(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    decision = CoreTicketGuard(lane=None, ledger_path=ledger).check(
        _defect_payload(f"{_LEDGER_NAME}:2")
    )
    assert decision.admitted is True, decision.reason


@pytest.mark.unit
def test_no_ledger_path_refuses_a_ledger_citation_even_when_the_env_names_a_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = _ledger(tmp_path)
    monkeypatch.setenv(_ENV_NAME, str(ledger))
    guard = CoreTicketGuard(lane=None)
    assert guard.ledger_path is None
    decision = guard.check(_defect_payload(f"{_LEDGER_NAME}:2"))
    assert decision.admitted is False
    assert "unresolvable_diagnosis" in decision.finding_codes
