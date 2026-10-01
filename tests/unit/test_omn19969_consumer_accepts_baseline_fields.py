"""OMN-20274: consumer-first acceptance of the OMN-19969 savings baseline fields.

The released consumer models forbid extra keys, so a producer that starts
emitting ``baseline_source``, ``baseline_state`` or ``pricing_manifest_version``
is refused by the Wire Compatibility Gate until the consumer accepts them.
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
    "model",
    [ModelDelegateSkillResponse, ModelDelegateSkillCompleted],
)
def test_response_accepts_baseline_fields(
    model: type[ModelDelegateSkillResponse],
) -> None:
    parsed = model.model_validate(_response_payload())
    assert parsed.baseline_source == "session_model"
    assert parsed.baseline_state == "BASELINE_UNRESOLVED"
    assert parsed.pricing_manifest_version == 7


def test_failed_accepts_baseline_fields() -> None:
    payload = _response_payload()
    payload["status"] = "failed"
    parsed = ModelDelegateSkillFailed.model_validate(payload)
    assert parsed.baseline_source == "session_model"
    assert parsed.baseline_state == "BASELINE_UNRESOLVED"


def test_response_baseline_defaults_are_the_resolved_fixed_default() -> None:
    payload = _response_payload()
    del payload["baseline_source"], payload["baseline_state"]
    parsed = ModelDelegateSkillResponse.model_validate(payload)
    assert parsed.baseline_source == "fixed_default"
    assert parsed.baseline_state == "RESOLVED"


def test_response_rejects_unknown_baseline_state() -> None:
    payload = _response_payload()
    payload["baseline_state"] = "MAYBE"
    with pytest.raises(ValidationError):
        ModelDelegateSkillResponse.model_validate(payload)


def test_savings_projection_accepts_baseline_fields() -> None:
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
    assert parsed.baseline_source == "overlay"
    assert parsed.pricing_manifest_version == 3
