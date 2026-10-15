# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The inference-intent mismatch refusal names the file that supplied the model.

OMN-18670. The first fix made ``HandlerLlmDelegationCall`` name the overlay file
and key behind a mismatching ``model_name``. The orchestrator's dispatch path
runs ``HandlerInferenceIntent`` instead, whose OMN-17098 guard refused with the
value and the endpoint but no file, so a machine-local overlay pinning a retired
model still took a rung dark with a refusal no repository grep could explain.

The fixture is the 2026-09-18 case on tmp paths: the tracked contract declares
``Qwen3.8-27B`` for ``local-coder``, an untracked overlay re-pins a retired id and
supplies the endpoint, and the endpoint (a real loopback server) serves only the
tracked id. The committed and overlay values are asserted to DIFFER before the
handler runs, so the test cannot pass without an override in play.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from tests.unit.delegation.test_omn17098_inference_intent_served_model_guard import (
    _StubOpenAiServer,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

_RETIRED_MODEL = "Qwen3.6-35B-A3B"
_SERVED_MODEL = "Qwen3.8-27B"
_BACKEND_ID = "local-coder"
_REAL_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)


@pytest.fixture(autouse=True)
def _fresh_served_models_state() -> None:
    transport.served_models_cache.clear()


def _bind_stale_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint_url: str
) -> Path:
    """Bind a tracked contract and an untracked overlay that overrides its model."""
    lane_overlay = tmp_path / "lane_overlay.yaml"
    lane_overlay.write_text(
        yaml.safe_dump(
            {"backends": [{"backend_id": _BACKEND_ID, "model_name": _SERVED_MODEL}]}
        ),
        encoding="utf-8",
    )
    rendered = load_bifrost_delegation_config(
        config_path=_REAL_CONTRACT, overlay_path=lane_overlay
    )
    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(
        yaml.safe_dump(rendered.model_dump(mode="json")), encoding="utf-8"
    )
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": _BACKEND_ID,
                        "endpoint_url": endpoint_url,
                        "model_name": _RETIRED_MODEL,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    committed = {
        row["backend_id"]: row.get("model_name")
        for row in yaml.safe_load(contract.read_text(encoding="utf-8"))["backends"]
    }
    assert committed[_BACKEND_ID] == _SERVED_MODEL != _RETIRED_MODEL, (
        "the fixture overlay overrides nothing, so this test would prove nothing"
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    return overlay


def _intent(chat_url: str, model: str) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=chat_url,
        model=model,
        system_prompt="You are a test author.",
        prompt="Write a test.",
        max_tokens=256,
        temperature=0.0,
        timeout_seconds=10.0,
        correlation_id=uuid4(),
        inference_attempt_id=uuid4(),
    )


def test_refusal_names_the_overlay_file_and_key_that_pinned_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _StubOpenAiServer(200, [_SERVED_MODEL]) as server:
        overlay = _bind_stale_overlay(tmp_path, monkeypatch, server.chat_url)

        response = HandlerInferenceIntent().handle(
            _intent(server.chat_url, _RETIRED_MODEL)
        )

    message = response.error_message or ""
    assert "model_attribution_mismatch" in message
    assert str(overlay) in message
    assert f"backends[{_BACKEND_ID}].model_name" in message
    assert _SERVED_MODEL in message
    assert server.chat_posts == []


def test_refusal_with_no_known_source_says_so_and_points_at_the_explain_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model no merged backend declares gets no invented file, but a next step."""
    with _StubOpenAiServer(200, [_SERVED_MODEL]) as server:
        _bind_stale_overlay(tmp_path, monkeypatch, server.chat_url)

        response = HandlerInferenceIntent().handle(
            _intent(server.chat_url, "model-no-backend-declares")
        )

    message = response.error_message or ""
    assert "model_attribution_mismatch" in message
    assert "bifrost_overrides.yaml" not in message
    assert "cli_explain_routing" in message
