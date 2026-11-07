# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22474 AC3: the pinned consumer decodes every attempt key this producer emits.

omniclaude pins omnimarket at dev-20261104060946-f745fee0e016. Its
``ModelDelegateSkillAttemptRecord`` forbids extra keys, so it decodes an
attempt key only when it declares the field or lists it in
``_FORTHCOMING_ATTEMPT_KEYS``. The two sets below are read from that commit's
``model_delegate_skill_response.py``.

Failure modes, each with a test below:
W1 a customer-key attempt's ``secret_ref`` never reaches the wire, so the
   pinned consumer never sees the key the run used.
W2 the producer emits an attempt key the pinned consumer neither declares nor
   lists as forthcoming, so it refuses the whole terminal.
"""

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)

pytestmark = pytest.mark.unit

PINNED_TAG = "dev-20261104060946-f745fee0e016"
PINNED_DECLARED = frozenset(
    {
        "acceptance_decision",
        "acceptance_detail",
        "acceptance_reason",
        "attempt_id",
        "attempt_kind",
        "backend_id",
        "configuration",
        "cost_usd",
        "error_message",
        "failure_class",
        "finish_reason",
        "host",
        "http_status",
        "input_token_budget",
        "input_tokens_measured",
        "model_id",
        "parent_attempt_id",
        "provider_code",
        "provider_id",
        "quality_gate_passed",
        "quality_score",
        "reasoning_preamble",
        "reasoning_preamble_rule",
        "rubric_verdict",
        "size_band",
        "split_id",
        "substituted_from_backend_id",
        "tier",
        "truncated",
        "usage_source",
    }
)
PINNED_FORTHCOMING = frozenset({"backend_pinned", "gate_record", "secret_ref"})
PINNED_DECODES = PINNED_DECLARED | PINNED_FORTHCOMING

X = "cred_tenant-a_openrouter_" + "a" * 32


def _attempt(**extra: object) -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord.model_validate(
        {
            "tier": "hosted",
            "backend_id": "openrouter",
            "model_id": "qwen/qwen3-coder",
            "quality_gate_passed": True,
            **extra,
        }
    )


def test_wire_customer_key_attempt_reaches_the_pinned_consumer() -> None:
    dumped = _attempt(secret_ref=X).model_dump(mode="json")
    assert dumped.get("secret_ref") == X, (
        f"the attempt's key ref is not on the wire for the consumer at {PINNED_TAG}"
    )
    assert set(dumped) <= PINNED_DECODES, sorted(set(dumped) - PINNED_DECODES)


def test_wire_attempt_without_a_key_stays_inside_the_pinned_vocabulary() -> None:
    dumped = _attempt().model_dump(mode="json")
    assert "secret_ref" not in dumped
    assert set(dumped) <= PINNED_DECODES, sorted(set(dumped) - PINNED_DECODES)
