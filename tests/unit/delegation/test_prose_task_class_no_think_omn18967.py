# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18967: the prose task classes must reach the provider with thinking off.

Measured 2026-09-21 against the live lab inference endpoint (vLLM 0.27.1,
``Qwen3.8-27B``), read-only:

* the reasoning trace arrives **inline in** ``message.content`` **with no tags at
  all** and ``message.reasoning`` is ``null``, because the server runs without a
  reasoning parser.  The OMN-18379 boundary rule leads with
  ``unpaired_closing_tag``, so on this shape it cannot fire and the trace is
  handed to the caller as the answer;
* the same document-class prompt with ``chat_template_kwargs:
  {enable_thinking: false}`` returned 461 chars opening directly with the
  deliverable, against 1650 chars opening ``We need to respond to user:``.

The suppression mechanism already exists and is contract-declared in
``inference_protocols.v1.yaml``.  It does not fire for prose because
``_profile_matches`` makes ``task_type`` authoritative and short-circuiting
(``protocol_config.py``): when a profile declares ``task_types`` and the caller
supplied one, membership decides and ``system_prompt_contains`` is never
consulted.  Ten of the sixteen slugs in the delegate contract's
``allowed_task_types`` matched no thinking-suppressing profile.

These tests assert the gap **per task class rather than in aggregate**, so a
future slug added to the contract cannot silently land on the thinking-on path:
every slug must either resolve to a thinking-suppressing profile or appear in
:data:`REASONING_IS_THE_DELIVERABLE` with a recorded reason.

OMN-19267 emptied that exclusion table.  ``reasoning`` and ``complex_reasoning``
were on it, and on the same endpoint they returned an EMPTY answer: the model
wrote its untagged trace, often skipped the ``### ANSWER`` marker line, and the
deliverable extractor refused the response and blanked it.  Measured
2026-09-23, six prompts per class: thinking on blanked 3/6 (``reasoning``) and
2/6 (``complex_reasoning``); thinking off blanked 1/6 and 0/6.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    _build_messages_and_request_options,
    _merge_provider_request_options,
)

# The local tier this defect was measured on.  The profiles match on
# ``model_name_patterns``, so the model name is load-bearing for the assertion.
LOCAL_MODEL: Final[str] = "Qwen3.8-27B"

DELEGATE_CONTRACT: Final[Path] = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)

# Task classes deliberately left on the thinking-on path, each with the reason.
#
# These are NOT an allowlist of convenience.  An entry needs a class whose
# caller actually RECEIVES the reasoning.  OMN-18967 listed `reasoning` and
# `complex_reasoning` here on the grounds that the deliberation is the product,
# but both declare a `### ANSWER` start marker and the extractor drops
# everything before it, so with thinking on the deliberation never reached the
# caller.  With thinking off the model writes its reasoning inside the
# deliverable, where the caller does receive it (OMN-19267).
REASONING_IS_THE_DELIVERABLE: Final[dict[str, str]] = {}


def _allowed_task_types() -> list[str]:
    """Read the delegate surface's own declared task classes.

    Read from the contract rather than hardcoded so a slug added there is
    covered by this test on the commit that adds it.
    """

    contract = yaml.safe_load(DELEGATE_CONTRACT.read_text(encoding="utf-8"))
    declared = contract["allowed_task_types"]
    assert isinstance(declared, list), (
        f"{DELEGATE_CONTRACT} allowed_task_types is {type(declared).__name__}, "
        "not a list; this test cannot sweep it"
    )
    assert declared, (
        f"{DELEGATE_CONTRACT} declares no allowed_task_types; this test's "
        "positive control is gone, so a green run would prove nothing"
    )
    return [str(slug) for slug in declared]


def _request_options_for(task_type: str) -> dict[str, Any]:
    _, _, request_options = apply_inference_protocol(
        system_prompt="You are a helpful assistant.",
        prompt="Write a four-sentence ticket body describing a projection defect.",
        model=LOCAL_MODEL,
        task_type=task_type,
    )
    return request_options


def _thinking_is_suppressed(request_options: dict[str, Any]) -> bool:
    kwargs = request_options.get("chat_template_kwargs")
    return isinstance(kwargs, dict) and kwargs.get("enable_thinking") is False


@pytest.mark.unit
def test_the_contract_declares_the_task_classes_this_test_sweeps() -> None:
    """Positive control: the sweep below is reading a non-empty, real list."""

    declared = _allowed_task_types()
    assert "document" in declared
    assert "documentation" in declared


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _allowed_task_types())
def test_every_allowed_task_class_suppresses_thinking_or_is_a_declared_exclusion(
    task_type: str,
) -> None:
    """Each declared task class resolves to thinking-off, or is excluded on record.

    This is the AC2 table.  It fails per slug, so the failure names the class
    that would leak rather than reporting one opaque aggregate.
    """

    request_options = _request_options_for(task_type)
    suppressed = _thinking_is_suppressed(request_options)

    if task_type in REASONING_IS_THE_DELIVERABLE:
        assert not suppressed, (
            f"task class {task_type!r} is recorded in "
            "REASONING_IS_THE_DELIVERABLE as deliberately thinking-on, but a "
            "profile now suppresses its reasoning. Either the exclusion is "
            "stale and should be removed with its reason, or a profile "
            "over-matched. Resolved request options: "
            f"{request_options!r}"
        )
        return

    assert suppressed, (
        f"task class {task_type!r} reaches the provider with reasoning ON, so "
        "the model's trace is emitted inline in message.content ahead of the "
        "answer and the caller receives the preamble. Measured on the lab "
        "endpoint 2026-09-21: 1650 chars opening 'We need to respond to user:' "
        "against 461 clean chars with enable_thinking=false. Either add this "
        "slug to a declared profile in inference_protocols.v1.yaml, or record "
        "it in REASONING_IS_THE_DELIVERABLE with the reason. Resolved request "
        f"options: {request_options!r}"
    )


@pytest.mark.unit
def test_document_class_is_the_measured_regression() -> None:
    """The exact class that leaked in the dogfood runs, asserted on its own.

    Kept separate from the table so this defect keeps a named test even if the
    contract's task-class list is later restructured.
    """

    options = _request_options_for("document")
    assert _thinking_is_suppressed(options), (
        "the `document` task class is the one measured leaking a reasoning "
        f"preamble to the caller; resolved request options: {options!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["reasoning", "complex_reasoning"])
def test_reasoning_classes_are_the_measured_empty_answer_regression(
    task_type: str,
) -> None:
    """The classes that returned an empty answer, asserted on their own (OMN-19267).

    Runs 621c6a72 and 253031bb: every local attempt was blanked because the
    thinking-on response carried no marker line.  Kept separate from the table
    so the defect keeps a named test if the exclusion table returns.
    """

    options = _request_options_for(task_type)
    assert _thinking_is_suppressed(options), (
        f"the `{task_type}` task class reached the local model with thinking "
        "on, which returned an untagged trace with no `### ANSWER` marker and "
        "was blanked by the deliverable extractor; resolved request options: "
        f"{options!r}"
    )


@pytest.mark.unit
def test_the_exclusions_are_real_task_classes() -> None:
    """A stale exclusion must not silently exempt nothing.

    If a slug is removed from the contract, its exclusion entry becomes dead
    and would quietly shrink this test's coverage.
    """

    declared = set(_allowed_task_types())
    unknown = sorted(set(REASONING_IS_THE_DELIVERABLE) - declared)
    assert not unknown, (
        "REASONING_IS_THE_DELIVERABLE names task classes the delegate contract "
        f"no longer declares: {unknown}. Remove the dead entries."
    )


@pytest.mark.unit
@pytest.mark.usefixtures("stub_provider_quota_reader")
@pytest.mark.parametrize(
    "task_type",
    [
        "document",
        "documentation",
        "summarization",
        "research",
        "review",
        "code_review",
        "planning",
        "escalation",
    ],
)
def test_prose_suppression_survives_the_bus_intent_and_provider_boundary(
    task_type: str,
) -> None:
    """Profile matching alone does not prove that thinking is off at egress.

    Exercise the orchestrator and the inference effect's request builders,
    with a wire round-trip between them. No provider response, extraction or
    grading is involved: the assertion is on the outbound provider options.
    """
    workflow = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = ModelDelegationRequest(
        prompt="Write a four-sentence ticket body describing a projection defect.",
        system_prompt="You are a helpful assistant.",
        task_type=task_type,
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )
    assert workflow.handle_delegation_request(request)
    decision = ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=task_type,
        selected_model=LOCAL_MODEL,
        selected_backend_id=uuid4(),
        endpoint_url="http://test-local-prose:8000/v1/chat/completions",
        cost_tier="local",
        max_context_tokens=32768,
        max_tokens=512,
        system_prompt="You are a helpful assistant.",
        rationale="Local prose provider-boundary regression.",
        tier_name="local",
        route="local-heavy-reasoning",
        provider="local",
    )
    intents = workflow.handle_routing_decision(decision)
    assert len(intents) == 1
    assert isinstance(intents[0], ModelInferenceIntent)
    intent = ModelInferenceIntent.model_validate_json(intents[0].model_dump_json())

    messages, options = _build_messages_and_request_options(intent)
    payload = _merge_provider_request_options(
        {"model": intent.model, "messages": messages}, options
    )

    assert intent.correlation_id == correlation_id
    assert intent.base_url == decision.endpoint_url
    assert intent.model == LOCAL_MODEL
    assert _thinking_is_suppressed(payload), payload
    assert payload["messages"][1]["content"].count("/no_think") == 1
