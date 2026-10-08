# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20477: Qwen3.8-27B writes tests only inside the repair loop.

The 2026-10-03 judged matrix accepted 2 of 18 single-shot ``test`` answers from
Qwen3.8-27B (interval 3-33), under the 0.50 suppression floor at n 18. Operator
RULING 2026-10-08T22:58:17Z applied that change by hand: an unpinned ``test``
request no longer selects the local rung, and the run-the-tests-and-repair loop
keeps the model by pinning ``local-coder``, which bypasses ``use_for``.

``code_review`` needed no change: the class contract has withheld it since
RULING 2026-10-02T19:33:51Z, so no rung is offered for it.

These run the real shipped routing contracts (routing tiers, task-class
contracts, bifrost contract) with an overlay that binds the local rungs to a
loopback endpoint and a credential that resolves everywhere, so the only thing
that can differ between requests is the contracts' own declaration.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.delegated_test_loop import loop_ports
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
)

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_LOOPBACK = (
    "http://127.0.0.1:18742/v1/chat/completions"  # url-authority-ok: test loopback
)
_LOCAL_CODER = "local-coder"
# The cloud rungs carry complete endpoints in the shipped contract; only the
# local rungs leave endpoint and model to the overlay.
_LOCAL_RUNGS = ("local-coder", "local-heavy-reasoning")


@pytest.fixture
def bound_ladder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    overlay = tmp_path / "bifrost_overrides.yaml"
    lines = ["backends:"]
    for backend_id in _LOCAL_RUNGS:
        lines += [
            f"  - backend_id: {backend_id}",
            f'    endpoint_url: "{_LOOPBACK}"',
            '    model_name: "served-model"',
        ]
    overlay.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key in (
        "BIFROST_CONTRACT_PATH",
        "DELEGATION_ROUTING_TIERS_PATH",
        "TASK_CLASS_CONTRACT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    monkeypatch.setattr(routing, "api_key_ref_available", lambda *_a, **_k: True)
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._load_bifrost_endpoints.cache_clear()


def _request(task_type: str, backend_id: str | None = None) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        backend_id=backend_id,
    )


@pytest.mark.usefixtures("bound_ladder")
def test_unpinned_test_request_leaves_the_local_rung() -> None:
    decision = delta(_request("test"))
    assert decision.tier_name == "cheap_frontier"
    assert decision.selected_backend_ref != _LOCAL_CODER


@pytest.mark.usefixtures("bound_ladder")
def test_loop_pin_still_selects_the_local_rung_for_test() -> None:
    decision = delta(_request("test", backend_id=loop_ports.LOOP_WRITER_BACKEND_ID))
    assert decision.tier_name == "local"
    assert decision.selected_backend_ref == _LOCAL_CODER


@pytest.mark.usefixtures("bound_ladder")
@pytest.mark.parametrize("task_type", ["summarization", "review"])
def test_unapproved_classes_still_route_to_the_local_tier(task_type: str) -> None:
    assert delta(_request(task_type)).tier_name == "local"


@pytest.mark.usefixtures("bound_ladder")
def test_code_review_stays_withheld_from_every_rung() -> None:
    contract = yaml.safe_load(
        (_CONFIGS / "task_class_contracts.v1.yaml").read_text(encoding="utf-8")
    )["task_classes"]["code_review"]
    assert contract["routing_availability"]["status"] == "withheld"
    tiers = yaml.safe_load((_CONFIGS / "routing_tiers.yaml").read_text("utf-8"))[
        "tiers"
    ]
    serving = [
        model["backend_id"]
        for tier in tiers
        for model in tier["models"]
        if "code_review" in (model.get("use_for") or [])
    ]
    assert serving == []


def test_test_class_ladder_declares_no_local_tier() -> None:
    contract = yaml.safe_load(
        (_CONFIGS / "task_class_contracts.v1.yaml").read_text(encoding="utf-8")
    )["task_classes"]["test"]
    policy = contract["escalation_policy"]
    assert "local" not in policy["tier_order"]
    assert policy["max_escalations"] == len(policy["tier_order"]) - 1


def test_the_repair_loop_pins_the_local_coder_on_every_delegate_call() -> None:
    captured: list[list[str]] = []

    def run_delegate(argv: list[str]) -> object:
        captured.append(argv)
        raise RuntimeError("stop after capturing the argv")

    ports = loop_ports.DelegatedTestLoopPorts(
        onex=Path("onex"),
        state_root=Path("state"),
        source_clone=Path("clone"),
        test_path="tests/test_x.py",
        run_focused=lambda _request: (_ for _ in ()).throw(AssertionError("unused")),
        run_delegate=run_delegate,  # type: ignore[arg-type]
    )
    prompt = loop_ports.ModelPrompt(prompt="write it", response_contract={})
    with pytest.raises(RuntimeError, match="stop after capturing"):
        ports.delegate(prompt, 1)
    argv = captured[0]
    assert argv[argv.index("--task-type") + 1] == "test"
    assert argv[argv.index("--backend-id") + 1] == loop_ports.LOOP_WRITER_BACKEND_ID
