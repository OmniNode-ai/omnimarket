# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model the overlay home validates a lab broker access declaration through (OMN-21611)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.overlay_documents.model_broker_acl_declaration import (
    ModelBrokerAclDeclarationOverlay,
)

pytestmark = pytest.mark.unit

GRANT = {
    "resource_type": "TOPIC",
    "pattern": "LITERAL",
    "name": "onex.cmd.example.v1",
    "operation": "WRITE",
}
PRINCIPAL = {"principal": "User:lane-login", "role": "a lane login", "grants": [GRANT]}
DECLARATION: dict[str, Any] = {
    "lane": "example-lane",
    "host": "h051",
    "compose_project": "project",
    "broker_container": "broker",
    "superuser_source_container": "sasl-enable",
    "principals": [PRINCIPAL],
}


def test_loads_the_way_the_overlay_home_loads_it(tmp_path: Path) -> None:
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "overlay.yaml").write_text(
        yaml.safe_dump(DECLARATION), encoding="utf-8"
    )
    overlay = load_structured_overlay(
        "doc",
        ModelBrokerAclDeclarationOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    assert overlay.principals[0].grants[0].operation == "WRITE"


@pytest.mark.parametrize(
    "document",
    [
        {**DECLARATION, "principals": []},
        {**DECLARATION, "stray": 1},
        {**DECLARATION, "principals": [PRINCIPAL, PRINCIPAL]},
        {**DECLARATION, "principals": [{**PRINCIPAL, "principal": "User:*"}]},
        {**DECLARATION, "principals": [{**PRINCIPAL, "principal": "lane-login"}]},
        {**DECLARATION, "principals": [{**PRINCIPAL, "grants": []}]},
        {**DECLARATION, "principals": [{**PRINCIPAL, "grants": [GRANT, GRANT]}]},
        {
            **DECLARATION,
            "principals": [
                {**PRINCIPAL, "grants": [{**GRANT, "operation": "ALTER"}]},
            ],
        },
        {
            **DECLARATION,
            "principals": [
                {**PRINCIPAL, "grants": [{**GRANT, "operation": "DELETE"}]},
            ],
        },
        {
            **DECLARATION,
            "principals": [{**PRINCIPAL, "grants": [{**GRANT, "name": "*"}]}],
        },
        {
            **DECLARATION,
            "principals": [
                {
                    **PRINCIPAL,
                    "grants": [
                        {
                            "resource_type": "CLUSTER",
                            "pattern": "LITERAL",
                            "name": "other",
                            "operation": "DESCRIBE",
                        }
                    ],
                }
            ],
        },
    ],
)
def test_refuses_a_row_the_tool_would_refuse(document: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelBrokerAclDeclarationOverlay.model_validate(document)


def test_a_group_delete_row_is_accepted() -> None:
    row = {
        "resource_type": "GROUP",
        "pattern": "PREFIXED",
        "name": "local.example_",
        "operation": "DELETE",
    }
    overlay = ModelBrokerAclDeclarationOverlay.model_validate(
        {**DECLARATION, "principals": [{**PRINCIPAL, "grants": [row]}]}
    )
    assert overlay.principals[0].grants[0].resource_type == "GROUP"
