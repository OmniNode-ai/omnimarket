# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19401: the ``test`` task-type gate follows the target repo's own test conventions.

Reported from 46 in-memory-bus runs on a laptop: the quality gate for ``--task-type
test`` refused drafts for ``TASK_MISMATCH: missing @pytest.mark.unit`` and for missing
edge-case and error-path words whatever the prompt asked for, scoring them 0.61-0.88
against the 0.8 bar. A repository that does not mark its tests ``unit`` (the infra CI
tests mark about half of theirs) could not get a correct draft through.

The marker is now required when the prompt says the repo uses it, and the edge-case and
error-path rules apply when the prompt asks for that coverage or hands over code with an
error path. A real failure still fails: a repo that marks its tests, a draft with no
collectable test, a draft that does not compile, a ``pytest`` attribute that does not
exist, and a prompt that asks for edge cases the draft never covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    is_quality_accepted,
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_uses_pytest_mark_unit,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_MARKER_REASON = "TASK_MISMATCH: missing @pytest.mark.unit"
_EDGE_REASON = "TASK_MISMATCH: failed covers_edge_cases"
_ERROR_REASON = "TASK_MISMATCH: failed covers_error_paths"

# A repo whose tests carry no marker: the prompt hands over one of its own tests.
_UNMARKED_REPO_TEST = (
    "Existing test from this repo, tests/ci/test_ttl.py:\n\n"
    "```python\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "def test_parse_ttl_minutes():\n"
    "    assert parse_ttl('2m') == 120\n"
    "```\n\n"
)
# A repo whose tests are all marked ``unit``.
_MARKED_REPO_TEST = (
    "Existing test from this repo, tests/unit/test_ttl.py:\n\n"
    "```python\n"
    "import pytest\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "@pytest.mark.unit\n"
    "def test_parse_ttl_minutes():\n"
    "    assert parse_ttl('2m') == 120\n"
    "```\n\n"
)
_ASK = "Write a pytest test for parse_ttl that checks the seconds suffix."
_ASK_COVERAGE = (
    "Write pytest tests for parse_ttl. Cover edge cases (empty string, zero) and the "
    "error path for an invalid suffix."
)

_PLAIN_DRAFT = (
    "```python\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "def test_parse_ttl_seconds():\n"
    "    assert parse_ttl('5s') == 5\n"
    "```"
)
_MARKED_DRAFT = (
    "```python\n"
    "import pytest\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "@pytest.mark.unit\n"
    "def test_parse_ttl_seconds():\n"
    "    assert parse_ttl('5s') == 5\n"
    "```"
)
_COVERING_PLAIN_DRAFT = (
    "```python\n"
    "import pytest\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "def test_parse_ttl_empty_is_an_edge_case():\n"
    "    assert parse_ttl('') is None\n\n\n"
    "def test_parse_ttl_invalid_suffix_raises():\n"
    "    with pytest.raises(ValueError):\n"
    "        parse_ttl('5x')\n"
    "```"
)
_COVERING_MARKED_DRAFT = _COVERING_PLAIN_DRAFT.replace(
    "def test_parse_ttl_empty", "@pytest.mark.unit\ndef test_parse_ttl_empty"
)
_BOGUS_ATTRIBUTE_DRAFT = (
    "```python\n"
    "import pytest\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "@pytest.mark_unit\n"
    "def test_parse_ttl_seconds():\n"
    "    assert parse_ttl('5s') == 5\n"
    "```"
)
_UNBOUND_PYTEST_DRAFT = (
    "```python\n"
    "from ci.ttl import parse_ttl\n\n\n"
    "@pytest.mark.slow\n"
    "def test_parse_ttl_seconds():\n"
    "    assert parse_ttl('5s') == 5\n"
    "```"
)
_NO_TEST_DRAFT = "```python\nfrom ci.ttl import parse_ttl\n\n\ndef helper():\n    return parse_ttl('5s')\n```"
_NOT_PYTHON_DRAFT = (
    "```python\ndef test_parse_ttl_seconds(:\n    assert parse_ttl('5s') == 5\n```"
)


@dataclass(frozen=True)
class ReplayCase:
    """One replayed draft: its prompt, the draft, and whether the gate must accept it."""

    name: str
    prompt: str
    draft: str
    passes: bool
    #: What the gate said before this ticket, measured at origin/dev c2eb05b30.
    passed_before: bool


REPLAY: tuple[ReplayCase, ...] = (
    # --- repo that does not mark its tests -------------------------------------
    ReplayCase("unmarked-repo/plain-ask", _ASK, _PLAIN_DRAFT, True, False),
    ReplayCase(
        "unmarked-repo/sample-test-in-prompt",
        _UNMARKED_REPO_TEST + _ASK,
        _PLAIN_DRAFT,
        True,
        False,
    ),
    ReplayCase(
        "unmarked-repo/prompt-says-no-markers",
        _ASK + " This repo does not use pytest markers; do not add any.",
        _PLAIN_DRAFT,
        True,
        False,
    ),
    ReplayCase(
        "unmarked-repo/prompt-forbids-the-unit-marker",
        _ASK + " Do not use @pytest.mark.unit here.",
        _PLAIN_DRAFT,
        True,
        False,
    ),
    ReplayCase(
        "unmarked-repo/coverage-asked-and-covered",
        _UNMARKED_REPO_TEST + _ASK_COVERAGE,
        _COVERING_PLAIN_DRAFT,
        True,
        False,
    ),
    # --- repo that marks its tests -----------------------------------------------
    ReplayCase(
        "marked-repo/sample-test-marked-draft-marked",
        _MARKED_REPO_TEST + _ASK,
        _MARKED_DRAFT,
        True,
        True,
    ),
    ReplayCase(
        "marked-repo/prompt-asks-for-the-marker-draft-marked",
        _ASK + " Mark every test with @pytest.mark.unit.",
        _MARKED_DRAFT,
        True,
        True,
    ),
    ReplayCase(
        "marked-repo/coverage-asked-and-covered",
        _MARKED_REPO_TEST + _ASK_COVERAGE,
        _COVERING_MARKED_DRAFT,
        True,
        True,
    ),
    # --- real failures that must keep failing ----------------------------------
    ReplayCase(
        "marked-repo/draft-drops-the-marker",
        _MARKED_REPO_TEST + _ASK,
        _PLAIN_DRAFT,
        False,
        False,
    ),
    ReplayCase(
        "marked-repo/prompt-asks-for-the-marker-draft-drops-it",
        _ASK + " Mark every test with @pytest.mark.unit.",
        _PLAIN_DRAFT,
        False,
        False,
    ),
    ReplayCase(
        "unmarked-repo/pytest-attribute-does-not-exist",
        _UNMARKED_REPO_TEST + _ASK,
        _BOGUS_ATTRIBUTE_DRAFT,
        False,
        False,
    ),
    ReplayCase(
        "unmarked-repo/pytest-read-but-never-imported",
        _UNMARKED_REPO_TEST + _ASK,
        _UNBOUND_PYTEST_DRAFT,
        False,
        False,
    ),
    ReplayCase(
        "unmarked-repo/no-collectable-test",
        _UNMARKED_REPO_TEST + _ASK,
        _NO_TEST_DRAFT,
        False,
        False,
    ),
    ReplayCase(
        "unmarked-repo/draft-does-not-compile",
        _UNMARKED_REPO_TEST + _ASK,
        _NOT_PYTHON_DRAFT,
        False,
        False,
    ),
)


def _run(prompt: str | None, draft: str) -> tuple[ModelQualityGateResult, bool]:
    """Run the gate as both dispatch paths run it, and decide acceptance as they do.

    The DoD comes from the prompt, the prompt is the grounding source, and the verdict
    is the one the bus orchestrator and the bus-less port share: a ``test`` answer is
    accepted on its executed checks, and a deterministic-floor rejection is final.
    """
    deterministic, heuristic = resolve_task_class_dod_checks("test", prompt=prompt)
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="test",
            llm_response_content=draft,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=prompt,
    )
    accepted = is_quality_accepted(
        result,
        resolve_required_bar_authority(task_type="test"),
        declared_deterministic=deterministic,
    )
    return result, accepted


def _gate(prompt: str, draft: str) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    result, accepted = _run(prompt, draft)
    return accepted, result.failure_reasons, result.skipped_checks


@pytest.mark.parametrize("case", REPLAY, ids=[case.name for case in REPLAY])
def test_replayed_drafts_are_accepted_when_correct_and_refused_when_wrong(
    case: ReplayCase,
) -> None:
    passed, reasons, _ = _gate(case.prompt, case.draft)
    assert passed is case.passes, reasons


def test_the_replay_set_moves_only_the_drafts_the_conventions_free() -> None:
    """Acceptance before and after: the correct drafts move, the wrong ones do not."""
    accepted_after = {case.name for case in REPLAY if _gate(case.prompt, case.draft)[0]}
    accepted_before = {case.name for case in REPLAY if case.passed_before}
    assert accepted_before < accepted_after
    assert accepted_after == {case.name for case in REPLAY if case.passes}


def test_the_bare_prompt_reproduces_the_reported_rejection_shape_no_more() -> None:
    """The unmarked draft for a bare ask scored 0.61 before; it must now be accepted."""
    passed, reasons, skipped = _gate(_ASK, _PLAIN_DRAFT)
    assert passed, reasons
    assert "uses_pytest_mark_unit" in skipped


# ---------------------------------------------------------------------------
# The marker follows the prompt.
# ---------------------------------------------------------------------------


def test_a_gate_with_no_prompt_keeps_requiring_the_marker() -> None:
    """No prompt means nothing to read a convention from; the old floor stays."""
    result, accepted = _run(None, _COVERING_PLAIN_DRAFT)
    assert not accepted
    assert _MARKER_REASON in result.failure_reasons


def test_a_skipped_marker_is_recorded_and_not_reported_as_a_pass() -> None:
    result, _ = _run(_ASK, _PLAIN_DRAFT)
    assert "uses_pytest_mark_unit" in result.skipped_checks
    assert "uses_pytest_mark_unit" not in {
        evaluation.rule for evaluation in result.rule_evaluations
    }


@pytest.mark.parametrize(
    "prompt",
    [
        _ASK + " Use the repo's @pytest.mark.unit marker.",
        _MARKED_REPO_TEST + _ASK,
        _ASK + " Tests are marked with pytestmark = pytest.mark.unit in this repo.",
        _ASK + " Add the unit marker to each test.",
    ],
)
def test_a_prompt_naming_the_marker_requires_it(prompt: str) -> None:
    passed, reasons, _ = _gate(prompt, _PLAIN_DRAFT)
    assert not passed
    assert _MARKER_REASON in reasons


@pytest.mark.parametrize(
    "prompt",
    [
        _ASK + " Do not use @pytest.mark.unit.",
        _ASK + " This repo has no pytest.mark.unit marker.",
        _ASK + " Don't add markers; the tests here are unmarked.",
        _ASK + " Write the test without markers.",
    ],
)
def test_a_prompt_ruling_the_marker_out_does_not_require_it(prompt: str) -> None:
    passed, reasons, skipped = _gate(prompt, _PLAIN_DRAFT)
    assert passed, reasons
    assert "uses_pytest_mark_unit" in skipped


def test_the_marker_helper_still_rejects_a_missing_marker_by_default() -> None:
    assert _check_uses_pytest_mark_unit(_PLAIN_DRAFT) == _MARKER_REASON


def test_the_marker_helper_can_waive_the_marker_and_keep_the_import_checks() -> None:
    assert _check_uses_pytest_mark_unit(_PLAIN_DRAFT, require_mark=False) is None
    bogus = _check_uses_pytest_mark_unit(_BOGUS_ATTRIBUTE_DRAFT, require_mark=False)
    assert bogus is not None
    assert "pytest.mark_unit does not exist" in bogus
    unbound = _check_uses_pytest_mark_unit(_UNBOUND_PYTEST_DRAFT, require_mark=False)
    assert unbound is not None
    assert "never imports it" in unbound


# ---------------------------------------------------------------------------
# Edge-case and error-path rules follow the prompt.
# ---------------------------------------------------------------------------


def _heuristic(prompt: str | None) -> tuple[str, ...]:
    return resolve_task_class_dod_checks("test", prompt=prompt)[1]


def test_a_prompt_asking_for_neither_does_not_carry_the_coverage_rules() -> None:
    heuristic = _heuristic(_ASK)
    assert "covers_edge_cases" not in heuristic
    assert "covers_error_paths" not in heuristic
    assert "no_refusal" in heuristic


def test_no_prompt_keeps_the_coverage_rules() -> None:
    heuristic = _heuristic(None)
    assert "covers_edge_cases" in heuristic
    assert "covers_error_paths" in heuristic


@pytest.mark.parametrize(
    "prompt",
    [
        "Write pytest tests for parse_ttl, including edge cases.",
        "Write pytest tests for parse_ttl with boundary values.",
        "Write pytest tests for parse_ttl and test the empty string.",
        "Write comprehensive pytest tests for parse_ttl.",
        "Write pytest tests for parse_ttl and cover the corner cases.",
    ],
)
def test_a_prompt_asking_for_edge_cases_keeps_that_rule(prompt: str) -> None:
    assert "covers_edge_cases" in _heuristic(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "Write pytest tests for parse_ttl, including error handling.",
        "Write pytest tests for parse_ttl that check it raises on a bad suffix.",
        "Write pytest tests for parse_ttl and the failure path.",
        "Write pytest tests for parse_ttl and cover invalid input.",
        "Write negative tests for parse_ttl.",
        "Write pytest tests for this function:\n\ndef f(x):\n    if x < 0:\n        raise ValueError(x)\n    return x\n",
    ],
)
def test_a_prompt_asking_for_error_paths_keeps_that_rule(prompt: str) -> None:
    assert "covers_error_paths" in _heuristic(prompt)


def test_an_asked_for_edge_case_is_still_scored_when_the_draft_covers_none() -> None:
    """The coverage rules grade the score; they never decided a test answer's verdict."""
    result, _ = _run(_ASK_COVERAGE, _PLAIN_DRAFT)
    verdicts = {e.rule: e.passed for e in result.rule_evaluations}
    assert verdicts["covers_edge_cases"] is False
    assert verdicts["covers_error_paths"] is False
    covered, _ = _run(_ASK_COVERAGE, _COVERING_PLAIN_DRAFT)
    assert covered.quality_score > result.quality_score
    assert covered.quality_score >= 0.8 > result.quality_score


def test_a_waived_coverage_rule_does_not_lower_the_score() -> None:
    result, _ = _run(_ASK, _PLAIN_DRAFT)
    assert result.quality_score == 1.0
