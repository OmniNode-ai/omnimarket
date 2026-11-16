# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23605: the lane the chain canary grades declares its tenant_id.

Step A1 of the HTTP API removal: the chain canary reads the lane's tenant from
the lane table and reports RED when it is absent. These tests pin that exactly
one lane declares a tenant, that the value is an identifier (a UUID, not a
credential-shaped string), and that it is the house tenant the dev lane's
compose file stamps. They name no lane id: a public repository carries no lab
lane id in a line it adds, so the declaring lane is found by the key it carries.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_OVERLAY = _REPO / "config" / "ci_bus_lanes.yaml"

_TENANT_ID = "820272f9-4aaf-5add-a2df-0af942852ab2"
_TENANT_KEY = "tenant_id"


def _lanes() -> dict[str, dict[str, object]]:
    lanes = yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))["lanes"]
    assert isinstance(lanes, dict)
    return lanes


def _declaring_lanes() -> dict[str, object]:
    return {
        lane: declaration[_TENANT_KEY]
        for lane, declaration in _lanes().items()
        if _TENANT_KEY in declaration
    }


def test_exactly_one_lane_declares_a_tenant() -> None:
    # The tenant is declared per lane only where a consumer needs it; a tenant
    # on a lane with no consuming canary leg is a declaration with no reader.
    assert len(_declaring_lanes()) == 1


def test_the_declared_tenant_is_the_house_tenant_uuid() -> None:
    (tenant_id,) = _declaring_lanes().values()
    assert isinstance(tenant_id, str)
    assert uuid.UUID(tenant_id) == uuid.UUID(_TENANT_ID)
    assert tenant_id == _TENANT_ID


def test_the_declaring_lane_has_a_real_broker() -> None:
    # The canary publishes to this lane's bus, so an in-memory lane is no home
    # for the tenant.
    (lane,) = _declaring_lanes()
    assert _lanes()[lane]["broker"] != "inmemory"


def test_the_tenant_is_a_value_not_a_credential() -> None:
    (tenant_id,) = _declaring_lanes().values()
    assert isinstance(tenant_id, str)
    assert "://" not in tenant_id
    assert "@" not in tenant_id
