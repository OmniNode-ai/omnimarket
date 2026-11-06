# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each typed capability resolves to its backend (OMN-22774)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_media_capability_resolve_compute.models import (
    ModelMediaBackend,
    ModelMediaCapabilityRequest,
)

NAME = "node_media_capability_resolve_compute"
_HOST = "http://192.0.2.30"
_BY_CAPABILITY = {
    "tts": ("media-tts", "/v1/audio/speech", 8102),
    "stt": ("media-stt", "/v1/audio/transcriptions", 8103),
    "embed": ("media-embed", "/v1/embeddings", 8100),
    "decide": ("media-decide", "/v1/chat/completions", 8101),
    "video": ("media-video", "/v1/videos", 8104),
}


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _handler() -> Any:
    handler = _contract()["handler_routing"]["handlers"][0]["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def backend(capability: str, **overrides: Any) -> ModelMediaBackend:
    backend_id, path, port = _BY_CAPABILITY[capability]
    fields: dict[str, Any] = {
        "backend_id": backend_id,
        "capability": capability,
        "endpoint_url": f"{_HOST}:{port}{path}",
        "served_model_id": f"served-{capability}",
        "serving": True,
    }
    fields.update(overrides)
    return ModelMediaBackend(**fields)


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()

    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["event_bus"]["subscribe_topics"][0]
    )
    assert contract["terminal_event"] == contract["event_bus"]["publish_topics"][0]
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.media-capability-resolved.v1"
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.media-capability-resolve-requested.v1"
    )


@pytest.mark.parametrize("capability", sorted(_BY_CAPABILITY))
def test_golden_capability_resolves_declared_backend_and_url(capability: str) -> None:
    request = ModelMediaCapabilityRequest(
        capability=capability,
        backends=tuple(backend(name) for name in _BY_CAPABILITY),
    )

    result = _handler().handle(request)
    backend_id, path, port = _BY_CAPABILITY[capability]

    assert result.status == "resolved"
    assert result.backend_id == backend_id
    assert result.endpoint_url == f"{_HOST}:{port}{path}"
    assert result.served_model_id == f"served-{capability}"
    assert result.refusal is None


def test_golden_a_dark_backend_does_not_serve_its_capability() -> None:
    request = ModelMediaCapabilityRequest(
        capability="tts",
        backends=(
            backend("tts", serving=False),
            backend("tts", backend_id="media-tts-2"),
        ),
    )

    assert _handler().handle(request).backend_id == "media-tts-2"
