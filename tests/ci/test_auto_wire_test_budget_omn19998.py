# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19998 - the in-process auto-wiring test keeps its own timeout budget.

``test_every_node_auto_wires_in_process`` wires every node contract in one
``wire_from_manifest`` pass, so it cannot be split per node without losing the
whole-manifest checks (unique contract names, single-owner command topics, the
registration-id preflight). Under a shard's coverage tracing it outgrows the
shard-wide 300 s pytest timeout, so it carries its own larger
``pytest.mark.timeout``. These tests read the marker from the source, so they
cost nothing, and they fail on a tree where the marker is gone.
"""

from __future__ import annotations

import ast
from pathlib import Path

_WIRING_TEST = (
    Path(__file__).resolve().parents[1]
    / "unit"
    / "contracts"
    / "test_every_node_auto_wires_in_process_omn17427.py"
)
_TEST_NAME = "test_every_node_auto_wires_in_process"
# The shard-wide bound in ci.yml is 300 s; the budget must clear it by a margin
# that covers the measured 482 to 489 s under coverage.
_MINIMUM_BUDGET_SECONDS = 900


def timeout_budget(source: str, test_name: str) -> tuple[int, str] | None:
    """The (seconds, method) of the test's own ``pytest.mark.timeout``, or None."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name != test_name:
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if ast.unparse(decorator.func) != "pytest.mark.timeout":
                continue
            seconds = decorator.args[0] if decorator.args else None
            if not (
                isinstance(seconds, ast.Constant) and isinstance(seconds.value, int)
            ):
                continue
            method = next(
                (
                    keyword.value.value
                    for keyword in decorator.keywords
                    if keyword.arg == "method"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                "",
            )
            return seconds.value, method
    return None


def test_wiring_test_has_its_own_timeout_budget() -> None:
    budget = timeout_budget(_WIRING_TEST.read_text(), _TEST_NAME)
    assert budget is not None, f"{_TEST_NAME} lost its own pytest.mark.timeout"
    seconds, method = budget
    assert seconds >= _MINIMUM_BUDGET_SECONDS
    assert method == "thread"


def test_budget_reader_sees_no_budget_without_the_marker() -> None:
    source = f"import pytest\n\nasync def {_TEST_NAME}() -> None:\n    pass\n"
    assert timeout_budget(source, _TEST_NAME) is None


def test_budget_reader_reads_a_marked_test() -> None:
    source = (
        "import pytest\n\n"
        '@pytest.mark.timeout(900, method="thread")\n'
        f"async def {_TEST_NAME}() -> None:\n    pass\n"
    )
    assert timeout_budget(source, _TEST_NAME) == (900, "thread")
