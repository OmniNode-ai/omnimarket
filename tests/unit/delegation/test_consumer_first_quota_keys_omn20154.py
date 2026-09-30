# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154, consumer first: a released consumer must decode the next shape.

The change after this one stamps ``provider_id``, ``http_status`` and
``provider_code`` onto every delegation attempt and declares ``scope`` on each
provider quota rule. Both models are ``extra="forbid"``, so a consumer released
without these keys would dead-letter every terminal (OMN-18852) and refuse the
policy. This release accepts exactly these keys, discards them, and still
refuses any other unknown key.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelQuotaCodeRule,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)

pytestmark = pytest.mark.unit

_ATTEMPT = {
    "tier": "cheap_cloud",
    "backend_id": "cloud-glm-5-3",
    "model_id": "glm-5.3",
    "quality_gate_passed": False,
    "failure_class": "rate_limited",
}


def test_an_attempt_carrying_the_provider_facts_decodes() -> None:
    record = ModelDelegateSkillAttemptRecord.model_validate(
        _ATTEMPT | {"provider_id": "zai", "http_status": 429, "provider_code": "1302"}
    )
    assert record.failure_class == "rate_limited"


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"surprise": 1})


def test_a_quota_rule_carrying_scope_decodes() -> None:
    rule = ModelQuotaCodeRule.model_validate(
        {"code": "429", "disposition": "retryable", "scope": "model"}
    )
    assert rule.code == "429"


def test_a_quota_rule_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelQuotaCodeRule.model_validate(
            {"code": "429", "disposition": "retryable", "surprise": "x"}
        )
