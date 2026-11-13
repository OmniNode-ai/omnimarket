# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_routing_order_compute: golden chain and error chain (OMN-23712).

Each fault is a typed refusal with ok False, no rungs and a non-empty detail.
There is no fallback order: an unbound overlay, a missing chain, an invalid
overlay or chain, or a chain that does not open on the lab tier is a refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

pytestmark = pytest.mark.unit

_CONTRACT = Path(
    "src/omnimarket/nodes/node_delegation_routing_order_compute/contract.yaml"
)
_HANDLER = Path(
    "src/omnimarket/nodes/node_delegation_routing_order_compute/handlers/handler_delegation_routing_order.py"
)


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
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay if overlay is not None else _overlay_dict()))
    return path


def _request():
    from omnibase_core.models.delegation.model_delegation_routing_order_request import (
        ModelDelegationRoutingOrderRequest,
    )

    return ModelDelegationRoutingOrderRequest(
        task_class="code_generation",
        tenant_slug=HOUSE_TENANT_SLUG,
        surface="internal",
        pin_lifted=True,
    )


def _answer(overlay_path: Path):
    import os

    os.environ["DELEGATION_ROUTING_OVERLAY_PATH"] = str(overlay_path)
    return HandlerDelegationRoutingOrder().handle(_request())


def _refusal_value(answer) -> str:
    return (
        answer.refusal.value
        if hasattr(answer.refusal, "value")
        else str(answer.refusal)
    )


def test_unbound_overlay_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH", raising=False)
    answer = HandlerDelegationRoutingOrder().handle(_request())
    assert answer.ok is False
    assert _refusal_value(answer) == "overlay_unbound"
    assert answer.rungs == ()
    assert answer.detail


def test_no_chain_for_class_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay_dict()
    overlay["escalation_chains"] = []
    overlay_path = _write_overlay(tmp_path, overlay)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    assert answer.ok is False
    assert _refusal_value(answer) == "chain_missing"
    assert answer.rungs == ()
    assert answer.detail


def test_invalid_overlay_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text("not: [valid: yaml")
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(path))
    answer = _answer(path)
    assert answer.ok is False
    assert _refusal_value(answer) == "overlay_invalid"
    assert answer.rungs == ()
    assert answer.detail


def test_unknown_rung_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = _overlay_dict()
    overlay["escalation_chains"][0]["rungs"] = ["local", "unknown_rung"]
    overlay_path = _write_overlay(tmp_path, overlay)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    assert answer.ok is False
    assert _refusal_value(answer) == "chain_invalid"
    assert answer.rungs == ()
    assert answer.detail


def test_not_lab_first_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = _overlay_dict()
    overlay["escalation_chains"][0]["rungs"] = [
        "harness_codex",
        "local",
        "harness_glm",
        "harness_lab_agent",
        "cheap_frontier",
        "cheap_cloud",
        "claude",
    ]
    overlay_path = _write_overlay(tmp_path, overlay)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    assert answer.ok is False
    assert _refusal_value(answer) == "not_lab_first"
    assert answer.rungs == ()
    assert answer.detail


def test_contract_topics() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-routing-order-read.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.delegation-routing-order-read.v1"
    ]
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.delegation-routing-order-requested.v1"
    ]


def test_no_fallback_order_written_in_handler() -> None:
    source = _HANDLER.read_text()
    for forbidden in ("codex", "glm", "lab_agent", "code_edit", "cheap_cloud"):
        assert forbidden not in source, f"handler source must not contain {forbidden!r}"
