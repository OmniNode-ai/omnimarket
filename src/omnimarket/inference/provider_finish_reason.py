# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provider's own truncation signal, parsed once for every path (OMN-18278).

WHY THIS EXISTS

    A local-tier delegation asked for a paragraph with a 350-token budget came
    back as the model's reasoning scratchpad and nothing else. The answer was
    never generated: the budget ran out while the model was still thinking, so
    the closing trace tag that OMN-18379's segmenter cuts at was never emitted,
    there was no boundary to strip, and the whole scratchpad became "the
    answer". The run reported ``completed`` with ``quality_gate_passed=true``
    and a score of ``1.0``. Reproduced four times on 2026-09-16 (lane
    ``delegation-dogfood-0045``; runs 1, 3, 10 and 11).

    The response had said so all along. ``choices[0].finish_reason`` was
    ``length`` — the provider stating that it stopped because the output budget
    ran out, not because the model was done. One of the two effect-boundary
    handlers already read that field and refused the call on it
    (``handler_inference_intent``); the handler the bus-less local path actually
    runs read ``choices[0].message.content`` and never looked at
    ``finish_reason`` at all, and the quality gate had no parameter through
    which the fact could have reached it.

ONE PARSE, THREE CONSUMERS

    This module is the single implementation. ``handler_inference_intent``
    refuses the provider call on it, ``handler_llm_delegation_call`` records it
    on its typed result so the local dispatch port can thread it into the gate,
    and the quality-gate reducer vetoes acceptance on it. Writing the
    comparison a second time is how the two effect handlers drifted apart in the
    first place.

WHAT IT DOES NOT DO

    It does not guess. A body that carries no ``finish_reason`` parses to
    :attr:`~omnimarket.enums.enum_provider_finish_reason.EnumProviderFinishReason.ABSENT`,
    which is a record that no signal accompanied the response — never a claim
    that the response completed, and never a claim that it was truncated. Only
    an explicit ``length`` vetoes anything.

    It is also not a completeness check. A response that ends cleanly under its
    budget can still be a bad answer, and one truncated at ``length`` can still
    contain a usable artifact; that judgement belongs to the gate's content
    checks. What this settles is the one case those checks provably cannot see:
    a transcript that stopped mid-thought and reads, to every textual heuristic,
    like well-formed prose.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason

#: The wire key an OpenAI-compatible provider reports the stop reason under.
_FINISH_REASON_KEY = "finish_reason"

#: The effect boundary's refusal message when a provider call is truncated.
#:
#: The literal ``finish_reason=length`` inside it is load-bearing, not
#: decoration: the bus orchestrator classifies an inference failure from its
#: TEXT (``handler_delegation_workflow._inference_error_failure_class``) and
#: matches on exactly that substring to resolve ``CONTEXT_TOO_LARGE``. Changing
#: the wording here silently reclassifies the failure.
TRUNCATED_RESPONSE_ERROR_MESSAGE = "API response truncated: finish_reason=length"

#: The gate check name recorded on a truncated response's rule evaluation.
TRUNCATION_CHECK_NAME = "not_truncated_by_output_budget"

#: The gate failure reason a truncated response carries.
#:
#: The ``WEAK_OUTPUT`` prefix is chosen deliberately over ``MALFORMED``. The
#: reducer escalates on ``REFUSAL`` / ``WEAK_OUTPUT`` / ``TASK_MISMATCH`` and
#: deliberately does not escalate on ``MALFORMED``, because a mid-token-truncated
#: code artifact is a structural defect a costlier tier is unlikely to fix. An
#: output-budget truncation is the opposite case: the next rung carries its own
#: budget and routinely does finish the answer, so this must climb rather than
#: terminalise.
TRUNCATED_RESPONSE_GATE_FAILURE_REASON = (
    "WEAK_OUTPUT: provider reported finish_reason=length -- the response was "
    "cut off by the output-token budget before the model finished generating, "
    "so this text is not a completed answer"
)


def parse_finish_reason(raw: object) -> EnumProviderFinishReason:
    """Classify one provider-reported finish reason.

    ``None`` and a missing field are :attr:`EnumProviderFinishReason.ABSENT`. A
    string the contract does not model is
    :attr:`EnumProviderFinishReason.UNRECOGNISED`, which is a distinct fact from
    ABSENT and is kept distinct so a new vendor value is visible rather than
    indistinguishable from silence.
    """
    if raw is None:
        return EnumProviderFinishReason.ABSENT
    if not isinstance(raw, str):
        return EnumProviderFinishReason.UNRECOGNISED
    try:
        return EnumProviderFinishReason(raw.strip().lower())
    except ValueError:
        return EnumProviderFinishReason.UNRECOGNISED


def finish_reason_from_choice(choice: Mapping[str, object]) -> EnumProviderFinishReason:
    """Read the finish reason off one OpenAI-compatible ``choices[]`` entry."""
    return parse_finish_reason(choice.get(_FINISH_REASON_KEY))


def is_truncated_by_output_budget(reason: EnumProviderFinishReason) -> bool:
    """Whether the provider said the output-token budget cut generation short.

    True for :attr:`EnumProviderFinishReason.LENGTH` and nothing else. ABSENT is
    not treated as truncation: a provider that reports no reason has told us
    nothing, and inferring truncation from silence would refuse every backend
    that omits the field.
    """
    return reason is EnumProviderFinishReason.LENGTH


__all__ = [
    "TRUNCATED_RESPONSE_ERROR_MESSAGE",
    "TRUNCATED_RESPONSE_GATE_FAILURE_REASON",
    "TRUNCATION_CHECK_NAME",
    "EnumProviderFinishReason",
    "finish_reason_from_choice",
    "is_truncated_by_output_budget",
    "parse_finish_reason",
]
