"""Tests for the swarm registry overlay model (OMN-24053)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_swarm_registry_compute.handlers.handler_swarm_registry import (
    _load_registry,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_registry_overlay import (
    ModelSwarmRegistryOverlay,
)

_TWO_EP_YAML = textwrap.dedent(
    """\
    registry_schema_version: "1.0.0"
    endpoints:
      - id: "embed-ep"
        base_url: "http://192.0.2.10:8000/v1"
        model_id: "embed-model"
        provider: "vllm"
        capabilities: [embeddings]
        context_window: 8192
        context_window_source: "probe"
        cost_basis: "local"
        health_check_path: "/health"
        declared_by: "operator"
        endpoint_ref: "EMBED_EP_URL"
      - id: "chat-ep"
        base_url: "http://192.0.2.20:8000/v1"
        model_id: "chat-model"
        provider: "vllm"
        capabilities: [general, reasoning]
        context_window: 131072
        context_window_source: "probe"
        cost_basis: "local"
        health_check_path: "/health"
        declared_by: "operator"
        endpoint_ref: "CHAT_EP_URL"
    """
)


def _write_overlay(root: Path, content: str) -> Path:
    path = root / "node_swarm_registry_compute" / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _load(root: Path) -> ModelSwarmRegistryOverlay:
    return load_structured_overlay(
        "node_swarm_registry_compute",
        ModelSwarmRegistryOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )


def test_golden_default_registry_validates() -> None:
    packaged = (
        Path(__file__).resolve().parents[1] / "contracts" / "endpoint_registry.yaml"
    )
    doc = ModelSwarmRegistryOverlay.model_validate(
        yaml.safe_load(packaged.read_text(encoding="utf-8"))
    )
    assert doc.registry_schema_version == "1.0.0"
    assert doc.endpoints == ()


def test_golden_two_endpoints_load(tmp_path: Path) -> None:
    _write_overlay(tmp_path, _TWO_EP_YAML)
    doc = _load(tmp_path)
    assert [ep.id for ep in doc.endpoints] == ["embed-ep", "chat-ep"]


def test_seam_handler_loads_same_ids(tmp_path: Path) -> None:
    path = _write_overlay(tmp_path, _TWO_EP_YAML)
    model = _load(tmp_path)
    handler_endpoints, _ = _load_registry(path)
    assert [ep.id for ep in handler_endpoints] == [ep.id for ep in model.endpoints]


def test_error_chain(tmp_path: Path) -> None:
    # Unknown top-level key refuses and names the field.
    _write_overlay(
        tmp_path,
        textwrap.dedent(
            """\
            registry_schema_version: "1.0.0"
            endpoints: []
            bogus_top: 1
            """
        ),
    )
    with pytest.raises(NodeOverlayError) as exc:
        _load(tmp_path)
    assert "bogus_top" in str(exc.value)

    # An endpoint with an unknown field refuses.
    _write_overlay(
        tmp_path,
        textwrap.dedent(
            """\
            registry_schema_version: "1.0.0"
            endpoints:
              - id: "ep"
                base_url: "http://192.0.2.10:8000/v1"
                model_id: "m"
                provider: "vllm"
                capabilities: [general]
                bogus_ep: 1
            """
        ),
    )
    with pytest.raises(NodeOverlayError) as exc:
        _load(tmp_path)
    assert "bogus_ep" in str(exc.value)

    # An endpoint with an unknown capability refuses.
    _write_overlay(
        tmp_path,
        textwrap.dedent(
            """\
            registry_schema_version: "1.0.0"
            endpoints:
              - id: "ep"
                base_url: "http://192.0.2.10:8000/v1"
                model_id: "m"
                provider: "vllm"
                capabilities: [not_a_capability]
            """
        ),
    )
    with pytest.raises(NodeOverlayError):
        _load(tmp_path)

    # A missing registry_schema_version refuses.
    _write_overlay(
        tmp_path,
        textwrap.dedent(
            """\
            endpoints: []
            """
        ),
    )
    with pytest.raises(NodeOverlayError) as exc:
        _load(tmp_path)
    assert "registry_schema_version" in str(exc.value)
