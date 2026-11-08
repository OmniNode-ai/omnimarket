# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The power node's iLO port (OMN-22906, Tessl review finding): one composition owner, reads need no ledger."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from omnimarket.models.ilo_redfish.enum_ilo_redfish import (
    EnumIloOutcome,
    EnumIloPowerAction,
    EnumIloRefusal,
    EnumIloVerb,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_host_power_effect.handlers import ilo_port_runtime
from omnimarket.nodes.node_host_power_effect.handlers.ilo_port_runtime import (
    build_ilo_port,
)
from omnimarket.nodes.node_host_power_effect.models.model_host_power_overlay import (
    ModelHostPowerOverlay,
)

pytestmark = pytest.mark.unit

LEDGER_ENV = "POWER_TEST_LEDGER_PATH"


def _power_overlay() -> ModelHostPowerOverlay:
    return ModelHostPowerOverlay.model_validate(
        {
            "backend": "ilo_redfish",
            "writes_need_approval": True,
            "ilo_ledger_path_env": LEDGER_ENV,
            "hosts": [
                {
                    "name": "h8",
                    "power_route": "ilo_redfish",
                    "allowed_actions": ["status", "on"],
                }
            ],
        }
    )


def _ilo_root(tmp_path: Path) -> dict[str, str]:
    node_dir = tmp_path / "node_ilo_redfish_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "journal_dir": str(tmp_path / "journal"),
                "hosts": [
                    {
                        "name": "h8",
                        "generation": "gen8",
                        "address": "192.0.2.8",
                        "expected_product": "DL380p Gen8",
                        "expected_serial": "SERIAL0001",
                        "tls": {"ca_file": "certs/test.pem", "sha256": "a" * 64},
                        "live": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_the_port_builds_without_a_ledger_because_a_read_needs_no_approval(
    tmp_path: Path,
) -> None:
    port = build_ilo_port(_power_overlay(), _ilo_root(tmp_path))
    assert callable(port)


def test_a_write_through_a_port_with_no_ledger_is_refused_for_want_of_an_approval_row(
    tmp_path: Path,
) -> None:
    port = build_ilo_port(_power_overlay(), _ilo_root(tmp_path))
    result = port(
        ModelIloRedfishRequest(
            verb=EnumIloVerb.POWER,
            host="h8",
            requested_by="lane-a",
            approval_ref="2026-11-02T11:00:00Z",
            power_action=EnumIloPowerAction.ON,
        )
    )
    assert result.outcome is EnumIloOutcome.REFUSED
    assert result.refusal is EnumIloRefusal.APPROVAL_INVALID


def test_the_port_reaches_the_ilo_node_only_through_its_one_composer() -> None:
    tree = ast.parse(Path(ilo_port_runtime.__file__).read_text(encoding="utf-8"))
    modules = {
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and n.module
        and n.module.startswith("omnimarket.nodes.node_ilo_redfish_effect")
    }
    assert modules == {
        "omnimarket.nodes.node_ilo_redfish_effect.handlers.compose_handler"
    }
