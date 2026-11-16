# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the packaged ladder through its public routing reducer."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.handlers.routing.routing_tiers_path import (
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    resolve_routing_tiers_path,
)
from omnimarket.models.delegation.wire import ModelDelegationRequest
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit
CONFIGS = ROUTING_TIERS_PACKAGED_DEFAULT_PATH.parent
LOCAL_CLASSES = (
    ("local-coder", "code_generation"),
    ("local-coder", "refactor"),
    ("local-coder", "validator_generation"),
    ("local-coder", "test"),
    *(
        ("local-heavy-reasoning", name)
        for name in (
            "research",
            "reasoning",
            "complex_reasoning",
            "planning",
            "review",
            "document",
            "escalation",
            "evidence_comment",
            "documentation",
            "summarization",
            "brief_drafting",
            "extraction_proposer",
            "tail_summary",
            "context_selection",
            "context_compaction",
        )
    ),
)


@pytest.fixture(autouse=True)
def packaged_ladder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # Demonstrate the ambient override, then explicitly pin the packaged file.
    decoy = tmp_path / "wrong-ladder.yaml"
    decoy.write_text("tiers: []\n")
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(decoy))
    assert resolve_routing_tiers_path() == decoy
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(ROUTING_TIERS_PACKAGED_DEFAULT_PATH)
    )
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(CONFIGS / "task_class_contracts.v1.yaml")
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty-overlays"))
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    # Load real backend declarations through the normal YAML loader. Only I/O
    # bindings and credentials are replaced; the full tier ladder stays intact.
    data = yaml.safe_load((CONFIGS / "bifrost_delegation.yaml").read_text())
    for backend in data["backends"]:
        backend["endpoint_url"] = "http://127.0.0.1:18742/v1/chat/completions"
        backend["model_name"] = backend["backend_id"]
        backend["secret_ref"] = None
        backend["explicit_pin_only"] = False
        backend.pop("endpoint_url_env", None)
        backend.pop("model_name_env", None)
    path = tmp_path / "bifrost.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(path))
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def request(
    task: str, tokens: int, backend: str | None = None
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        task_type=task,
        prompt="x" * (tokens * 4),
        backend_id=backend,
    )


def overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ceiling: int) -> None:
    root = tmp_path / "overlays"
    path = root / "node_delegation_code_budget_compute" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    data = {
        "class_budgets_s": {"summarization": 120},
        "sizing_margin": 1,
        "round_up_to_s": 1,
        "min_completed_samples": 1,
        "ceiling_s": 300,
        "input_ceilings": {"local-heavy-reasoning": {"summarization": ceiling}},
    }
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


def test_packaged_ladder_routes_recorded_prompt_despite_ambient_override() -> None:
    assert resolve_routing_tiers_path() == ROUTING_TIERS_PACKAGED_DEFAULT_PATH
    decision = routing.delta(request("summarization", 11072))
    assert decision.tier_name == "local"
    assert decision.selected_backend_ref == "local-heavy-reasoning"
    assert decision.max_context_tokens + decision.max_tokens <= 131072


@pytest.mark.parametrize(("backend", "task"), LOCAL_CLASSES)
@pytest.mark.parametrize("with_overlay", [False, True])
def test_every_local_class_reserves_output(
    backend: str,
    task: str,
    with_overlay: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if with_overlay:
        # This overlay intentionally names only one class; unlisted pairs
        # still need the packaged reserve.
        overlay(monkeypatch, tmp_path, 65536)
    with pytest.raises(ProtocolConfigurationError, match="not routable"):
        routing.delta(request(task, 65537, backend))
    accepted = routing.delta(request(task, 65536, backend))
    assert accepted.selected_backend_ref == backend
    assert accepted.max_tokens == 65536
    assert accepted.max_context_tokens + accepted.max_tokens <= 131072
    # Ordinary traffic must walk past the local tier too; classes with no
    # declared large-window fallback refuse instead of overflowing locally.
    try:
        ordinary = routing.delta(request(task, 65537))
    except ProtocolConfigurationError:
        pass
    else:
        assert ordinary.tier_name != "local"


@pytest.mark.parametrize("task", ["escalation", "research", "reasoning"])
@pytest.mark.parametrize("tokens", [8193, 11072, 65536, 65537, 131072])
def test_reasoning_classes_reach_large_window_rungs(task: str, tokens: int) -> None:
    decision = routing.delta(request(task, tokens))
    if tokens <= 65536:
        assert decision.tier_name == "local"
        assert decision.max_context_tokens + decision.max_tokens <= 131072
    else:
        assert decision.tier_name != "local"
        assert decision.max_context_tokens >= tokens


def test_overlay_gate_is_read_by_public_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert routing.delta(request("summarization", 11072)).tier_name == "local"
    overlay(monkeypatch, tmp_path, 8192)
    assert routing.delta(request("summarization", 11072)).tier_name != "local"
    overlay_path = (
        tmp_path / "overlays/node_delegation_code_budget_compute/overlay.yaml"
    )
    data = yaml.safe_load(overlay_path.read_text())
    data["input_ceilings"]["local-heavy-reasoning"]["summarization"] = 12000
    overlay_path.write_text(yaml.safe_dump(data))
    assert routing.delta(request("summarization", 11072)).tier_name == "local"
