# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the generated password key in the overlay matches the secret store contract (OMN-24115).

The overlay's user.password_key_id, password_folder, and password_key must equal
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
    / "src/omnimarket/nodes/node_dev_identity_provision_effect/overlay.example.yaml"
)
GENERATED_ID = "dev_probe_user_password"


def _declared() -> dict[str, object]:
    return yaml.safe_load(STORE_CONTRACT.read_text(encoding="utf-8"))["generated_keys"][
        GENERATED_ID
    ]


def _overlay_user() -> dict[str, object]:
    overlay = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return overlay["user"]


def _problems(declared: dict[str, object], user: dict[str, object]) -> list[str]:
    found: list[str] = []
    if user["password_key_id"] != GENERATED_ID:
        found.append(
            f"overlay password_key_id={user['password_key_id']}, expected {GENERATED_ID}"
        )
    if user["password_folder"] != declared["folder"]:
        found.append(
            f"overlay folder={user['password_folder']}, contract folder={declared['folder']}"
        )
    if user["password_key"] != declared["key"]:
        found.append(
            f"overlay key={user['password_key']}, contract key={declared['key']}"
        )
    return found


def test_seam_holds() -> None:
    assert _problems(_declared(), _overlay_user()) == []


def test_failing_control_renamed_key() -> None:
    user = dict(_overlay_user(), password_key="RENAMED_KEY")
    assert _problems(_declared(), user)


def test_failing_control_moved_folder() -> None:
    user = dict(_overlay_user(), password_folder="/elsewhere")
    assert _problems(_declared(), user)
