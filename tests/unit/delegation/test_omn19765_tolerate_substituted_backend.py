# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-side wire tests for ``ModelDelegateSkillAttemptRecord`` that hold at both steps (OMN-19765).

Step 1 (omnimarket#2979) made the model tolerate the wire key
``substituted_from_backend_id`` without declaring it (accept-and-discard via
``_FORTHCOMING_ATTEMPT_KEYS``), mirroring OMN-19436's
``finish_reason``/``truncated`` precedent. Step 2 (this same PR) declares
``substituted_from_backend_id: str | None = Field(default=None)`` and drops
the key from ``_FORTHCOMING_ATTEMPT_KEYS``, so it now decodes as a real
field rather than being discarded.

This file keeps only the tests that still hold at step 2: an unrelated
unknown key is still refused, and an attempt with no substitution still
decodes with the field at its default. The step-1-only tests (asserting the
key is discarded/absent from the dump) are removed. Their step-2 successors
live in ``tests/unit/delegation/test_omn19765_byok_substitution_attribution.py``.

The file is kept, not deleted, mirroring
``tests/unit/delegation/test_omn18931_no_escalation_consumer_first.py``: it
re-executes this exact path as the behaviour proof of omnimarket#2979's step-1
tolerance at every later OMN-19765 PR head.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)

pytestmark = pytest.mark.unit


def _attempt(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tier": "local",
        "backend_id": "cloud-glm",
        "model_id": "m",
        "quality_gate_passed": False,
    }
    payload.update(overrides)
    return payload


def test_an_unrelated_unknown_key_is_still_refused() -> None:
    """Control: a real named field is not relaxing ``extra=forbid`` generally."""
    with pytest.raises(ValidationError):
        ModelDelegateSkillAttemptRecord.model_validate(
            _attempt(substituted_from_backend_ids="cloud-glm")
        )


def test_a_record_from_before_this_key_still_decodes() -> None:
    """Control: an attempt with no substitution decodes with the field at its default."""
    record = ModelDelegateSkillAttemptRecord.model_validate(_attempt())
    assert record.substituted_from_backend_id is None
