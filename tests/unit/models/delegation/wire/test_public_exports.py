"""Proof for delegation wire public exports."""

from __future__ import annotations

import pytest
from omnibase_core.models.delegation.wire.model_delegation_wire_request import (
    ModelDelegationRequest,
)
from omnibase_core.models.delegation.wire.model_task_delegated_event import (
    TASK_DELEGATED_TOPIC_V1,
)

import omnimarket.models.delegation.wire as wire


@pytest.mark.unit
def test_wire_all_exports_are_bound_attributes() -> None:
    """The package barrel must not advertise missing contract symbols."""
    missing = [name for name in wire.__all__ if not hasattr(wire, name)]

    assert missing == []


@pytest.mark.unit
def test_wire_exports_point_to_canonical_models() -> None:
    """Dependents can import stable DTOs from the wire package boundary."""
    assert wire.ModelDelegationRequest is ModelDelegationRequest
    assert wire.TASK_DELEGATED_TOPIC_V1 == TASK_DELEGATED_TOPIC_V1
    assert "extend_task_class" in wire.EnumQualityContractMode.__args__
    assert "replace_task_class" in wire.EnumQualityContractMode.__args__
