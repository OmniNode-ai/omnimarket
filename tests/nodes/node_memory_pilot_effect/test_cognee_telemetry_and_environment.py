# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cognee settings come only from the overlay, and telemetry is off with a control that turns it on.

The control and the spy need the installed package, so they skip where it is absent
(CI); the pilot host runs them. Nothing here leaves the process: the sender is stubbed.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_cognee import (
    cognee_environment,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_graphiti import (
    BackendGraphiti,
    graphiti_environment,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotOverlay,
)

from .conftest import PILOT_DIR, frozen_hash, make_graphiti_overlay

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


def test_telemetry_is_off_with_the_pilot_environment_and_on_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    utils = pytest.importorskip("cognee.shared.utils")
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(
        utils._telemetry_sender, "enqueue", lambda payload: sent.append(payload)
    )
    monkeypatch.delenv("ENV", raising=False)
    for key, value in cognee_environment(_overlay(tmp_path)).items():
        monkeypatch.setenv(key, value)
    utils.send_telemetry("pilot probe", uuid.uuid4())
    assert sent == [], "telemetry must be off under the pilot environment"
    monkeypatch.delenv("TELEMETRY_DISABLED")
    utils.send_telemetry("pilot probe", uuid.uuid4())
    assert len(sent) == 1, (
        "control: with the switch removed the same call does queue an event"
    )


def test_graphiti_environment_is_built_from_the_overlay_alone(tmp_path: Path) -> None:
    env = graphiti_environment(make_graphiti_overlay(tmp_path, cap=4))
    assert env["GRAPHITI_TELEMETRY_ENABLED"] == "false"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["SEMAPHORE_LIMIT"] == "4", "the in-flight cap comes from the overlay"
    assert (
        graphiti_environment(make_graphiti_overlay(tmp_path, cap=2))["SEMAPHORE_LIMIT"]
        == "2"
    )
    assert not any("KEY" in k for k in env)


def test_graphiti_telemetry_is_off_with_the_pilot_environment_and_on_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library disables itself under pytest, so the test hides pytest from it for the call."""
    telemetry = pytest.importorskip("graphiti_core.telemetry.telemetry")
    posthog = pytest.importorskip("posthog")
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(posthog, "capture", lambda **payload: sent.append(payload))
    monkeypatch.setattr(telemetry, "get_anonymous_id", lambda: "spy-id")
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv("GRAPHITI_TELEMETRY_ENABLED", raising=False)
    for key, value in graphiti_environment(make_graphiti_overlay(tmp_path)).items():
        monkeypatch.setenv(key, value)
    telemetry.capture_event("pilot probe")
    assert sent == [], "telemetry must be off under the pilot environment"
    monkeypatch.delenv("GRAPHITI_TELEMETRY_ENABLED")
    telemetry.capture_event("pilot probe")
    assert len(sent) == 1, (
        "control: with the switch removed the same call does queue an event"
    )


async def test_graphiti_opens_on_the_embedded_store_and_takes_the_overlays_in_flight_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("graphiti_core")
    pytest.importorskip("redislite")
    monkeypatch.delenv("SEMAPHORE_LIMIT", raising=False)
    overlay = make_graphiti_overlay(tmp_path / "store", cap=3)
    backend = BackendGraphiti(overlay, answerer=_Answerer(), system_prompt="p")
    try:
        graphiti = await backend._open("stage1")
        assert graphiti.max_coroutines == 3
        assert type(graphiti.driver).__name__ == "FalkorDriver"
        assert graphiti.driver._database == "stage1"
        with pytest.raises(RuntimeError, match="no cross encoder"):
            await graphiti.cross_encoder.rank("q", ["p"])
        assert await backend._episode_count(graphiti, "stage1") == 0, (
            "a dataset never written reads as empty, not as an error"
        )
    finally:
        await backend.close()


class _Answerer:
    async def answer(self, system_prompt: str, question: str, context: str) -> str:
        return ""


async def _write_one_episode(backend: BackendGraphiti) -> None:
    graphiti = await backend._open("stage1")
    await graphiti.driver.execute_query(
        "CREATE (:Episodic {group_id: $dataset_group, name: 'a'})",
        dataset_group="stage1",
    )


async def test_the_embedded_store_survives_a_restart_after_the_backend_saves(
    tmp_path: Path,
) -> None:
    """redislite's async close does not promise a save, so the backend saves after each document."""
    pytest.importorskip("graphiti_core")
    pytest.importorskip("redislite")
    overlay = make_graphiti_overlay(tmp_path / "store")
    first = BackendGraphiti(overlay, answerer=_Answerer(), system_prompt="p")
    await _write_one_episode(first)
    await first.save()
    await first.close()
    reopened = BackendGraphiti(overlay, answerer=_Answerer(), system_prompt="p")
    try:
        assert (
            await reopened._episode_count(await reopened._open("stage1"), "stage1") == 1
        )
        assert (
            await reopened._episode_count(await reopened._open("other"), "other") == 0
        ), "another dataset is another graph of the same store"
    finally:
        await reopened.close()
