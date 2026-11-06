# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cognee settings come only from the overlay, and telemetry is off with a control that turns it on.

The telemetry control needs the installed package and lives in
pilot_host_cognee_telemetry_check.py, which CI does not collect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_cognee import (
    cognee_environment,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotOverlay,
)

from .conftest import PILOT_DIR, frozen_hash

pytestmark = pytest.mark.unit


def _overlay(tmp_path: Path) -> ModelMemoryPilotOverlay:
    return ModelMemoryPilotOverlay.model_validate(
        {
            "backend": "cognee",
            "frozen_hash": frozen_hash(),
            "pilot_dir": PILOT_DIR,
            "telemetry_disabled": True,
            "allowed_hosts": ("lab-chat.example.test", "lab-embed.example.test"),
            "chat": {
                "base_url": "http://lab-chat.example.test:8000/v1",
                "model": "chat-m",
                "timeout_seconds": 5,
            },
            "embeddings": {
                "base_url": "http://lab-embed.example.test:8002/v1",
                "model": "embed-m",
                "timeout_seconds": 5,
            },
            "embedding_dimensions": 16,
            "ingest_data_per_batch": 2,
            "storage_root": tmp_path,
        }
    )


def test_environment_is_built_from_the_overlay_alone(tmp_path: Path) -> None:
    env = cognee_environment(_overlay(tmp_path))
    assert env["TELEMETRY_DISABLED"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert env["LLM_ENDPOINT"] == "http://lab-chat.example.test:8000/v1"
    assert env["EMBEDDING_ENDPOINT"] == "http://lab-embed.example.test:8002/v1"
    assert env["LLM_MODEL"] == "openai/chat-m"
    assert env["EMBEDDING_DIMENSIONS"] == "16"
    for key in (
        "DATA_ROOT_DIRECTORY",
        "SYSTEM_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
        "TIKTOKEN_CACHE_DIR",
    ):
        assert env[key].startswith(str(tmp_path)), (
            f"{key} must stay under the overlay's storage root"
        )
    assert not any("KEY" in k and v not in ("keyless",) for k, v in env.items())


def test_extraction_and_summarization_route_to_their_own_endpoint_when_set(
    tmp_path: Path,
) -> None:
    base = _overlay(tmp_path)
    routed = ModelMemoryPilotOverlay.model_validate(
        {
            **base.model_dump(),
            "chat_extraction": {
                "base_url": "http://lab-chat.example.test:8100/v1",
                "model": "chat-m",
                "timeout_seconds": 5,
            },
        }
    )
    env = cognee_environment(routed)
    for stage in ("EXTRACTION", "SUMMARIZATION"):
        assert env[f"LLM_{stage}_ENDPOINT"] == "http://lab-chat.example.test:8100/v1"
    assert env["LLM_ENDPOINT"] == "http://lab-chat.example.test:8000/v1"
    assert "LLM_EXTRACTION_ENDPOINT" not in cognee_environment(base)


def test_a_secret_shaped_placeholder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="never a key"):
        ModelMemoryPilotOverlay.model_validate(
            {
                **_overlay(tmp_path).model_dump(),
                "api_key_placeholder": "sk-live-1234567890",
            }
        )
