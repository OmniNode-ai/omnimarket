# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14220: declared local-first task classes must PASS the LOCAL gate.

Before this fix, ``code_review`` / ``review`` / ``agent_delegation`` / ``planning``
had a task-class contract but no *local* acceptance authority, so a valid LOCAL
output could never PASS ``delta`` (no verifiable floor, no ``semantic_adequacy``,
no local judge) and the ladder force-escalated to the PAID cloud tier. ``planning``
additionally declared two DoD checks (``structured_output``/``covers_dependencies``)
that had no executor and hard-failed as ``MALFORMED``. ``documentation`` was tripped
by two reject-only false positives (an ``error:`` substring match on exception-type
names, and an unconditional ``raises:`` requirement).

These tests assert a representative GOOD local output PASSES for each class, that
the refusal/docstring reject-only pre-filters no longer false-fire, and that BAD
output is still rejected.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_covers_args_returns_raises,
    _check_no_refusal,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

# Representative GOOD local outputs — the kind of artifact the local Qwen coder
# actually returns for each class (verified live for ``review``).
_GOOD_OUTPUTS: dict[str, str] = {
    "code_review": (
        "Reviewing the diff:\n"
        "- Line 42: the file opened here is never closed; use a context manager.\n"
        "- Line 58: `except Exception` is too broad; narrow it to ValueError.\n"
        "Two must-fix issues at lines 42 and 58."
    ),
    "review": (
        "Review with line-by-line feedback:\n"
        "1. Line 2: resource leak — the file handle is never closed; use "
        "`with open(path) as f:`.\n"
        "2. Line 4: `json.loads` is called but `json` is never imported."
    ),
    "agent_delegation": (
        "Task completed. Sub-tasks verified: (1) contract added — checked; "
        "(2) sets updated — checked; (3) tests pass — evidence: 641 passed."
    ),
    "planning": (
        "Plan:\n"
        "1. Add the contract (no dependencies).\n"
        "2. Update the verifiable set — depends on step 1.\n"
        "3. Add a regression test — requires steps 1 and 2 in order."
    ),
    "documentation": (
        "def divide(a, b):\n"
        '    """Return a divided by b.\n\n'
        "    Args:\n"
        "        a: numerator.\n"
        "        b: denominator.\n"
        "    Returns:\n"
        "        The quotient a / b, guarding a ZeroDivisionError at runtime.\n"
        '    """\n'
        "    return a / b"
    ),
}


def _gate(task_type: str, content: str) -> ModelQualityGateInput:
    det, heur = resolve_task_class_dod_checks(task_type)
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=det,
        dod_heuristic=heur,
    )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", sorted(_GOOD_OUTPUTS))
def test_declared_local_first_class_passes_locally(task_type: str) -> None:
    """A representative good output PASSES the LOCAL gate (judge unavailable)."""
    result = quality_gate_delta(
        _gate(task_type, _GOOD_OUTPUTS[task_type]),
        judge_adequacy_score=None,
        judge_verdict=None,
    )
    assert result.passed, (
        f"{task_type} good local output must PASS the gate without a judge; "
        f"reasons={list(result.failure_reasons)}"
    )


@pytest.mark.unit
def test_bad_planning_output_still_rejected() -> None:
    """An unstructured, dependency-free plan still fails the reject-only markers."""
    result = quality_gate_delta(
        _gate("planning", "Just do the thing however you want."),
        judge_adequacy_score=None,
        judge_verdict=None,
    )
    assert not result.passed


@pytest.mark.unit
def test_refusal_review_still_rejected() -> None:
    """A genuine refusal is still rejected for a code-review class."""
    result = quality_gate_delta(
        _gate("code_review", "I cannot help with that request."),
        judge_adequacy_score=None,
        judge_verdict=None,
    )
    assert not result.passed


@pytest.mark.unit
def test_leading_error_message_is_refusal() -> None:
    """A bare leading ``Error:`` message is still detected as a refusal."""
    assert _check_no_refusal("Error: could not generate output.") is not None
    assert _check_no_refusal("error: something failed") is not None


@pytest.mark.unit
def test_in_word_exception_name_is_not_refusal() -> None:
    """An in-word exception type (``ZeroDivisionError:``) is NOT a refusal."""
    assert _check_no_refusal("Raises:\n    ZeroDivisionError: if b is zero.") is None
    assert _check_no_refusal("May raise a ValueError: when input is invalid.") is None


@pytest.mark.unit
def test_docstring_without_raises_section_passes_covers_check() -> None:
    """A docstring for a non-raising function need not carry a Raises: section."""
    docstring = (
        '"""Return the sum.\n\n'
        "    Args:\n        a: first.\n        b: second.\n"
        "    Returns:\n        The sum.\n    "
        '"""'
    )
    assert _check_covers_args_returns_raises(docstring) is None


@pytest.mark.unit
def test_docstring_missing_args_returns_still_rejected() -> None:
    """The reject-only docstring pre-filter still fails on missing args/returns."""
    assert (
        _check_covers_args_returns_raises("Just some prose, no sections.") is not None
    )
