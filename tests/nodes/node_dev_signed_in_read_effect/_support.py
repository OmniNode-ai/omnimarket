# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the dev signed-in read node tests (OMN-24345)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dev_signed_in_read_effect.handlers import (
    HandlerDevSignedInRead,
)
from omnimarket.nodes.node_dev_signed_in_read_effect.overlay import (
    ModelDevSignedInReadOverlay,
)

from .fakes import FakeKeycloakAndApi, FakeSettingStore

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_signed_in_read_effect"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
EXAMPLE_PATH = NODE_DIR / "overlay.example.yaml"

PASSWORD = "Dp1!stored"
USER_TOKEN = "user-access-token"
PASSWORD_LOCATION = ("/shared/env", "DEV_PROBE_USER_PASSWORD")


def overlay_document() -> dict[str, Any]:
    """The example overlay, as the document a test may mutate before building a model."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))
    return document


def overlay(document: dict[str, Any] | None = None) -> ModelDevSignedInReadOverlay:
    return ModelDevSignedInReadOverlay.model_validate(document or overlay_document())


def build(
    transport: FakeKeycloakAndApi,
    store: FakeSettingStore,
    document: dict[str, Any] | None = None,
) -> HandlerDevSignedInRead:
    return HandlerDevSignedInRead(
        overlay=overlay(document),
        transport=transport,
        settings_store=store,
        contract_path=CONTRACT_PATH,
    )


def seeded_store() -> FakeSettingStore:
    store = FakeSettingStore()
    store.set(*PASSWORD_LOCATION, PASSWORD)
    return store
