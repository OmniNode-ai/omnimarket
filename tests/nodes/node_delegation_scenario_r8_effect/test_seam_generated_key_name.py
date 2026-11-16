# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the key the secret store node generates is the key scenario R8 reads (OMN-23746).

The store node's contract declares ``generated_keys.r8_probe_tenant_key`` (folder, key name); R8's settings overlay reads
the probe tenant key from a folder under a name, and R8's lane contract names the environment variable that carries it.
The three must agree, so that a rename or move on either side fails here rather than in a running unit. The store block
itself is the shared ``ModelSecretStoreOverlay`` model: the R8 overlay document and the secret reading overlay are checked
against the same model by the overlays repository's pins.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.handlers.delegation_scenario_settings import load_settings_overlay
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    NODE_NAME,
    load_lane_settings,
    load_runtime,
)

STORE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
GENERATED_ID = "r8_probe_tenant_key"


def _declared() -> dict[str, object]:
    return yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))["generated_keys"][
        GENERATED_ID
    ]


def _problems(declared: dict[str, object]) -> list[str]:
    """Where the generated key and the key R8 reads disagree; empty when the seam holds."""
    overlay = load_settings_overlay(
        CONTRACT_PATH.parent / load_runtime().settings_overlay_relpath
    )
    wanted = load_lane_settings().api_key_env
    setting = next((s for s in overlay.settings if s.env == wanted), None)
    found: list[str] = []
    if setting is None:
        return [f"R8 settings overlay fills no {wanted}"]
    if setting.secret != declared["key"]:
        found.append(
            f"R8 reads {setting.secret}, the store generates {declared['key']}"
        )
    if overlay.folder != declared["folder"]:
        found.append(
            f"R8 reads folder {overlay.folder}, the store writes {declared['folder']}"
        )
    return found


def test_the_generated_key_is_the_one_r8_reads() -> None:
    assert _problems(_declared()) == []


def test_the_overlay_id_is_the_node_directory() -> None:
    assert CONTRACT_PATH.parent.name == NODE_NAME


def test_failing_control_a_renamed_or_moved_generated_key_breaks_the_seam() -> None:
    assert _problems(dict(_declared(), key="R8_RENAMED_KEY"))
    assert _problems(dict(_declared(), folder="/elsewhere"))
