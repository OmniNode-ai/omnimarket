"""OMN-20274: consumer-first decoding of the OMN-19969 savings baseline keys.

The released consumer models forbid extra keys, and the Wire Compatibility Gate
refuses a PR that DECLARES a new wire field while the last release forbids it.
So step 1 tolerates the keys without declaring them, and omnimarket#3172
declares them after a release carries this.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
)

pytestmark = pytest.mark.unit


def _response_payload() -> dict[str, object]:
    return {
        "status": "completed",
        "correlation_id": str(uuid4()),
        "task_type": "test",
        "baseline_source": "session_model",
        "baseline_state": "BASELINE_UNRESOLVED",
        "pricing_manifest_version": 7,
    }


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillCompleted]
)
def test_response_decodes_the_baseline_keys(
    model: type[ModelDelegateSkillResponse],
) -> None:
    parsed = model.model_validate(_response_payload())
    assert parsed.pricing_manifest_version == 7


def test_failed_decodes_the_baseline_keys() -> None:
    payload = _response_payload()
    payload["status"] = "failed"
    assert ModelDelegateSkillFailed.model_validate(payload).status == "failed"


def test_response_still_refuses_an_unknown_key() -> None:
    payload = _response_payload()
    payload["not_a_real_key"] = "x"
    with pytest.raises(ValidationError):
        ModelDelegateSkillResponse.model_validate(payload)


def test_savings_projection_decodes_the_baseline_keys() -> None:
    parsed = ModelDelegateSkillSavingsProjection.model_validate(
        {
            "event_timestamp": datetime.now(UTC),
            "session_id": str(uuid4()),
            "model_local": "local",
            "model_cloud_baseline": "cloud",
            "baseline_source": "overlay",
            "pricing_manifest_version": 3,
            "local_cost_usd": Decimal("0.01"),
            "cloud_cost_usd": Decimal("0.02"),
            "savings_usd": Decimal("0.01"),
        }
    )
    assert parsed.savings_usd == Decimal("0.01")


def test_savings_projection_still_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ModelDelegateSkillSavingsProjection.model_validate(
            {
                "event_timestamp": datetime.now(UTC),
                "session_id": str(uuid4()),
                "model_local": "local",
                "model_cloud_baseline": "cloud",
                "not_a_real_key": "x",
                "local_cost_usd": Decimal("0.01"),
                "cloud_cost_usd": Decimal("0.02"),
                "savings_usd": Decimal("0.01"),
            }
        )
