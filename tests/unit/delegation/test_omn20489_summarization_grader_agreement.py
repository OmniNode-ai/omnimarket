# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""The summarization grader agrees with a careful reading of 120 summaries (OMN-20489).

RED-first anchor: 60 summarization prompts (pull request descriptions, commit
ranges, CI logs and scan reports from this repository's own history) were each
answered by two models, and all 120 answers were read against their sources by a
careful reader. The contract grader as it stood agreed with that reading on 78
of 120 answers. 25 of its 27 rejections were answers the careful reading accepts:

* ``claims_grounded`` read a status word in a commit or PR body as a claim about
  the ticket id on the subject line, then searched only that one line (16 answers);
* ``accurate`` read the bare word "unverified" as the summary disclaiming its own
  accuracy, in "0 unverified secrets" and in a quoted field name (3 answers);
* the number check flagged a correctly derived count, "Two" (1 answer, not
  changed here);

and it accepted two answers that exceed the prompt's own "at most N sentences".

``summarization_labelled_rows.json`` holds the 60 prompts, the 120 answers and the
careful verdict of each. These tests grade every answer with the real gate, built
the way production builds it, and pin the agreement count and each named case.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
    resolve_task_class_response_contract,
)

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "delegation"
    / "omn20489"
    / "summarization_labelled_rows.json"
)
_QWEN = "Qwen3.8-27B"
_GLM = "glm-5.3-flash"

# The grader as it stood agreed on 78 of 120; the three changes reach 99.
_MIN_AGREEMENT = 99
_AGREEMENT_BEFORE = 78

# Careful reading ACCEPTS these; the claims check refused them because the state
# word sat in the commit or PR body, on a different line from the ticket id.
_CLAIM_IN_BODY_CASES = (
    f"i43|{_QWEN}",
    f"i46|{_QWEN}",
    f"i50|{_QWEN}",
    f"i07|{_GLM}",
    f"i08|{_GLM}",
    f"i15|{_GLM}",
    f"i16|{_GLM}",
    f"i26|{_GLM}",
    f"i32|{_GLM}",
    f"i33|{_GLM}",
    f"i34|{_GLM}",
    f"i35|{_GLM}",
    f"i39|{_GLM}",
    f"i43|{_GLM}",
    f"i44|{_GLM}",
    f"i48|{_GLM}",
    f"i49|{_GLM}",
)
# Careful reading ACCEPTS these; the bare word "unverified" vetoed them.
_BARE_UNVERIFIED_CASES = (
    f"i18|{_QWEN}",
    f"i53|{_QWEN}",
    f"i18|{_GLM}",
    f"i53|{_GLM}",
)
# Careful reading REJECTS these for exceeding the prompt's sentence limit.
_OVER_SENTENCE_LIMIT_CASES = (
    f"i28|{_GLM}",
    f"i35|{_QWEN}",
)


def _load() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, object]]]:
    data = json.loads(_FIXTURE.read_text())
    return data["items"], {row["row_id"]: row for row in data["rows"]}


_ITEMS, _ROWS = _load()
_VERDICTS: dict[str, tuple[bool, list[str]]] = {}


def _grade(content: str, prompt: str, tag: str) -> tuple[bool, list[str]]:
    deterministic, heuristic = resolve_task_class_dod_checks(
        "summarization", prompt=prompt
    )
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid5(NAMESPACE_URL, tag),
            task_type="summarization",
            llm_response_content=content,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            quality_contract_mode="extend_task_class",
        ),
        response_contract=resolve_task_class_response_contract("summarization"),
        grounding_source=prompt,
    )
    return bool(result.passed), [
        str(r) for r in result.model_dump(mode="json")["failure_reasons"] or []
    ]


def _verdict(row_id: str) -> tuple[bool, list[str]]:
    if row_id not in _VERDICTS:
        row = _ROWS[row_id]
        _VERDICTS[row_id] = _grade(
            str(row["answer"]), _ITEMS[str(row["item"])]["prompt"], row_id
        )
    return _VERDICTS[row_id]


def test_fixture_is_the_120_labelled_answers() -> None:
    assert len(_ITEMS) == 60
    assert len(_ROWS) == 120
    assert {str(row["model"]) for row in _ROWS.values()} == {_QWEN, _GLM}


def test_grader_agrees_with_the_careful_reading_on_99_of_120() -> None:
    agree = sum(
        _verdict(row_id)[0] == bool(row["careful_accept"])
        for row_id, row in _ROWS.items()
    )
    assert agree >= _MIN_AGREEMENT, (
        f"{agree}/120 agree with the careful reading; was {_AGREEMENT_BEFORE}/120 "
        f"before OMN-20489, must be at least {_MIN_AGREEMENT}"
    )


@pytest.mark.parametrize("row_id", _CLAIM_IN_BODY_CASES)
def test_claim_found_in_the_commit_or_pr_body_is_grounded(row_id: str) -> None:
    assert _ROWS[row_id]["careful_accept"] is True
    passed, reasons = _verdict(row_id)
    assert passed, reasons


@pytest.mark.parametrize("row_id", _BARE_UNVERIFIED_CASES)
def test_bare_unverified_is_not_a_disclaimer(row_id: str) -> None:
    assert _ROWS[row_id]["careful_accept"] is True
    passed, reasons = _verdict(row_id)
    assert passed, reasons
    assert not any("disclaims accuracy" in reason for reason in reasons)


@pytest.mark.parametrize("row_id", _OVER_SENTENCE_LIMIT_CASES)
def test_answer_over_the_prompts_sentence_limit_is_rejected(row_id: str) -> None:
    assert _ROWS[row_id]["careful_accept"] is False
    passed, reasons = _verdict(row_id)
    assert not passed
    assert any("allows at most" in reason for reason in reasons), reasons


def test_an_invented_state_in_a_commit_body_is_still_refused() -> None:
    """The record is wider; a state the record does not hold is still invented."""
    prompt = (
        "Summarize the following commit range in at most 3 sentences.\n\n"
        "commit 0123456789abcdef0123456789abcdef01234567\n"
        "Author: A <a@example.com>\n\n"
        "    feat(OMN-12345): add the widget\n\n"
        "    Adds the widget behind a flag.\n\n"
        "commit 89abcdef0123456789abcdef0123456789abcdef\n"
        "Author: A <a@example.com>\n\n"
        "    fix(OMN-54321): rename a helper\n\n"
        "    The helper was renamed and all call sites were completed.\n"
    )
    invented = "OMN-12345 was completed and shipped, and it adds the widget."
    passed, reasons = _grade(invented, prompt, "invented")
    assert not passed
    assert any("OMN-12345" in reason for reason in reasons), reasons
    # The state that sits in the OTHER commit's body does not ground this one.
    grounded = "OMN-54321 renames a helper after all its call sites were completed."
    assert _grade(grounded, prompt, "grounded")[0]
