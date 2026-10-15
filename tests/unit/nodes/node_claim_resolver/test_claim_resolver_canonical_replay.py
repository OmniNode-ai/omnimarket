# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical dispatch regression against outputs recorded from the legacy resolver."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_claim_resolver.models import (
    ModelClaimResolutionRequest,
    ModelClaimResolutionResponse,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _ROOT / "tests/fixtures/golden/node_claim_resolver"
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_claim_resolver/contract.yaml"


@pytest.mark.parametrize(
    "input_path", sorted(_FIXTURES.glob("*.input.json")), ids=lambda p: p.stem
)
def test_contract_handler_replays_legacy(input_path: Path) -> None:
    """The contract-declared class accepts one typed request at its runtime entrypoint."""
    binding = yaml.safe_load(_CONTRACT.read_text())["handler"]
    cls = getattr(importlib.import_module(binding["module"]), binding["class"])
    handler = cls()
    assert callable(getattr(handler, "handle", None)), (
        "runtime handle entrypoint is absent"
    )
    request = ModelClaimResolutionRequest.model_validate_json(input_path.read_text())
    result = handler.handle(request)
    expected = json.loads((_FIXTURES / "expected.json").read_text())
    assert isinstance(result, ModelClaimResolutionResponse)
    assert (
        result.model_dump(mode="json")
        == expected[input_path.name.removesuffix(".input.json")]
    )


def test_mismatch_oracle_discriminates() -> None:
    """Changing a recorded result changes the expected mismatch response."""
    expected = json.loads((_FIXTURES / "expected.json").read_text())["blocker_missing"]
    changed = json.loads(json.dumps(expected))
    changed["mismatches"] = []
    assert changed != expected


def test_canonical_shape_and_baselines() -> None:
    """One typed payload enters the contract handler; both debt lists retire it."""
    binding = yaml.safe_load(_CONTRACT.read_text())["handler"]
    cls = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert callable(getattr(cls, "handle", None)), "runtime handle entrypoint is absent"
    assert list(inspect.signature(cls().handle).parameters) == ["request"]
    shape = ast.parse(
        (_ROOT / "scripts/ci/canonical_handler_shape_baseline.py").read_text()
    )
    baseline = next(
        ast.literal_eval(item.value)
        for item in shape.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id == "NON_CANONICAL"
    )
    assert "omnimarket.nodes.node_claim_resolver" not in baseline
    entrypoints = yaml.safe_load(
        (_ROOT / "validation/handler_dispatch_entrypoint_baseline.yaml").read_text()
    )
    assert all(
        row["handler"] != binding["class"]
        for row in entrypoints["known_entrypointless"]
    )


def test_rewrite_attempt_accounting() -> None:
    """The rejected draft remains in the denominator of the recorded acceptance rate."""
    record = json.loads((_FIXTURES / "rewrite_attempts.json").read_text())
    outcomes = [attempt["outcome"] for attempt in record["attempts"]]
    assert outcomes == ["rejected", "accepted"]
    accepted = outcomes.count("accepted")
    rejected = outcomes.count("rejected")
    assert accepted + rejected == len(outcomes)
    assert accepted / len(outcomes) == 0.5
    assert accepted / len(outcomes) < record["target_acceptance_fraction"] == 0.7
    proof = json.loads(
        (
            _ROOT
            / "scripts/ci/adequacy_receipts"
            / f"{record['node_id']}.equivalence.json"
        ).read_text()
    )
    assert proof["node_id"] == record["node_id"]
    assert proof["status"] == "pass"


@pytest.mark.asyncio
async def test_completed_terminal_reachable_over_runtime_bus(tmp_path: Path) -> None:
    """The contract's completed terminal is reachable through the shared runtime."""
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.claim-resolver-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    request_path = tmp_path / "request.json"
    request_path.write_text(ModelClaimResolutionRequest().model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=_CONTRACT,
        input_path=request_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.COMPLETED
    result = runtime.handler_result
    assert isinstance(result, ModelClaimResolutionResponse)
    assert result.results == ()
    assert result.mismatches == ()
