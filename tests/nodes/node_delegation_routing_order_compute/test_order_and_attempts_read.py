# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_routing_order_compute: the answer's tiers, attempts and refusals (OMN-23712 AC7).

The overlay is written to tmp_path as yaml and bound by
DELEGATION_ROUTING_OVERLAY_PATH; the shipped contracts (routing_tiers.yaml and
task_class_contracts.v1.yaml) are read from src/omnimarket/configs. The local
rung's attempts are max_retries + 1, read from the shipped routing_tiers.yaml
through yaml, never a literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_config,
    resolve_task_class_tier_order,
)
from omnimarket.nodes.node_lab_first_routing_check_compute.handlers.handler_lab_first_routing_check import (
    HandlerLabFirstRoutingCheck,
)
from omnimarket.nodes.node_lab_first_routing_check_compute.models.model_lab_first_routing_check import (
    ModelLabFirstRoutingRequest,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

pytestmark = pytest.mark.unit

_CONFIGS = Path("src/omnimarket/configs")


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


def _request(pin_lifted: bool = True):
    from omnibase_core.models.delegation.model_delegation_routing_order_request import (
        ModelDelegationRoutingOrderRequest,
    )

    return ModelDelegationRoutingOrderRequest(
        task_class="code_generation",
        tenant_slug=HOUSE_TENANT_SLUG,
        surface="internal",
        pin_lifted=pin_lifted,
    )


def _answer(overlay_path: Path, pin_lifted: bool = True):
    import os

    os.environ["DELEGATION_ROUTING_OVERLAY_PATH"] = str(overlay_path)
    return HandlerDelegationRoutingOrder().handle(_request(pin_lifted))


def _kind_value(rung) -> str:
    return rung.kind.value if hasattr(rung.kind, "value") else str(rung.kind)


def _harness_rungs(answer):
    return [r for r in answer.rungs if _kind_value(r) == "harness"]


def test_answer_tiers_and_harness_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    assert answer.ok is True
    tiers = [rung.tier for rung in answer.rungs]
    assert tiers == [
        "local",
        "harness_codex",
        "harness_glm",
        "harness_lab_agent",
        "cheap_frontier",
        "cheap_cloud",
        "claude",
    ]
    assert [r.harness for r in _harness_rungs(answer)] == [
        "codex",
        "claude-glm",
        "lab-agent",
    ]


def test_local_rung_backend_ids_and_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    local_rung = answer.rungs[0]
    assert _kind_value(local_rung) == "ladder"
    tiers = _get_config().tiers
    local_tier = next(t for t in tiers if t.name == "local")
    expected_backend_ids = tuple(
        m.backend_ref for m in local_tier.models if "code_generation" in m.use_for
    )
    assert local_rung.backend_ids == expected_backend_ids
    assert local_rung.attempts == local_tier.max_retries + 1


def test_overlay_change_changes_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    tiers = [rung.tier for rung in answer.rungs]
    assert tiers[1] == "harness_codex"
    assert tiers[2] == "harness_glm"
    swapped = _overlay_dict()
    chain = swapped["escalation_chains"][0]
    rungs = chain["rungs"]
    idx_codex = rungs.index("harness_codex")
    idx_glm = rungs.index("harness_glm")
    rungs[idx_codex], rungs[idx_glm] = rungs[idx_glm], rungs[idx_codex]
    swapped_path = _write_overlay(tmp_path, swapped)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(swapped_path))
    answer2 = _answer(swapped_path)
    tiers2 = [rung.tier for rung in answer2.rungs]
    assert tiers2[1] == "harness_glm"
    assert tiers2[2] == "harness_codex"


def test_changed_max_retries_changes_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path)
    base_attempts = answer.rungs[0].attempts
    tiers_yaml = _CONFIGS / "routing_tiers.yaml"
    data = yaml.safe_load(tiers_yaml.read_text())
    local_tier = next(t for t in data["tiers"] if t["name"] == "local")
    local_tier["max_retries"] = local_tier["max_retries"] + 1
    modified = tmp_path / "routing_tiers_modified.yaml"
    modified.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(modified))
    answer2 = _answer(overlay_path)
    assert answer2.rungs[0].attempts == base_attempts + 1


def test_resolve_task_class_tier_order_matches_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    order_with = resolve_task_class_tier_order("code_generation")
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH")
    order_without = resolve_task_class_tier_order("code_generation")
    assert order_with == order_without
    contract = yaml.safe_load((_CONFIGS / "task_class_contracts.v1.yaml").read_text())
    entry = contract["task_classes"]["code_generation"]
    tier_order = tuple(entry["escalation_policy"]["tier_order"])
    assert order_with == tier_order


def test_lab_first_check_passes_on_shipped_contracts() -> None:
    contracts = yaml.safe_load((_CONFIGS / "task_class_contracts.v1.yaml").read_text())
    tiers = yaml.safe_load((_CONFIGS / "routing_tiers.yaml").read_text())
    result = HandlerLabFirstRoutingCheck().handle(
        ModelLabFirstRoutingRequest(task_class_contracts=contracts, routing_tiers=tiers)
    )
    assert result.ok is True


def test_pin_lifted_false_keeps_not_pin_routable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_path = _write_overlay(tmp_path)
    monkeypatch.setenv("DELEGATION_ROUTING_OVERLAY_PATH", str(overlay_path))
    answer = _answer(overlay_path, pin_lifted=False)
    for rung in _harness_rungs(answer):
        assert EnumHarnessRungRefusal.NOT_PIN_ROUTABLE.value in rung.refusals
