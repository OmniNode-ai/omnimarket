# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20844: a unit mark on a module that never binds ``pytest`` is refused.

The 2026-10-14 rung benchmark stored three ``test`` answers from the
local-qwen3.8-27b rung (R4-08, R4-10, R4-11) that decorate every test
``@pytest.mark.unit`` and never import pytest. Collecting the module raises
``NameError: name 'pytest' is not defined``, so no test runs, and the benchmark
scorer failed all three. The gate accepted all three on the deterministic floor
(acceptance_reason judge_unavailable_deterministic_floor): the
``uses_pytest_mark_unit`` rule found the mark it demands and did not ask whether
the module can read it. ``names_resolve`` did not see it either, because the
prompt names pytest, so the name occurs in the grounding source.

The fixture holds the three stored answers and, as the negative control, every
stored answer to the same three tasks that the scorer passed and that
carries the unit mark.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_uses_pytest_mark_unit,
)

pytestmark = pytest.mark.unit

_FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "omn20844_unbound_pytest_answers.json"
    ).read_text(encoding="utf-8")
)
_UNBOUND = _FIXTURE["unbound_pytest"]
_PASSED = _FIXTURE["scorer_passed"]


def test_fixture_holds_the_stored_answers() -> None:
    assert sorted(cell["task_id"] for cell in _UNBOUND) == ["R4-08", "R4-10", "R4-11"]
    assert all(cell["gate_passed"] is True for cell in _UNBOUND)
    assert all(cell["scorer_outcome"] == "fail" for cell in _UNBOUND)
    assert len(_PASSED) == 4
    assert all(cell["scorer_outcome"] == "pass" for cell in _PASSED)


@pytest.mark.parametrize("cell", _UNBOUND, ids=lambda cell: cell["task_id"])
def test_stored_answer_without_import_pytest_is_refused(cell: dict[str, str]) -> None:
    reason = _check_uses_pytest_mark_unit(cell["response"])
    assert reason is not None
    assert "pytest" in reason
    assert "never" in reason


@pytest.mark.parametrize(
    "cell", _PASSED, ids=lambda cell: f"{cell['task_id']}-{cell['rung']}"
)
def test_stored_answer_the_scorer_passed_is_still_accepted(
    cell: dict[str, str],
) -> None:
    assert _check_uses_pytest_mark_unit(cell["response"]) is None


_MODULE = """from subject import add


@pytest.mark.unit
def test_add():
    assert add(1, 2) == 3
"""


@pytest.mark.parametrize(
    "binding",
    [
        "import pytest\n",
        "import pytest as pytest\n",
        "from _pytest import compat\nimport os, pytest\n",
    ],
)
def test_any_import_binding_pytest_satisfies(binding: str) -> None:
    assert _check_uses_pytest_mark_unit(binding + _MODULE) is None


def test_module_marker_without_import_is_refused() -> None:
    code = _MODULE.replace("@pytest.mark.unit\n", "").replace(
        "from subject import add\n",
        "from subject import add\n\npytestmark = pytest.mark.unit\n",
    )
    assert _check_uses_pytest_mark_unit(code) is not None


def test_fenced_answer_without_import_is_refused() -> None:
    assert _check_uses_pytest_mark_unit(f"```python\n{_MODULE}```") is not None


def test_fragment_with_no_imports_is_not_judged_on_binding() -> None:
    # A fragment carries no import of its own: it is pasted into a module that
    # may import pytest, so the binding cannot be judged from the answer.
    fragment = "@pytest.mark.unit\ndef test_add():\n    assert 1 + 2 == 3\n"
    assert _check_uses_pytest_mark_unit(fragment) is None
