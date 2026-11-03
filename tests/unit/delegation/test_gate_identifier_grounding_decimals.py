# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""A correct 7+ digit decimal answer is not an ungrounded commit sha (OMN-22210).

The ``commit_sha`` identifier class required only "at least one digit" over
the hex alphabet, so any decimal number of seven or more digits matched it.
A correct arithmetic answer that does not occur in the prompt was then refused
UNGROUNDED, the run climbed the ladder and ended failed. The contract now also
requires at least one hex letter, so a pure decimal number is not read as a
sha, while a real hex sha absent from the source is still refused.

The receipts below are scrubbed: only the prompt shape and the answer values
the recorded runs carried are kept, the rest of the prompt is not.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.delegation.identifier_grounding import (
    evaluate_identifier_grounding,
    resolve_identifier_grounding_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_identifier_grounding import (
    ModelIdentifierGroundingPolicy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit

# The commit_sha pattern the contract declared before OMN-22210, kept here only
# to prove the recorded receipts are refused under it.
_PRE_FIX_COMMIT_SHA_PATTERN = (
    r"(?<![\w])(?P<token>(?=[0-9a-f]*\d)[0-9a-f]{7,40})(?![\w])"
)

# Scrubbed receipts: (run, prompt, correct answer). The prompts never contain
# the answer, which is the point -- the model computed it.
_RECEIPT_DA8E0025 = (
    "da8e0025",
    "H5: compute the product and reply with the number only.",
    "5736396",
)
_RECEIPT_FDCBBB0F = (
    "fdcbbb0f",
    "What is 1234 times 5678? Reply with the number only.",
    "7006652",
)
_RECEIPTS = (_RECEIPT_DA8E0025, _RECEIPT_FDCBBB0F)

_DOD_DETERMINISTIC: tuple[str, ...] = ("response_non_empty",)
_DOD_HEURISTIC: tuple[str, ...] = ("no_refusal", "identifiers_grounded")


def _gate_input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content=content,
        dod_deterministic=_DOD_DETERMINISTIC,
        dod_heuristic=_DOD_HEURISTIC,
    )


def _pre_fix_policy() -> ModelIdentifierGroundingPolicy:
    policy = resolve_identifier_grounding_policy()
    classes = tuple(
        cls.model_copy(update={"pattern": _PRE_FIX_COMMIT_SHA_PATTERN})
        if cls.class_name == "commit_sha"
        else cls
        for cls in policy.classes
    )
    return policy.model_copy(update={"classes": classes})


@pytest.mark.parametrize("answer", ["5736396", "7006652", "12345678901234"])
def test_decimal_not_sha_is_not_refused_ungrounded(answer: str) -> None:
    verdict = evaluate_identifier_grounding(
        content=f"The answer is {answer}.",
        grounding_source="Compute the product and reply with the number only.",
        policy=resolve_identifier_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert verdict.ungrounded == ()


@pytest.mark.parametrize("answer", ["5736396", "7006652"])
def test_decimal_not_sha_passes_the_gate_check(answer: str) -> None:
    result = delta(
        _gate_input(answer),
        grounding_source="Compute the product and reply with the number only.",
    )

    assert result.ungrounded_identifiers == ()
    assert not any(r.startswith("UNGROUNDED") for r in result.failure_reasons)


@pytest.mark.parametrize(
    "sha",
    [
        "7b42a4c",
        "a1b2c3d4e5",
        "7b42a4c6e1d0f9a8b7c6d5e4f3a2b1c0d9e8f7a6",
    ],
)
def test_hex_sha_still_refused_when_absent_from_source(sha: str) -> None:
    verdict = evaluate_identifier_grounding(
        content=f"The fix landed in {sha}.",
        grounding_source="Summarise the fix; no commit is named here.",
        policy=resolve_identifier_grounding_policy(),
    )

    assert [u.class_name for u in verdict.ungrounded] == ["commit_sha"]
    assert verdict.ungrounded[0].identifier == sha


def test_hex_sha_still_refused_by_the_gate() -> None:
    result = delta(
        _gate_input("The fix landed in 7b42a4c."),
        grounding_source="Summarise the fix; no commit is named here.",
    )

    assert any(r.startswith("UNGROUNDED") for r in result.failure_reasons)


def test_hex_sha_still_refused_control_grounded_sha_passes() -> None:
    """Positive control: the same sha present in the source is grounded."""
    verdict = evaluate_identifier_grounding(
        content="The fix landed in 7b42a4c.",
        grounding_source="The fix commit is 7b42a4c6e1d0f9a8b7c6d5e4f3a2b1c0d9e8f7a6.",
        policy=resolve_identifier_grounding_policy(),
    )

    assert verdict.checked_count == 1
    assert verdict.ungrounded == ()


@pytest.mark.parametrize(("run", "prompt", "answer"), _RECEIPTS)
def test_receipt_da8e0025_refused_on_pre_fix_contract(
    run: str, prompt: str, answer: str
) -> None:
    """RED anchor: the recorded correct answers were refused UNGROUNDED."""
    verdict = evaluate_identifier_grounding(
        content=answer, grounding_source=prompt, policy=_pre_fix_policy()
    )

    assert [u.identifier for u in verdict.ungrounded] == [answer], run


@pytest.mark.parametrize(("run", "prompt", "answer"), _RECEIPTS)
def test_receipt_da8e0025_accepted_on_fixed_contract(
    run: str, prompt: str, answer: str
) -> None:
    verdict = evaluate_identifier_grounding(
        content=answer,
        grounding_source=prompt,
        policy=resolve_identifier_grounding_policy(),
    )

    assert verdict.ungrounded == (), run
    result = delta(_gate_input(answer), grounding_source=prompt)
    assert not any(r.startswith("UNGROUNDED") for r in result.failure_reasons), run
