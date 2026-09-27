# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Static name resolution for the code task classes (OMN-19529).

``compiles_without_errors`` proves only that delegated code parses. Run
``40e9de70`` of the delegation capability matrix returned code that raises
NameError on import and the gate scored it 1.0. ``names_resolve`` is the
non-executing half of "imports in isolation": every name the code reads must
be bound by it, be a builtin, or occur in the grounding source. A pure reducer
must never run model-written code, so the check reads and does not execute.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.delegation.content_grounding import (
    evaluate_name_resolution,
    resolve_name_resolution_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    SUPPORTED_DETERMINISTIC_CHECKS,
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FENCE = "`" * 3

_INVENTED_HELPER = (
    f"{_FENCE}python\n"
    "def total(rows: list[int]) -> int:\n"
    "    return summed_with_retry(rows)\n"
    f"{_FENCE}"
)


def _unresolved(content: str, source: str | None) -> tuple[str, ...] | None:
    verdict = evaluate_name_resolution(
        content=content,
        grounding_source=source,
        policy=resolve_name_resolution_policy(),
    )
    return verdict.unresolved if verdict.evaluated else None


def _code_input(content: str) -> ModelQualityGateInput:
    deterministic, heuristic = resolve_task_class_dod_checks("code_generation")
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


def test_a_name_bound_nowhere_fails_the_gate_deterministically() -> None:
    result = delta(
        _code_input(_INVENTED_HELPER),
        grounding_source="Write total(rows) returning the sum of the rows.",
    )

    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any(
        reason.startswith("UNGROUNDED:") and "summed_with_retry" in reason
        for reason in result.failure_reasons
    ), result.failure_reasons


def test_a_name_the_prompt_mentions_resolves() -> None:
    """A fragment may use names from the file the prompt describes."""
    result = delta(
        _code_input(_INVENTED_HELPER),
        grounding_source="Use summed_with_retry from utils to implement total(rows).",
    )

    assert result.passed is True, result.failure_reasons
    assert "names_resolve" not in result.skipped_checks


def test_without_a_source_the_check_is_skipped_and_recorded_never_passed() -> None:
    result = delta(_code_input(_INVENTED_HELPER))

    assert "names_resolve" in result.skipped_checks
    assert all(
        evaluation.rule != "names_resolve" for evaluation in result.rule_evaluations
    )
    assert _unresolved(_INVENTED_HELPER, None) is None


@pytest.mark.parametrize(
    "code",
    [
        # builtins, imports, parameters, locals
        "import os\n\ndef f(x: int) -> str:\n    y = len(str(x))\n    print(os.getcwd())\n"
        "    if y < 0:\n        raise ValueError(y)\n    return str(y)\n",
        # comprehension variables and self attributes
        "class C:\n    def __init__(self) -> None:\n        self.items = [i for i in range(3)]\n"
        "    def total(self) -> int:\n        return sum(self.items)\n",
        # a module-level function called before its definition
        "def first() -> int:\n    return second()\n\ndef second() -> int:\n    return 2\n",
        # from-imports and a class used before its definition in a function
        "from pathlib import Path\n\ndef make() -> 'Box':\n    return Box(Path('.'))\n\n"
        "class Box:\n    def __init__(self, p: Path) -> None:\n        self.p = p\n",
        # nested scopes and closures
        "def outer() -> int:\n    n = 1\n    def inner() -> int:\n        return n + 1\n"
        "    return inner()\n",
        # module dunders
        "print(__name__, __file__)\n",
        # a global bound inside a function
        "def setup() -> None:\n    global CACHE\n    CACHE = {}\n\ndef get() -> dict:\n"
        "    return CACHE\n",
    ],
)
def test_names_bound_by_the_code_or_python_resolve(code: str) -> None:
    assert _unresolved(code, "") == ()


def test_unparseable_code_is_left_to_compiles_without_errors() -> None:
    assert _unresolved("def f(:\n    pass\n", "") == ()


def test_a_star_import_makes_names_unresolvable_so_nothing_is_reported() -> None:
    assert _unresolved("from os.path import *\n\nprint(joinx('a'))\n", "") == ()


def test_only_python_fences_are_read() -> None:
    answer = (
        f"{_FENCE}python\nvalue = missing_name + 1\n{_FENCE}\n\n"
        f"{_FENCE}yaml\nkey: other_name\n{_FENCE}\n"
    )

    assert _unresolved(answer, "") == ("missing_name",)


def test_the_check_is_declared_on_the_code_classes_and_is_supported() -> None:
    assert "names_resolve" in SUPPORTED_DETERMINISTIC_CHECKS
    assert resolve_name_resolution_policy().check_name == "names_resolve"
    for task_type in ("code_generation", "refactor", "test", "validator_generation"):
        deterministic, _ = resolve_task_class_dod_checks(task_type)
        assert "names_resolve" in deterministic, task_type
    for task_type in ("summarization", "document", "code_review"):
        deterministic, _ = resolve_task_class_dod_checks(task_type)
        assert "names_resolve" not in deterministic, task_type
