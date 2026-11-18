# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routing order reader: overlay values, absences and refusals (OMN-23712)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_delegation_routing_order_rung_kind import (
    EnumDelegationRoutingOrderRungKind,
)
from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)

import omnimarket.handlers.delegation_routing_order_reader as reader
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
    read_engine_order,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)

pytestmark = pytest.mark.unit


def _overlay_dict() -> dict:
    return {
        "schema_version": "delegation_routing_overlay.v1",
        "harness_backends": [
            {
                "backend_id": "harness-codex",
                "provider": "openai",
                "kind": "harness",
                "harness": "codex",
                "surface": "internal",
                "tenant_scope": "house",
                "tier": "harness",
                "explicit_pin_only": True,
                "model_name": None,
                "capabilities": ["code_generation"],
            },
            {
                "backend_id": "harness-claude-glm",
                "provider": "zhipu",
                "kind": "harness",
                "harness": "claude-glm",
                "surface": "internal",
                "tenant_scope": "house",
                "tier": "harness",
                "explicit_pin_only": True,
                "model_name": "glm-5.3",
                "capabilities": ["code_generation"],
            },
            {
                "backend_id": "harness-lab-agent",
                "provider": "qwen",
                "kind": "harness",
                "harness": "lab-agent",
                "surface": "internal",
                "tenant_scope": "house",
                "tier": "harness",
                "explicit_pin_only": True,
                "model_name": "Qwen3.8-27B",
                "capabilities": ["code_generation"],
            },
        ],
        "harness_tiers": [
            {
                "name": "harness_codex",
                "backend_id": "harness-codex",
                "use_for": ["code_generation"],
            },
            {
                "name": "harness_glm",
                "backend_id": "harness-claude-glm",
                "use_for": ["code_generation"],
            },
            {
                "name": "harness_lab_agent",
                "backend_id": "harness-lab-agent",
                "use_for": ["code_generation"],
            },
        ],
        "escalation_chains": [
            {
                "task_class": "code_generation",
                "escalate_on": "acceptance_check",
                "rungs": [
                    "local",
                    "harness_codex",
                    "harness_glm",
                    "harness_lab_agent",
                    "cheap_frontier",
                    "cheap_cloud",
                    "claude",
                ],
            }
        ],
    }


def _write_overlay(tmp_path: Path, overlay: dict | None = None) -> Path:
    path = tmp_path / "delegation_routing" / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(overlay if overlay is not None else _overlay_dict()))
    return path


def _node_order(task_class: str) -> tuple[str, ...]:
    """The order the node itself answers for ``task_class``, mapped the way the reader maps it."""
    answer = HandlerDelegationRoutingOrder().handle(
        ModelDelegationRoutingOrderRequest(
            task_class=task_class,
            tenant_slug=None,
            surface=EnumDelegationBackendSurface.INTERNAL.value,
            pin_lifted=True,
        )
    )
    assert answer.ok, answer.detail
    return tuple(
        rung.kind.value if rung.harness is None else rung.harness
        for rung in answer.rungs
    )


@pytest.fixture(autouse=True)
def _node_hosted_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """These cases exercise the in-process answer whatever overlay the host holds (the bus path is in test_delegation_routing_order_reader_bus.py)."""
    monkeypatch.setattr(
        reader, "resolve_node_event_bus", lambda *_args, **_kwargs: (None, None)
    )


def _bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH", raising=False)


def test_golden_named_class_returns_node_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named class reads its overlay order, equal to the node's answer and to the overlay's chain."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    order = read_engine_order("code_generation")
    assert order == _node_order("code_generation")
    assert order == (
        "ladder",
        "codex",
        "claude-glm",
        "lab-agent",
        "ladder",
        "ladder",
        "ladder",
    )


def test_first_element_is_ladder_for_every_named_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For every class the overlay names that the node answers ok, rung 0 is the ladder."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    for chain in _overlay_dict()["escalation_chains"]:
        order = read_engine_order(chain["task_class"])
        assert order is not None
        assert order[0] == EnumDelegationRoutingOrderRungKind.LADDER.value


def test_unnamed_class_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A class the overlay does not name has no order."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    assert read_engine_order("document") is None


def test_no_overlay_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install with no overlay root naming the routing overlay has no order."""
    _bind(tmp_path, monkeypatch)
    assert read_engine_order("code_generation") is None


def test_invalid_overlay_refuses_never_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present but invalid overlay raises; the reader never returns a default."""
    path = tmp_path / "delegation_routing" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: nope\n", encoding="utf-8")
    _bind(tmp_path, monkeypatch)
    with pytest.raises(DelegationRoutingOrderError, match="overlay_invalid"):
        read_engine_order("code_generation")


def test_not_lab_first_refuses_never_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain that does not open on the lab tier raises; the reader never defaults."""
    overlay = _overlay_dict()
    overlay["escalation_chains"][0]["rungs"] = [
        "harness_codex",
        "harness_glm",
        "harness_lab_agent",
    ]
    _write_overlay(tmp_path, overlay)
    _bind(tmp_path, monkeypatch)
    with pytest.raises(DelegationRoutingOrderError, match="not_lab_first"):
        read_engine_order("code_generation")


def test_overlay_edit_changes_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay is read on every call, so an edit is in force on the next read."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    before = read_engine_order("code_generation")
    swapped = _overlay_dict()
    rungs = swapped["escalation_chains"][0]["rungs"]
    codex, glm = rungs.index("harness_codex"), rungs.index("harness_glm")
    rungs[codex], rungs[glm] = rungs[glm], rungs[codex]
    _write_overlay(tmp_path, swapped)
    after = read_engine_order("code_generation")
    assert after == _node_order("code_generation")
    assert after != before
