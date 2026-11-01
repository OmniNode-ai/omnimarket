# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The installed core pin accepts machine principals through Market's overlay reader."""

from __future__ import annotations

import pytest
from omnibase_core.models.config_overlay import ModelDeveloperPrincipalOverlay

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)

pytestmark = pytest.mark.unit


def test_installed_core_reads_machine_and_legacy_principals() -> None:
    document = {
        "schema_version": "developer_principal.v1",
        "grants_source": "deployment-profile",
        "principals": [
            {
                "principal": "build-agent",
                "login": "build-service",
                "device": "build-host",
                "role": "machine",
            },
            {
                "principal": "developer-workstation",
                "login": "developer",
                "device": "workstation",
            },
        ],
    }

    overlay = load_structured_overlay(
        "developer.principal", ModelDeveloperPrincipalOverlay, explicit=document
    )
    serialized = overlay.model_dump(mode="json")
    assert serialized["grants_source"] == "deployment-profile"
    assert [row["role"] for row in serialized["principals"]] == [
        "machine",
        "developer",
    ]
    assert ModelDeveloperPrincipalOverlay.model_validate(serialized) == overlay


def test_installed_core_round_trips_machine_grant_role_bindings() -> None:
    document = {
        "schema_version": "developer_principal.v1",
        "grants_source": "deployment-profile",
        "principals": [
            {
                "principal": "build-agent",
                "login": "build-service",
                "device": "build-host",
                "role": "machine",
                "grant_roles": ["fleet-member", "fix-listener"],
            },
            {
                "principal": "developer-workstation",
                "login": "developer",
                "device": "workstation",
            },
        ],
    }

    overlay = load_structured_overlay(
        "developer.principal", ModelDeveloperPrincipalOverlay, explicit=document
    )

    assert overlay.principals[0].grant_roles == ("fleet-member", "fix-listener")
    assert overlay.principals[1].grant_roles == ()
    serialized = overlay.model_dump(mode="json")
    assert serialized["principals"][0]["grant_roles"] == [
        "fleet-member",
        "fix-listener",
    ]
    assert ModelDeveloperPrincipalOverlay.model_validate(serialized) == overlay


@pytest.mark.parametrize(
    ("role", "grant_roles"),
    [
        ("developer", ["fleet-member"]),
        ("machine", ["fleet-member", "fleet-member"]),
        ("machine", [{"topic": "private", "operation": "WRITE"}]),
    ],
)
def test_market_refuses_invalid_grant_role_bindings(
    role: str, grant_roles: list[object]
) -> None:
    with pytest.raises(NodeOverlayError, match="principals"):
        load_structured_overlay(
            "developer.principal",
            ModelDeveloperPrincipalOverlay,
            explicit={
                "schema_version": "developer_principal.v1",
                "grants_source": "deployment-profile",
                "principals": [
                    {
                        "principal": "build-agent",
                        "login": "build-service",
                        "device": "build-host",
                        "role": role,
                        "grant_roles": grant_roles,
                    }
                ],
            },
        )


@pytest.mark.parametrize("extra", [{"role": "admin"}, {"grants": ["admin"]}])
def test_principal_role_does_not_grant_authority(extra: dict[str, object]) -> None:
    with pytest.raises(NodeOverlayError, match="principals"):
        load_structured_overlay(
            "developer.principal",
            ModelDeveloperPrincipalOverlay,
            explicit={
                "schema_version": "developer_principal.v1",
                "grants_source": "deployment-profile",
                "principals": [
                    {
                        "principal": "build-agent",
                        "login": "build-service",
                        "device": "build-host",
                        **extra,
                    }
                ],
            },
        )
