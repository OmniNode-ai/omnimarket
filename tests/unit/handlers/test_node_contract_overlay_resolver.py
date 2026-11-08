# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22472: the node contract overlay resolver reads a node's overlay contract section.

Failure modes, one case or more each:
  (1) the overlay's contract section is not what the resolver answers, or the path is dropped
  (2) a node with no overlay answers something other than an empty contract and no path
  (3) an unreadable or unparseable overlay, or a contract section that is not a mapping, reads as
      an empty contract instead of a typed refusal naming the overlay path
  (4) the overlay's other keys (event_bus) leak into the contract
  (5) the resolver is not the one registered provider, so ModelNodeContractOverlay.for_node
      cannot reach it in an installed omnimarket
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
from omnibase_core.models.contract_overlay import (
    NODE_CONTRACT_OVERLAY_RESOLVER_ENTRY_POINT_GROUP,
    ModelNodeContractOverlay,
    ModelNodeContractOverlayResolutionError,
)

from omnimarket.handlers.bus_binding.node_contract_overlay_resolver import (
    NodeContractOverlayResolver,
)

NODE = "node_tenant_credentials_writer"


def _workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlays: dict[str, bytes]
) -> dict[str, str]:
    """A workspace whose config root holds ``overlays``; returns the environ naming it."""
    config_root = tmp_path / "config-owner"
    for node, data in overlays.items():
        path = config_root / "config" / "onex" / "overlays" / node / "overlay.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    config_root.mkdir(exist_ok=True)
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"OMNIBASE_PATH": str(workspace)}


def _overlay_path(tmp_path: Path) -> Path:
    return tmp_path / "config-owner/config/onex/overlays" / NODE / "overlay.yaml"


def test_contract_overlay_resolver_answers_the_contract_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = _workspace(
        tmp_path,
        monkeypatch,
        {
            NODE: b"event_bus:\n  type: inmemory\ncontract:\n  writer:\n    stamp_uuid: true\n"
        },
    )
    answer = NodeContractOverlayResolver(environ=environ).resolve_contract_overlay(NODE)
    assert isinstance(answer, ModelNodeContractOverlay)
    assert answer.contract == {"writer": {"stamp_uuid": True}}
    assert answer.overlay_path == _overlay_path(tmp_path)
    assert answer.node_id == NODE


def test_contract_overlay_resolver_overlay_without_section_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = _workspace(
        tmp_path, monkeypatch, {NODE: b"event_bus:\n  type: inmemory\n"}
    )
    answer = NodeContractOverlayResolver(environ=environ).resolve_contract_overlay(NODE)
    assert answer.contract == {}
    assert answer.overlay_path == _overlay_path(tmp_path)


def test_contract_overlay_resolver_no_overlay_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = _workspace(tmp_path, monkeypatch, {})
    answer = NodeContractOverlayResolver(environ=environ).resolve_contract_overlay(NODE)
    assert answer.contract == {}
    assert answer.overlay_path is None


def test_contract_overlay_resolver_no_overlay_without_workspace() -> None:
    answer = NodeContractOverlayResolver(environ={}).resolve_contract_overlay(NODE)
    assert answer.contract == {}
    assert answer.overlay_path is None


@pytest.mark.parametrize(
    ("case", "data"),
    [
        ("unreadable", b"\xff\xfe not utf-8 \xff"),
        ("malformed", b"contract: [unclosed\n"),
        ("malformed_top_level", b"- just\n- a list\n"),
        ("not_a_mapping", b"contract:\n  - a\n  - b\n"),
    ],
)
def test_contract_overlay_resolver_refuses_a_bad_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, data: bytes
) -> None:
    environ = _workspace(tmp_path, monkeypatch, {NODE: data})
    with pytest.raises(ModelNodeContractOverlayResolutionError) as caught:
        NodeContractOverlayResolver(environ=environ).resolve_contract_overlay(NODE)
    assert caught.value.overlay_path == str(_overlay_path(tmp_path)), case
    assert str(_overlay_path(tmp_path)) in str(caught.value), case


def test_contract_overlay_resolver_is_the_one_registered_entry_point() -> None:
    registered = list(
        entry_points(group=NODE_CONTRACT_OVERLAY_RESOLVER_ENTRY_POINT_GROUP)
    )
    assert [ep.value for ep in registered] == [
        "omnimarket.handlers.bus_binding.node_contract_overlay_resolver:NodeContractOverlayResolver"
    ]
    assert registered[0].load() is NodeContractOverlayResolver


def test_contract_overlay_resolver_entry_point_reached_by_for_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ = _workspace(
        tmp_path, monkeypatch, {NODE: b"contract:\n  writer:\n    stamp_uuid: true\n"}
    )
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    answer = ModelNodeContractOverlay.for_node(NODE)
    assert answer.contract == {"writer": {"stamp_uuid": True}}
