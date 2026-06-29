# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain tests for node_auto_merge_effect (OMN-13530).

Verifies the EFFECT node dispatches headless: the contract declares a non-empty
``operation`` on its operation_match handler entry AND a resolvable initial-payload
model, and ``HandlerAutoMergeEffect.handle`` accepts a single typed
``ModelAutoMergeInput`` payload (the RuntimeLocal event-driven dispatch shape).

Regression context: omnibase_core 0.46.0 hardened RuntimeLocal._validate_routing to
require ``operation`` on operation_match handler entries. This contract omitted it,
so the node failed closed at startup with ``handlers[0].operation is missing`` /
``result=failed`` and the auto_merge skill was dead headless. This chain locks in the
fix: operation present + initial-payload model resolvable + single-payload handler.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_auto_merge_effect.handlers.handler_auto_merge_effect import (
    HandlerAutoMergeEffect,
)
from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_input import (
    ModelAutoMergeInput,
)
from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_result import (
    ModelAutoMergeResult,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_auto_merge_effect/contract.yaml"
)
CMD_TOPIC = "onex.cmd.omnimarket.auto-merge-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.pr-merged.v1"
REPO = "OmniNode-ai/omnimarket"
PR_NUM = 42


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _input(**overrides: object) -> ModelAutoMergeInput:
    base: dict[str, object] = {
        "correlation_id": uuid4(),
        "pr_number": PR_NUM,
        "repo": REPO,
    }
    base.update(overrides)
    return ModelAutoMergeInput(**base)


def _make_run(responses: list[tuple[int, str, str]]):
    idx = 0

    def _run(_cmd: list[str]) -> tuple[int, str, str]:
        nonlocal idx
        rc, out, err = responses[idx]
        idx += 1
        return rc, out, err

    return _run


def _pr_view(merge_state: str = "CLEAN", review_decision: str = "APPROVED") -> str:
    return json.dumps(
        {
            "mergeStateStatus": merge_state,
            "statusCheckRollup": [],
            "reviewDecision": review_decision,
            "latestReviews": [],
        }
    )


@pytest.mark.unit
class TestAutoMergeContractRouting:
    """OMN-13530: operation_match entry has operation + resolvable payload model."""

    def test_contract_declares_operation_on_operation_match_entry(self) -> None:
        raw = _contract()
        routing = raw["handler_routing"]
        assert routing["routing_strategy"] == "operation_match"
        handlers = routing["handlers"]
        assert len(handlers) == 1
        # The regression: operation_match entries MUST declare a non-empty operation.
        assert handlers[0]["operation"] == "auto_merge"

    def test_validate_routing_reports_no_errors(self) -> None:
        """The exact RuntimeLocal gate that failed closed before the fix."""
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
        """RuntimeLocal must resolve an initial-payload model for headless dispatch."""
        raw = _contract()
        runtime = RuntimeLocal.__new__(RuntimeLocal)
        runtime._contract = raw
        spec = runtime._resolve_event_driven_payload_spec(raw["handler_routing"])
        assert spec is not None, (
            "no initial-payload model resolved — node is headless-dead"
        )
        model_spec, _source = spec
        assert model_spec["class"] == "ModelAutoMergeInput"

    def test_contract_topics(self) -> None:
        raw = _contract()
        eb = raw["event_bus"]
        assert CMD_TOPIC in eb["subscribe_topics"]
        assert COMPLETED_TOPIC in eb["publish_topics"]
        assert raw["terminal_event"] == COMPLETED_TOPIC


@pytest.mark.unit
class TestAutoMergeGoldenChain:
    """start payload -> gate checks -> merge -> completed result."""

    async def test_single_payload_handler_merges_clean_pr(self) -> None:
        """handle(ModelAutoMergeInput) is the RuntimeLocal single-payload shape."""
        payload = _input()
        responses = [
            (0, _pr_view("CLEAN", "APPROVED"), ""),  # fetch mergeStateStatus
            (0, _pr_view("CLEAN", "APPROVED"), ""),  # CodeRabbit gate
            (0, "", ""),  # execute merge
            (0, json.dumps({"mergeCommit": {"oid": "deadbeef1234"}}), ""),  # sha
            (0, json.dumps({"headRefName": "jonah/fix-no-ticket"}), ""),  # branch
        ]
        handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
        handler._sleep = lambda _s: None

        result = await handler.handle(payload)

        assert isinstance(result, ModelAutoMergeResult)
        assert result.merged is True
        assert result.merge_commit_sha == "deadbeef1234"
        assert result.correlation_id == payload.correlation_id

    async def test_single_payload_handler_blocks_dirty_pr(self) -> None:
        payload = _input()
        handler = HandlerAutoMergeEffect(run_fn=_make_run([(0, _pr_view("DIRTY"), "")]))
        handler._sleep = lambda _s: None

        result = await handler.handle(payload)

        assert result.merged is False
        assert "merge conflicts" in (result.blocked_reason or "")
