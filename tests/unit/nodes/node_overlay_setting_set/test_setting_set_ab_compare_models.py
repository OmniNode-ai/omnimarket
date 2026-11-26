# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The A/B compare orchestrator's model registry ``models`` is a declared setting: a valid registry is set through the store, an invalid one is refused with nothing written."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import JsonValue

from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    SettingValueInvalidError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    LIVE_LOADED_AT,
    build,
    contract,
)

pytestmark = pytest.mark.unit

OVERLAY_ID = "node_ab_compare_orchestrator"
KEY = "models"
MODEL = "omnimarket.models.overlay_documents.model_ab_compare_models_overlay:ModelAbCompareModelsOverlay"


def entry(**over: JsonValue) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "id": "qwen3.8-27b-h202",
        "display_name": "Qwen3.8-27B (lab h202)",
        "endpoint": "http://h202.example.test:8000",
        "protocol": "openai_compatible",
        "model_id": "Qwen3.8-27B",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "location": "local",
        "context_window": 131072,
    }
    base.update(over)
    return base


def registry_document() -> dict[str, JsonValue]:
    return {
        "schema_version": "1.0.0",
        "models": [
            {
                "id": "qwen3-coder-30b",
                "display_name": "Qwen3-Coder-30B",
                "endpoint_env": "LLM_CODER_URL",
                "protocol": "openai_compatible",
                "model_id_env": "LLM_CODER_MODEL_NAME",
                "model_id_default": "Qwen3.8-27B",
                "cost_per_1k_input": 0.0,
                "cost_per_1k_output": 0.0,
                "location": "local",
                "context_window": 112000,
            }
        ],
    }


def record() -> dict[str, Any]:
    document = registry_document()
    return ModelOverlayStoreRecord(
        overlay_id=OVERLAY_ID,
        digest=document_digest(document),
        document=document,
        source_ref=f"{OVERLAY_ID}@abc123",
        loaded_by="loader",
        loaded_at=LIVE_LOADED_AT,
    ).model_dump(mode="json")


def request(value: JsonValue) -> ModelOverlaySettingSetRequest:
    return ModelOverlaySettingSetRequest.model_validate(
        {"overlay_id": OVERLAY_ID, "key": KEY, "value": value, "actor": ACTOR}
    )


def test_the_contract_declares_the_models_row() -> None:
    rows = [
        row
        for row in contract()["settable_settings"]
        if row["overlay_id"] == OVERLAY_ID
    ]
    assert [(row["key"], row["path"], row["document_model"]) for row in rows] == [
        (KEY, KEY, MODEL)
    ]


async def test_a_registry_with_inkling_and_h202_by_address_is_set_and_stored() -> None:
    handler, bus, _ = build({OVERLAY_ID: record()})
    value: JsonValue = [
        entry(),
        entry(
            id="inkling-2-mini-h201",
            display_name="Inkling-2-Mini (lab h201)",
            endpoint="http://h201.example.test:8111",
            model_id="Inkling-2-Mini",
            context_window=334848,
        ),
    ]
    result = await handler.handle(request(value))
    assert result.setting_path == KEY
    stored = [json.loads(v) for _topic, _key, v in bus.published]
    records = [row for row in stored if row.get("overlay_id") == OVERLAY_ID]
    assert records[0]["document"]["models"] == value
    assert records[0]["operator_paths"] == [KEY]


@pytest.mark.parametrize(
    "value",
    [
        [],
        "x",
        None,
        [entry(cost_per_1k_input=-1.0)],
        [entry(protocol="grpc")],
        [entry(endpoint="not a url")],
        [entry(location="moon")],
        [entry(api_key="sk-secret")],
        [entry(), entry()],
        [{k: v for k, v in entry().items() if k != "id"}],
    ],
)
async def test_an_invalid_registry_is_refused_and_nothing_is_written(
    value: JsonValue,
) -> None:
    handler, bus, _ = build({OVERLAY_ID: record()})
    with pytest.raises(SettingValueInvalidError, match="models"):
        await handler.handle(request(value))
    assert bus.published == []


async def test_a_first_set_creates_the_record_when_the_store_holds_none() -> None:
    handler, bus, _ = build(live={})
    result = await handler.handle(request([entry()]))
    assert result.previous_present is False
    ((_, record_raw),) = bus.on(result.loaded_topic)
    document = json.loads(record_raw)["document"]
    assert document == {"schema_version": "1.0.0", "models": [entry()]}
