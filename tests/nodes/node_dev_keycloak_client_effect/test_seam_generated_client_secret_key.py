# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the generated client secret key in the overlay matches the store contract (OMN-24237).

The overlay's client.secret_key_id, secret_folder, and secret_key must equal
the generated_keys entry in node_secret_store_effect/contract.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

STORE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_keycloak_client_effect/overlay.example.yaml"
)
GENERATED_ID = "dev_keycloak_client_secret"


def _declared() -> dict[str, object]:
    return yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))["generated_keys"][
        GENERATED_ID
    ]


def _overlay_client() -> dict[str, object]:
    overlay = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return overlay["client"]


def _problems(declared: dict[str, object], client: dict[str, object]) -> list[str]:
    found: list[str] = []
    if client["secret_key_id"] != GENERATED_ID:
        found.append(
            f"overlay secret_key_id={client['secret_key_id']}, expected {GENERATED_ID}"
        )
    if client["secret_folder"] != declared["folder"]:
        found.append(
            f"overlay folder={client['secret_folder']}, contract folder={declared['folder']}"
        )
    if client["secret_key"] != declared["key"]:
        found.append(
            f"overlay key={client['secret_key']}, contract key={declared['key']}"
        )
    return found


def test_seam_holds() -> None:
    assert _problems(_declared(), _overlay_client()) == []


def test_failing_control_renamed_key() -> None:
    client = dict(_overlay_client(), secret_key="RENAMED_KEY")
    assert _problems(_declared(), client)


def test_failing_control_moved_folder() -> None:
    client = dict(_overlay_client(), secret_folder="/elsewhere")
    assert _problems(_declared(), client)
