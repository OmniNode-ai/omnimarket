# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A length-constrained prose prompt declares a shape (OMN-17547).

Live case, collaborator lane 2026-09-02T11:58:33Z, correlation
109c2285-639b-43d7-8b80-2e589c5d84ee: ``Summarize in one sentence what a
Bifrost lane overlay does.`` under ``--task-type research`` was answered
correctly in one sentence and failed on ``cites_sources`` and
``methodical_analysis``, because "in one sentence" matched no declared
directive and resolved ``unconstrained``.
"""

from __future__ import annotations

import uuid

import pytest

from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_requested_shape_for_prompt,
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

LIVE_PROMPT = "Summarize in one sentence what a Bifrost lane overlay does."
LIVE_SHAPED_ANSWER = (
    "A Bifrost lane overlay layers lane-specific routing settings over the base "
    "gateway configuration so each lane resolves its own model endpoints."
)


@pytest.mark.parametrize(
    "prompt",
    [
        LIVE_PROMPT,
        "Explain in a sentence or two how the ledger lock works.",
        "Briefly, describe the runtime train.",
        "Describe the deploy agent in 30 words or fewer.",
    ],
)
def test_resolves_length_constrained(prompt: str) -> None:
    """AC1: prose length directives resolve to the declared shape."""
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.LENGTH_CONSTRAINED
    )


def test_resolves_member_value_is_stable() -> None:
    """AC1: the wire value of the new member is fixed."""
    assert EnumRequestedResponseShape.LENGTH_CONSTRAINED.value == "length_constrained"


def test_override_band_is_short_form() -> None:
    """AC2: citation and analysis-structure checks are dropped, adequacy kept."""
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    assert det == ("response_non_empty",)
    assert heur == ("no_refusal", "short_form_adequacy")


def test_override_exact_literal_still_wins() -> None:
    """AC2: a literal directive keeps precedence over a length directive."""
    assert (
        resolve_requested_shape_for_prompt(
            "Briefly: reply with exactly the word: alive"
        )
        is EnumRequestedResponseShape.EXACT_LITERAL
    )


def test_bifrost_live_case_passes_research_gate() -> None:
    """AC3: the one-sentence answer to the live prompt passes the research gate."""
    det, heur = resolve_task_class_dod_checks("research", prompt=LIVE_PROMPT)
    result = delta(
        ModelQualityGateInput(
            correlation_id=str(uuid.uuid4()),
            task_type="research",
            llm_response_content=LIVE_SHAPED_ANSWER,
            dod_deterministic=det,
            dod_heuristic=heur,
        )
    )
    assert result.passed is True, result.failure_reasons


@pytest.mark.parametrize(
    "prompt",
    [
        "Analyse why the style guide requires one sentence per line in docs.",
        "Discuss whether a brief outage justifies a rollback, citing sources.",
        "Compare the 30 words or fewer limit in tweets to headline limits.",
    ],
)
def test_mentions_do_not_constrain(prompt: str) -> None:
    """AC4: prose that merely mentions a length does not match."""
    assert (
        resolve_requested_shape_for_prompt(prompt)
        is EnumRequestedResponseShape.UNCONSTRAINED
    )
