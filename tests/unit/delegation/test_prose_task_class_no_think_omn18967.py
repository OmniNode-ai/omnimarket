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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from omnimarket.inference.protocol_config import apply_inference_protocol

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
# These are NOT an allowlist of convenience: for these classes the reasoning is
# arguably the product, and suppressing it would be a silent product change
# rather than a defect fix.  A response from one of these that carries a
# preamble and no resolvable answer region is handled by the response seam's
# typed refusal, not by suppression here.
REASONING_IS_THE_DELIVERABLE: Final[dict[str, str]] = {
    "reasoning": (
        "the chain of thought is the requested artifact; suppressing it would "
        "change what the caller asked for"
    ),
    "complex_reasoning": (
        "same as `reasoning`, and the class exists precisely to buy more "
        "deliberation than the default"
    ),
}


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
