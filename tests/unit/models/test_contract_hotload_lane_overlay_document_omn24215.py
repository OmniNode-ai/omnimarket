# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/runtime_contract_hotload/overlay.yaml through (OMN-24215)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_contract_hotload_lane_overlay import (
    ModelContractHotloadLaneOverlay,
)

pytestmark = pytest.mark.unit

ENABLED_ON_DEMAND_LANE = {
    "enabled": True,
    "mode": "on_demand",
    "allowed_environments": ["env_a"],
}


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "lanes": {"lane_a": ENABLED_ON_DEMAND_LANE}},
        {
            "schema_version": 1,
            "lanes": {
                "lane_a": {
                    "enabled": True,
                    "mode": "full_load",
                    "allowed_environments": ["env_a"],
                }
            },
        },
        {"schema_version": 1, "lanes": {"lane_a": {"enabled": False}}},
        # The runtime's own reader (omnibase_infra ModelContractHotloadLaneOverlay) accepts these two,
        # measured on its dev branch; a stricter model here would refuse a document the runtime takes.
        {"schema_version": 1, "lanes": {}},
        {"schema_version": 1, "lanes": {"": {"enabled": False}}},
        {
            "schema_version": 1,
            "lanes": {"lane_a": ENABLED_ON_DEMAND_LANE, "lane_b": {"enabled": False}},
        },
    ],
)
def test_a_valid_document_passes(document: dict[str, object]) -> None:
    overlay = ModelContractHotloadLaneOverlay.model_validate(document)
    assert overlay.schema_version == 1
    assert isinstance(overlay.lanes, dict)


def test_environment_names_are_trimmed_and_lowercased() -> None:
    overlay = ModelContractHotloadLaneOverlay.model_validate(
        {
            "schema_version": 1,
            "lanes": {
                "lane_a": {
                    "enabled": True,
                    "mode": "on_demand",
                    "allowed_environments": ["  ENV_A  "],
                },
            },
        }
    )
    assert overlay.lanes["lane_a"].allowed_environments == ("env_a",)


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "lanes": {"lane_a": {"enabled": False}}, "stray": 1},
        {"schema_version": 2, "lanes": {"lane_a": {"enabled": False}}},
        {"schema_version": 1, "lanes": {"lane_a": {"enabled": False, "limit": 5}}},
        {
            "schema_version": 1,
            "lanes": {"lane_a": {"enabled": False, "mode": "always"}},
        },
        {"schema_version": 1, "lanes": {"lane_a": {"enabled": True}}},
        {
            "schema_version": 1,
            "lanes": {
                "lane_a": {
                    "enabled": True,
                    "mode": "on_demand",
                    "allowed_environments": [],
                },
            },
        },
        {
            "schema_version": 1,
            "lanes": {
                "lane_a": {
                    "enabled": True,
                    "mode": "on_demand",
                    "allowed_environments": ["  "],
                },
            },
        },
    ],
)
def test_a_wrong_document_refuses(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelContractHotloadLaneOverlay.model_validate(document)


def test_the_shipped_row_shape() -> None:
    overlay = ModelContractHotloadLaneOverlay.model_validate(
        {
            "schema_version": 1,
            "lanes": {
                "lane_a": {
                    "enabled": True,
                    "mode": "on_demand",
                    "allowed_environments": ["env_a"],
                },
            },
        }
    )
    row = overlay.lanes["lane_a"]
    assert row.enabled is True
    assert row.mode == "on_demand"


LAZY_START = {
    "ingress_contract": "ingress_a",
    "boot_contracts": ["boot_a", "boot_b"],
    "budget_seconds": 45,
    "max_retries": 2,
}


def lazy_document(lazy_start: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "lanes": {"lane_a": {**ENABLED_ON_DEMAND_LANE, "lazy_start": lazy_start}},
    }


def test_a_lane_row_accepts_a_lazy_start_block() -> None:
    row = ModelContractHotloadLaneOverlay.model_validate(
        lazy_document(LAZY_START)
    ).lanes["lane_a"]
    assert row.lazy_start is not None
    assert row.lazy_start.ingress_contract == "ingress_a"
    assert row.lazy_start.boot_contracts == ("boot_a", "boot_b")
    assert (row.lazy_start.budget_seconds, row.lazy_start.max_retries) == (45, 2)


def test_a_lazy_start_block_takes_the_runtime_defaults() -> None:
    row = ModelContractHotloadLaneOverlay.model_validate(
        lazy_document({"ingress_contract": "ingress_a"})
    ).lanes["lane_a"]
    assert row.lazy_start is not None
    assert row.lazy_start.boot_contracts == ()
    assert (row.lazy_start.budget_seconds, row.lazy_start.max_retries) == (30.0, 3)


def test_a_row_with_no_lazy_start_block_holds_none() -> None:
    row = ModelContractHotloadLaneOverlay.model_validate(
        {"schema_version": 1, "lanes": {"lane_a": ENABLED_ON_DEMAND_LANE}}
    ).lanes["lane_a"]
    assert row.lazy_start is None


def test_a_lazy_start_block_with_zero_retries_is_accepted() -> None:
    row = ModelContractHotloadLaneOverlay.model_validate(
        lazy_document({**LAZY_START, "max_retries": 0})
    ).lanes["lane_a"]
    assert row.lazy_start is not None
    assert row.lazy_start.max_retries == 0


@pytest.mark.parametrize(
    "lazy_start",
    [
        {"boot_contracts": ["boot_a"]},
        {**LAZY_START, "ingress_contract": ""},
        {**LAZY_START, "ingress_contract": "  "},
        {**LAZY_START, "boot_contracts": ["boot_a", " "]},
        {**LAZY_START, "boot_contracts": ["boot_a", "boot_a"]},
        {**LAZY_START, "budget_seconds": 0},
        {**LAZY_START, "budget_seconds": -1},
        {**LAZY_START, "max_retries": -1},
        {**LAZY_START, "stray": 1},
    ],
)
def test_a_wrong_lazy_start_block_refuses(lazy_start: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelContractHotloadLaneOverlay.model_validate(lazy_document(lazy_start))
