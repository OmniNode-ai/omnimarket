# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18534: the real-Postgres twin errors, and never skips, without a database.

The twin (tests/test_omn18534_turn_count_real_postgres.py) is the evidence that
migration 0069 applies and that the terminal's count reads back on Postgres. A
skip there is a green mark with nothing behind it, so its guard must raise a
failure when the database is absent or unreachable, and no file or helper the
twin runs through may skip, xfail or be deselected. This file needs no database.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.test_omn18534_turn_count_real_postgres import require_postgres

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
_TWIN = _TESTS / "test_omn18534_turn_count_real_postgres.py"
_UNIT = _TESTS / "unit" / "delegation" / "test_terminal_turn_count_omn18534.py"
_GUARD = Path(__file__).resolve()
# The helpers the twin runs through; their names are what the twin imports.
_HELPERS = _TESTS / "test_omn15909_real_postgres_projection_write_path_gate.py"
_SKIPPING_NAMES = {"skip", "skipif", "xfail", "importorskip", "_connect_or_skip"}
_TWIN_CASES = 5


async def test_an_unreachable_database_fails_the_twin_guard_and_does_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("INTEGRATION_POSTGRES_PORT", "1")
    with pytest.raises(pytest.fail.Exception, match="cannot reach it"):
        await require_postgres()


def _skip_sites(tree: ast.AST, label: str) -> list[str]:
    """Every skip, xfail or skip-helper marker, call or reference, by line."""
    found: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        if name in _SKIPPING_NAMES:
            found.append(f"{label}:{node.lineno}:{name}")
    return found


def _file_skip_sites(path: Path) -> list[str]:
    return _skip_sites(ast.parse(path.read_text(encoding="utf-8")), path.name)


def _function_skip_sites(path: Path, name: str) -> list[str]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.name == name
        ):
            return _skip_sites(node, f"{path.name}::{name}")
    raise AssertionError(f"{name} is not defined in {path.name}")


def test_every_file_that_holds_the_twin_carries_no_skip_or_xfail() -> None:
    # The twin, its unit twin and this guard are the files that hold the cases.
    for path in (_TWIN, _UNIT, _GUARD):
        assert _file_skip_sites(path) == [], path.name


def test_the_helpers_the_twin_runs_through_never_skip() -> None:
    assert _function_skip_sites(_HELPERS, "_base_dsn") == []
    # `_provisioned_runner` reaches `_connect_or_skip` only when it is given no
    # DSN; the next test proves the twin never calls it that way, so the helper
    # itself may name nothing else that skips.
    sites = _function_skip_sites(_HELPERS, "_provisioned_runner")
    assert [site.rsplit(":", 1)[-1] for site in sites] == ["_connect_or_skip"], sites


def test_the_twin_always_hands_the_runner_its_own_dsn() -> None:
    calls = [
        node
        for node in ast.walk(ast.parse(_TWIN.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_provisioned_runner"
    ]
    assert len(calls) >= _TWIN_CASES - 1
    # With no DSN the runner's connect helper skips on a missing password, so
    # every call names one and the skip branch is unreachable.
    assert all(call.args or call.keywords for call in calls)


def test_no_pytest_option_or_conftest_deselects_the_integration_marker() -> None:
    options = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["pytest"]["ini_options"]
    assert "addopts" not in options, options.get("addopts")
    for name in ("pytest.ini", "tox.ini", "setup.cfg"):
        assert not (_ROOT / name).exists(), name
    for conftest in (_ROOT / "conftest.py", _TESTS / "conftest.py"):
        text = conftest.read_text(encoding="utf-8")
        assert "deselect" not in text, conftest
        assert "pytest_ignore_collect" not in text, conftest
    # The collection itself: the marker selects every case and drops none.
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "integration",
            "-p",
            "no:cacheprovider",
            str(_TWIN),
        ],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        timeout=240,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-600:] + done.stderr[-600:]
    assert f"{_TWIN_CASES}/{_TWIN_CASES} tests collected" in done.stdout or (
        f"{_TWIN_CASES} tests collected" in done.stdout
        and "deselected" not in done.stdout
    ), done.stdout[-600:]


def test_the_twin_keeps_its_five_cases() -> None:
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
        if not parametrized:
            cases += 1
            continue
        cases_arg = parametrized[0].args[1]
        assert isinstance(cases_arg, ast.List)
        cases += len(cases_arg.elts)
    assert cases == _TWIN_CASES


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
        "test_the_escalated_producer_count_exceeds_its_attempt_list_and_the_row_stores_it",
        "test_a_no_count_refusal_over_a_routed_row_with_no_tokens_stays_unknown",
        "test_a_reported_zero_survives_a_later_no_count_terminal_and_a_stamped_zero_yields",
        "test_the_infra_copy_check_reads_the_copy_and_fails_on_one_changed_byte",
    }
    assert required <= names, sorted(required - names)
