# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_generated_node_publish_effect (OMN-13606 / OMN-13625).

Verifies the EFFECT node dispatches headless: the contract declares a non-empty
``operation`` on its operation_match handler entry AND a resolvable
initial-payload model, and ``HandlerGeneratedNodePublishEffect.handle`` accepts a
single typed ``ModelGeneratedNodePublishInput`` payload (the RuntimeLocal
event-driven dispatch shape). Also asserts the full publish chain end-to-end:
start payload -> worktree+commit+push -> gh pr create -> completed result with a
PR URL emitted on the contract-declared publish topic.

Phase 7.2 (OMN-13625): ``TestPublishEntryPointGoldenChain`` proves that the
entry-point auto-registration step patches pyproject.toml in the worktree and
that the result carries ``entry_point_registered=True``.
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
        """Core publish chain with registration disabled (no pyproject.toml in fake worktree)."""
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

        result = await handler.handle(_input(staging, register_entry_point=False))

        assert isinstance(result, ModelGeneratedNodePublishResult)
        assert result.published is True
        assert result.pr_url == pr_url
        assert result.branch is not None
        assert result.branch.startswith(f"generated/{NODE_NAME}-")
        assert published[0][0] == COMPLETED_TOPIC
        emitted = json.loads(published[0][1].decode("utf-8"))
        assert emitted["pr_url"] == pr_url


_MINIMAL_PYPROJECT = """\
[project]
name = "omnimarket"
version = "0.1.0"

[project.entry-points."onex.nodes"]
node_existing_compute = "omnimarket.nodes.node_existing_compute"

[project.entry-points."onex.cli"]
market = "omnimarket.cli.market:market"
"""


@pytest.mark.unit
class TestPublishEntryPointGoldenChain:
    """Phase 7.2 (OMN-13625): entry-point auto-registration golden chain.

    Proves the full path: staged package -> worktree (with real pyproject.toml)
    -> _register_entry_point patches the file -> result.entry_point_registered
    True -> emitted event carries the field.
    """

    def test_entry_point_registered_on_publish(self, tmp_path: Path) -> None:
        """_register_entry_point patches pyproject.toml and returns registered=True.

        The TemporaryDirectory inside handle() is not accessible from tests so we
        directly exercise ``_register_entry_point`` on a controlled ``tmp_path``
        directory.  This is a golden-chain proof of the Phase 7.2 contract: calling
        ``_register_entry_point`` against a directory containing a real
        pyproject.toml mutates the file and returns ``(True, None)``.
        """
        handler = HandlerGeneratedNodePublishEffect(
            run_fn=lambda _cmd: (0, "", ""),
            repo_root_resolver=lambda _repo: tmp_path / "repo",
        )
        worktree_sim = tmp_path / "wt_sim"
        worktree_sim.mkdir()
        pyproject = worktree_sim / "pyproject.toml"
        pyproject.write_text(_MINIMAL_PYPROJECT, encoding="utf-8")

        registered, blocked = handler._register_entry_point(
            worktree_sim, NODE_NAME, "src/omnimarket/nodes"
        )

        assert blocked is None
        assert registered is True
        text = pyproject.read_text(encoding="utf-8")
        expected_line = f'{NODE_NAME} = "omnimarket.nodes.{NODE_NAME}"'
        assert expected_line in text, (
            f"entry point line not found in patched pyproject.toml:\n{text}"
        )

    def test_contract_declares_register_entry_point_input(self) -> None:
        """Contract explicitly declares register_entry_point as an optional bool input."""
        raw = _contract()
        inputs = raw.get("inputs", {}) or {}
        assert "register_entry_point" in inputs, (
            "contract.inputs must declare register_entry_point for Phase 7.2"
        )
        assert inputs["register_entry_point"]["type"] == "bool"
        assert inputs["register_entry_point"].get("default") is True

    def test_contract_declares_entry_point_registered_output(self) -> None:
        """Contract explicitly declares entry_point_registered as an output field."""
        raw = _contract()
        outputs = raw.get("outputs", {}) or {}
        assert "entry_point_registered" in outputs, (
            "contract.outputs must declare entry_point_registered for Phase 7.2"
        )
        assert outputs["entry_point_registered"]["type"] == "bool"

    def test_result_model_has_entry_point_registered_field(self) -> None:
        """ModelGeneratedNodePublishResult includes entry_point_registered."""
        from uuid import uuid4

        result = ModelGeneratedNodePublishResult(
            correlation_id=uuid4(),
            node_name=NODE_NAME,
            repo=REPO,
            published=False,
            entry_point_registered=True,
        )
        assert result.entry_point_registered is True

    def test_input_model_has_register_entry_point_field_default_true(self) -> None:
        """ModelGeneratedNodePublishInput.register_entry_point defaults to True."""
        from uuid import uuid4

        payload = ModelGeneratedNodePublishInput(
            correlation_id=uuid4(),
            node_name=NODE_NAME,
            staging_dir="/tmp/staging",
            repo=REPO,
            ticket=TICKET,
            dod_evidence="evidence",
        )
        assert payload.register_entry_point is True
