# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18534: the real-Postgres twin errors, and never skips, without a database.

The twin (tests/test_omn18534_turn_count_real_postgres.py) is the evidence that
migration 0069 applies and that the terminal's count reads back on Postgres. A
skip there is a green mark with nothing behind it, so its guard must raise a
failure when the database is absent or unreachable. This file needs no database.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.test_omn18534_turn_count_real_postgres import require_postgres

_TESTS = Path(__file__).resolve().parent
_TWIN = _TESTS / "test_omn18534_turn_count_real_postgres.py"
_UNIT = _TESTS / "unit" / "delegation" / "test_terminal_turn_count_omn18534.py"


async def test_an_unreachable_database_fails_the_twin_guard_and_does_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("INTEGRATION_POSTGRES_PORT", "1")
    with pytest.raises(pytest.fail.Exception, match="cannot reach it"):
        await require_postgres()


def _skip_sites(path: Path) -> list[str]:
    """Every skip or xfail marker or call in a test file, by line."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and (
            (node.attr in {"skip", "skipif", "xfail"})
            and isinstance(node.value, ast.Name | ast.Attribute)
        ):
            found.append(f"{path.name}:{node.lineno}:{node.attr}")
    return found


def test_the_twin_and_the_unit_file_carry_no_skip_or_xfail() -> None:
    assert _skip_sites(_TWIN) == []
    assert _skip_sites(_UNIT) == []


def test_the_twin_keeps_its_four_cases() -> None:
    tree = ast.parse(_TWIN.read_text(encoding="utf-8"))
    cases = 0
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith(
            "test_"
        ):
            continue
        parametrized = [
            deco
            for deco in node.decorator_list
            if isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Attribute)
            and deco.func.attr == "parametrize"
        ]
        cases += len(parametrized[0].args[1].elts) if parametrized else 1  # type: ignore[attr-defined]
    assert cases == 4


def test_the_unit_file_keeps_the_tests_that_carry_the_criteria() -> None:
    names = {
        node.name
        for node in ast.walk(ast.parse(_UNIT.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    required = {
        "test_failure_before_any_turn_stores_zero_not_null",
        "test_skill_terminal_stores_the_stated_count_not_the_history_length",
        "test_a_malformed_count_is_never_replaced_by_the_inferred_zero",
        "test_async_later_refused_terminal_keeps_an_earlier_count",
        "test_async_later_terminal_that_reports_a_count_replaces_the_earlier_one",
        "test_migration_0069_bytes_are_pinned_for_the_omnibase_infra_copy",
    }
    assert required <= names, sorted(required - names)
