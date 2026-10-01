# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154: declared quota and attempt fields retain their wire values.

The consumer-first shims have been replaced by declared ``scope``,
``provider_id``, ``http_status`` and ``provider_code`` fields. These values
round-trip through the wire models; unrelated unknown keys remain forbidden.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaScope,
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


def test_declared_attempt_provider_facts_round_trip() -> None:
    provider_facts = {"provider_id": "zai", "http_status": 429, "provider_code": "1302"}
    assert provider_facts.keys() <= ModelDelegateSkillAttemptRecord.model_fields.keys()
    record = ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | provider_facts)
    assert record.failure_class == "rate_limited"
    assert record.provider_id == "zai"
    assert record.http_status == 429
    assert record.provider_code == "1302"
    payload = record.model_dump(mode="json")
    assert {key: payload[key] for key in provider_facts} == provider_facts
    assert (
        ModelDelegateSkillAttemptRecord.model_validate_json(record.model_dump_json())
        == record
    )


def test_an_attempt_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelDelegateSkillAttemptRecord.model_validate(_ATTEMPT | {"surprise": 1})


@pytest.mark.parametrize("scope", list(EnumQuotaScope))
def test_declared_quota_scope_round_trips(scope: EnumQuotaScope) -> None:
    assert "scope" in ModelQuotaCodeRule.model_fields
    rule = ModelQuotaCodeRule.model_validate(
        {"code": "429", "disposition": "retryable", "scope": scope.value}
    )
    assert rule.code == "429"
    assert rule.scope == scope
    assert rule.model_dump(mode="json")["scope"] == scope.value
    assert ModelQuotaCodeRule.model_validate_json(rule.model_dump_json()) == rule


def test_a_quota_rule_with_any_other_unknown_key_is_still_refused() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ModelQuotaCodeRule.model_validate(
            {"code": "429", "disposition": "retryable", "surprise": "x"}
        )
