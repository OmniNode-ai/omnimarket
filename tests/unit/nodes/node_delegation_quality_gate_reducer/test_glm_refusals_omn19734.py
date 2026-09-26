# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19734: GLM shell and edit answers, and prompt-quoted dangling tails."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_compiles_without_errors,
    _check_semantic_adequacy,
    _check_short_form_adequacy,
    _compiles_without_errors_is_evaluable,
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit


class _StampedGateInput(ModelQualityGateInput):
    """Stand in for the core wire field until the local core floor includes it."""

    grounding_source: str | None = None


_SHELL_ANSWER = """```sh
#!/bin/sh
[ $# -gt 0 ] || unsupported
case "$1" in
    topic) printf '%s\\n' events ;;
    *) unsupported ;;
esac
```"""

_EDIT_ANSWER = """FILE: src/x.py
<<<<<<< SEARCH
    def old(self):
        return 1
=======
    def new(self):
        return 2
>>>>>>> REPLACE"""

_SOURCE = (
    "The note was posted under the operator's own identity -- held, never drafted at"
)
_COPIED_TAIL = (
    "- Reason (verbatim): posted under the operator's own identity -- "
    "held, never drafted at"
)
_UNQUOTED_TAIL = "the change adds a graded score so the"


def test_glm_refusals_shell_fence_is_not_python() -> None:
    assert _check_compiles_without_errors(_SHELL_ANSWER) is None
    assert not _compiles_without_errors_is_evaluable(_SHELL_ANSWER)


def test_glm_refusals_search_replace_is_not_evaluable() -> None:
    assert not _compiles_without_errors_is_evaluable(_EDIT_ANSWER)


@pytest.mark.parametrize(
    "content",
    ["```\ndef f(:\n```", "```python\ndef f(:\n```", "def f(:"],
)
def test_glm_refusals_broken_python_still_fails(content: str) -> None:
    assert _compiles_without_errors_is_evaluable(content)
    assert "does not compile as Python" in (
        _check_compiles_without_errors(content) or ""
    )


def test_glm_refusals_mixed_fences_still_parse_python() -> None:
    content = "```bash\necho okay\n```\n```python\ndef f(:\n```"
    assert _compiles_without_errors_is_evaluable(content)
    assert "does not compile as Python" in (
        _check_compiles_without_errors(content) or ""
    )


def test_glm_refusals_shell_gate_records_skip_without_pass() -> None:
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="code_generation",
            llm_response_content=_SHELL_ANSWER,
            dod_deterministic=("compiles_without_errors",),
        )
    )
    assert "compiles_without_errors" in result.skipped_checks
    assert result.passed is False
    assert all(
        evaluation.rule != "compiles_without_errors"
        for evaluation in result.rule_evaluations
    )
    assert not any(
        "does not compile as Python" in reason for reason in result.failure_reasons
    )


@pytest.mark.parametrize(
    "check", [_check_semantic_adequacy, _check_short_form_adequacy]
)
def test_glm_refusals_copied_tail_is_complete(
    check: Callable[..., str | None],
) -> None:
    assert check(_COPIED_TAIL, grounding_source=_SOURCE) is None
    assert "truncated mid-clause (ends on 'at')" in (
        check(_COPIED_TAIL, grounding_source=None) or ""
    )
    assert "truncated mid-clause (ends on 'the')" in (
        check(_UNQUOTED_TAIL, grounding_source=_SOURCE) or ""
    )


@pytest.mark.parametrize(
    "check", [_check_semantic_adequacy, _check_short_form_adequacy]
)
def test_glm_refusals_answer_cut_inside_a_quote_is_still_truncated(
    check: Callable[..., str | None],
) -> None:
    """The source continues past the answer's last word, so the cut is the answer's."""
    source = "Summarize: the change adds a graded score so the gate can compare rungs."
    cut = "Summary: the change adds a graded score so the"
    assert "truncated mid-clause (ends on 'the')" in (
        check(cut, grounding_source=source) or ""
    )


@pytest.mark.parametrize("adequacy_rule", ["semantic_adequacy", "short_form_adequacy"])
def test_glm_refusals_document_gate_uses_stamped_grounding_source(
    adequacy_rule: str,
) -> None:
    result = delta(
        _StampedGateInput(
            correlation_id=uuid4(),
            task_type="document",
            llm_response_content=_COPIED_TAIL,
            grounding_source=_SOURCE,
            dod_deterministic=("response_non_empty",),
            dod_heuristic=(adequacy_rule,),
        )
    )
    assert result.passed, result.failure_reasons
    assert not any(
        "truncated mid-clause" in reason for reason in result.failure_reasons
    )
