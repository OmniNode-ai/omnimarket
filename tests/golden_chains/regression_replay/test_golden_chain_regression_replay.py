# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-derived golden chains for node_regression_test_orchestrator (OMN-13616).

These are *golden chains*, not ad-hoc unit tests: each chain is a JSON fixture
(a typed regression-suite start carrying a recorded replay corpus + the expected
canonical ModelExperimentResult end-state) that we **replay deterministically**
through the *real-dispatch-path* handler resolved via the canonical
``omnibase_core`` contract loader (the same loader the runtime uses). The
experiment result is asserted **byte-for-byte** against the fixture, so any drift
in the replay semantics, the aggregation, or the canonical result schema fails
the chain.

Two DoD-mandated chains:

  * POSITIVE — every task has a non-empty recorded output -> all pass -> score
    1.0, status COMPLETED.
  * NEGATIVE — one task records an empty output and one task has no recorded
    entry at all -> both fail -> fractional score, status FAILED.

Each chain is replayed twice and asserted identical, proving determinism: given
a recorded corpus the regression suite replays to the same result every time.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.contracts.contract_loader import load_contract
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)

from omnimarket.nodes.node_regression_test_orchestrator.handlers.handler_regression_test_orchestrator import (
    TOPIC_REGRESSION_COMPLETED,
    HandlerRegressionTestOrchestrator,
)
from omnimarket.nodes.node_regression_test_orchestrator.models.model_regression_suite_start import (
    ModelRegressionSuiteStart,
)

_CHAIN_DIR = Path(__file__).resolve().parent
_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_regression_test_orchestrator"
)
_SUBSCRIBE_TOPIC = "onex.cmd.omnimarket.regression-suite-start.v1"
_CHAIN_FILES = sorted(_CHAIN_DIR.glob("chain_*.json"))


def _load_chain(path: Path) -> dict[str, Any]:
    chain: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return chain


def _expected(chain: dict[str, Any]) -> Any:
    """Round-trip the fixture's expected result through the model so the compare
    is byte-for-byte against the model's canonical serialization (and the fixture
    itself is validated)."""
    result = ModelExperimentResult.model_validate(chain["expected_result"])
    return json.loads(result.model_dump_json())


def _resolve_wired_handler() -> type[HandlerRegressionTestOrchestrator]:
    """Resolve the handler class through the canonical contract loader.

    Real-dispatch-path: load the node's contract via the exact ``omnibase_core``
    loader the runtime uses, confirm the subscribe topic is the canonical
    constant, then resolve the handler class declared in ``handler_routing``
    through importlib. A handler never wired into the contract would fail here.
    """
    contract = load_contract(_NODE_DIR / "contract.yaml")

    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert _SUBSCRIBE_TOPIC in event_bus["subscribe_topics"]

    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    entry = routing["handlers"][0]
    handler_ref = entry["handler"]
    module = importlib.import_module(handler_ref["module"])
    resolved_cls = getattr(module, handler_ref["name"])
    assert resolved_cls is HandlerRegressionTestOrchestrator
    return HandlerRegressionTestOrchestrator


async def _replay_dispatch(chain: dict[str, Any]) -> ModelExperimentResult:
    """Replay the chain through the contract-loader-resolved handler dispatch."""
    handler = _resolve_wired_handler()()
    start = ModelRegressionSuiteStart.model_validate(chain["start"])
    envelope: ModelEventEnvelope[ModelRegressionSuiteStart] = ModelEventEnvelope(
        payload=start,
        correlation_id=start.correlation_id,
        event_type=_SUBSCRIBE_TOPIC,
    )
    output = await handler.handle(envelope)
    assert output.result is None, "ORCHESTRATOR must not return a result"
    assert len(output.events) == 1
    emitted = output.events[0]
    assert emitted.event_type == TOPIC_REGRESSION_COMPLETED
    assert isinstance(emitted.payload, ModelExperimentResult)
    return emitted.payload


@pytest.mark.integration
@pytest.mark.parametrize("chain_path", _CHAIN_FILES, ids=[p.stem for p in _CHAIN_FILES])
class TestRegressionReplayGoldenChains:
    """Replay each contract-derived chain through the real dispatch path."""

    def test_dispatch_replay_matches_golden_result(self, chain_path: Path) -> None:
        """The real-dispatch-path replay materializes the golden result byte-for-byte."""
        import asyncio

        chain = _load_chain(chain_path)
        actual = asyncio.run(_replay_dispatch(chain))
        assert json.loads(actual.model_dump_json()) == _expected(chain), (
            f"dispatch replay drifted from golden result for {chain['chain_name']}"
        )

    def test_replay_is_deterministic(self, chain_path: Path) -> None:
        """Two independent replays of the same corpus yield identical results."""
        import asyncio

        chain = _load_chain(chain_path)
        first = asyncio.run(_replay_dispatch(chain))
        second = asyncio.run(_replay_dispatch(chain))
        assert first.model_dump_json() == second.model_dump_json(), (
            f"replay non-deterministic for {chain['chain_name']}"
        )


@pytest.mark.integration
def test_at_least_one_positive_and_one_negative_chain() -> None:
    """The suite proves both an all-pass and a partial-fail replay."""
    statuses = {_load_chain(p)["expected_result"]["status"] for p in _CHAIN_FILES}
    assert "completed" in statuses
    assert "failed" in statuses
