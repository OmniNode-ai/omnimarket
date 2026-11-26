# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill overlay's live seam keys are declared settings: each is set through the store, never refused as undeclared."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    LIVE_OVERLAY_SECTION,
    SEAM_KEYS,
)
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    SettingNotDeclaredError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    LIVE_LOADED_AT,
    build,
)

pytestmark = pytest.mark.unit

FLOOR_FILL_OVERLAY_ID = "node_floor_fill_orchestrator"
POOL_PATH = "/srv/example/pool-snapshot.json"
ANDON_DIR = "/srv/example/exception-queue"


def floor_fill_document() -> dict[str, JsonValue]:
    """The node's own example overlay with the six seam keys unset, the state of a host that has declared none."""
    text = (
        resources.files(f"omnimarket.nodes.{FLOOR_FILL_OVERLAY_ID}")
        .joinpath("overlay.example.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    for key in SEAM_KEYS:
        loaded[LIVE_OVERLAY_SECTION].pop(key, None)
    return json.loads(json.dumps(loaded))


def floor_fill_record() -> dict[str, Any]:
    document = floor_fill_document()
    return ModelOverlayStoreRecord(
        overlay_id=FLOOR_FILL_OVERLAY_ID,
        digest=document_digest(document),
        document=document,
        source_ref=f"{FLOOR_FILL_OVERLAY_ID}@abc123",
        loaded_by="loader",
        loaded_at=LIVE_LOADED_AT,
    ).model_dump(mode="json")


@pytest.mark.parametrize("seam_key", SEAM_KEYS)
async def test_every_seam_key_is_a_declared_setting(seam_key: str) -> None:
    handler, _bus, _ = build({FLOOR_FILL_OVERLAY_ID: floor_fill_record()})
    value: JsonValue = 180 if seam_key.endswith("_seconds") else POOL_PATH
    try:
        result = await handler.handle(
            ModelOverlaySettingSetRequest.model_validate(
                {
                    "overlay_id": FLOOR_FILL_OVERLAY_ID,
                    "key": f"{LIVE_OVERLAY_SECTION}.{seam_key}",
                    "value": value,
                    "actor": ACTOR,
                }
            )
        )
    except SettingNotDeclaredError as error:
        pytest.fail(f"{seam_key} is not a declared setting: {error}")
    assert result is not None


async def test_the_andon_state_dir_lands_in_the_stored_document() -> None:
    handler, bus, _ = build({FLOOR_FILL_OVERLAY_ID: floor_fill_record()})
    await handler.handle(
        ModelOverlaySettingSetRequest.model_validate(
            {
                "overlay_id": FLOOR_FILL_OVERLAY_ID,
                "key": f"{LIVE_OVERLAY_SECTION}.andon_state_dir",
                "value": ANDON_DIR,
                "actor": ACTOR,
            }
        )
    )
    published = [json.loads(value) for _topic, _key, value in bus.published]
    stored = [
        row for row in published if row.get("overlay_id") == FLOOR_FILL_OVERLAY_ID
    ]
    assert stored, "no record of the floor fill overlay was published"
    assert stored[0]["document"][LIVE_OVERLAY_SECTION]["andon_state_dir"] == ANDON_DIR
