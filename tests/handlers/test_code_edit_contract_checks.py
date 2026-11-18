# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of declared checks from a ticket's contract (OMN-23930)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.code_edit_contract_checks import declared_checks_from_contract


def test_two_test_passes_checks(tmp_path: Path) -> None:
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        "dod_evidence:\n"
        "  - execution_scope: ci\n"
        "    checks:\n"
        "      - check_type: test_passes\n"
        "        check_value: uv run pytest tests/test_a.py -q\n"
        "      - check_type: lint\n"
        "        check_value: uv run ruff check\n"
        "  - execution_scope: local_done_gate\n"
        "    checks:\n"
        "      - check_type: test_passes\n"
        "        check_value: uv run pytest tests/test_b.py -q\n"
        "  - execution_scope: ci\n"
        "    checks:\n"
        "      - check_type: test_passes\n"
        "        check_value: uv run pytest tests/test_c.py -q\n",
        encoding="utf-8",
    )
    checks = declared_checks_from_contract(contract, timeout_seconds=300)
    assert len(checks) == 2
    assert checks[0].name == "ticket0"
    assert checks[0].argv == ("uv", "run", "pytest", "tests/test_a.py", "-q")
    assert checks[0].targets == ("tests/test_a.py",)
    assert checks[0].timeout_seconds == 300
    assert checks[1].name == "ticket1"
    assert checks[1].argv == ("uv", "run", "pytest", "tests/test_c.py", "-q")
    assert checks[1].targets == ("tests/test_c.py",)


def test_local_done_gate_skipped(tmp_path: Path) -> None:
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        "dod_evidence:\n"
        "  - execution_scope: local_done_gate\n"
        "    checks:\n"
        "      - check_type: test_passes\n"
        "        check_value: uv run pytest tests/test_a.py -q\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no test_passes check"):
        declared_checks_from_contract(contract, timeout_seconds=300)


def test_no_test_passes_raises(tmp_path: Path) -> None:
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        "dod_evidence:\n"
        "  - execution_scope: ci\n"
        "    checks:\n"
        "      - check_type: lint\n"
        "        check_value: uv run ruff check\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no test_passes check"):
        declared_checks_from_contract(contract, timeout_seconds=300)
