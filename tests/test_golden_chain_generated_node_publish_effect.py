# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_generated_node_publish_effect (OMN-13606).

Verifies the EFFECT node dispatches headless: the contract declares a non-empty
``operation`` on its operation_match handler entry AND a resolvable
initial-payload model, and ``HandlerGeneratedNodePublishEffect.handle`` accepts a
single typed ``ModelGeneratedNodePublishInput`` payload (the RuntimeLocal
event-driven dispatch shape). Also asserts the full publish chain end-to-end:
start payload -> worktree+commit+push -> gh pr create -> completed result with a
PR URL emitted on the contract-declared publish topic.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_generated_node_publish_effect.handlers.handler_generated_node_publish_effect import (
    HandlerGeneratedNodePublishEffect,
)
from omnimarket.nodes.node_generated_node_publish_effect.models.model_generated_node_publish_input import (
    ModelGeneratedNodePublishInput,
)
from omnimarket.nodes.node_generated_node_publish_effect.models.model_generated_node_publish_result import (
    ModelGeneratedNodePublishResult,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_generated_node_publish_effect/contract.yaml"
)
CMD_TOPIC = "onex.cmd.omnimarket.generated-node-publish-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.generated-node-published.v1"
REPO = "OmniNode-ai/omnimarket"
NODE_NAME = "node_demo_widget_compute"
TICKET = "OMN-13606"


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _staged_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "staging" / NODE_NAME
    pkg.mkdir(parents=True)
    (pkg / "contract.yaml").write_text("name: " + NODE_NAME + "\n", encoding="utf-8")
    return pkg


def _input(staging_dir: Path, **overrides: object) -> ModelGeneratedNodePublishInput:
    base: dict[str, object] = {
        "correlation_id": uuid4(),
        "node_name": NODE_NAME,
        "staging_dir": str(staging_dir),
        "repo": REPO,
        "ticket": TICKET,
        "dod_evidence": "golden-chain proof",
    }
    base.update(overrides)
    return ModelGeneratedNodePublishInput(**base)


def _make_run(
    responses: list[tuple[int, str, str]],
) -> Callable[[list[str]], tuple[int, str, str]]:
    idx = 0

    def _run(_cmd: list[str]) -> tuple[int, str, str]:
        nonlocal idx
        rc, out, err = responses[idx]
        idx += 1
        return rc, out, err

    return _run


@pytest.mark.unit
class TestPublishContractRouting:
    """Contract dispatches headless: operation + resolvable payload model."""

    def test_contract_declares_operation_on_operation_match_entry(self) -> None:
        raw = _contract()
        routing = raw["handler_routing"]
        assert routing["routing_strategy"] == "operation_match"
        handlers = routing["handlers"]
        assert len(handlers) == 1
        assert handlers[0]["operation"] == "publish_generated_node"

    def test_validate_routing_reports_no_errors(self) -> None:
        raw = _contract()
        eb = raw.get("event_bus", {}) or {}
        errors = RuntimeLocal._validate_routing(
            raw["handler_routing"],
            eb.get("subscribe_topics", []) or [],
            eb.get("publish_topics", []) or [],
        )
        assert errors == [], f"routing validation must be clean, got: {errors}"
        assert not any("operation is missing" in e for e in errors)

    def test_initial_payload_model_is_resolvable(self) -> None:
        raw = _contract()
        runtime = RuntimeLocal.__new__(RuntimeLocal)
        runtime._contract = raw
        spec = runtime._resolve_event_driven_payload_spec(raw["handler_routing"])
        assert spec is not None, (
            "no initial-payload model resolved -- node is headless-dead"
        )
        model_spec, _source = spec
        assert model_spec["class"] == "ModelGeneratedNodePublishInput"

    def test_contract_topics(self) -> None:
        raw = _contract()
        eb = raw["event_bus"]
        assert CMD_TOPIC in eb["subscribe_topics"]
        assert COMPLETED_TOPIC in eb["publish_topics"]
        assert raw["terminal_event"] == COMPLETED_TOPIC


@pytest.mark.unit
class TestPublishGoldenChain:
    """start payload -> worktree+commit+push -> gh pr create -> completed result."""

    async def test_single_payload_handler_publishes(self, tmp_path: Path) -> None:
        staging = _staged_package(tmp_path)
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        pr_url = "https://github.com/OmniNode-ai/omnimarket/pull/12345"
        run_fn = _make_run(
            [
                (0, "", ""),  # git worktree add
                (0, "", ""),  # git add
                (0, "", ""),  # git commit
                (0, "", ""),  # git push
                (0, pr_url + "\n", ""),  # gh pr create
            ]
        )
        published: list[tuple[str, bytes]] = []
        handler = HandlerGeneratedNodePublishEffect(
            run_fn=run_fn,
            repo_root_resolver=lambda _repo: repo_root,
            event_publisher=lambda t, p: published.append((t, p)),
        )

        result = await handler.handle(_input(staging))

        assert isinstance(result, ModelGeneratedNodePublishResult)
        assert result.published is True
        assert result.pr_url == pr_url
        assert result.branch is not None
        assert result.branch.startswith(f"generated/{NODE_NAME}-")
        assert published[0][0] == COMPLETED_TOPIC
        emitted = json.loads(published[0][1].decode("utf-8"))
        assert emitted["pr_url"] == pr_url
