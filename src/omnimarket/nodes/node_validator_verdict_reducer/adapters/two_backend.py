# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's in-memory store factory and the scenario that compares its backends (OMN-23078).

The real (file) factory is make_reducer_file_backend in
node_validator_workflow_orchestrator.adapters.verdict_store, beside the other file effects.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_validator_verdict_reducer.adapters.verdict_store import (
    InMemoryVerdictStore,
)
from omnimarket.nodes.node_validator_verdict_reducer.handlers.handler_validator_verdict_reducer import (
    HandlerValidatorVerdictReducer,
)
from omnimarket.nodes.node_validator_verdict_reducer.models.model_validator_verdict import (
    ModelRatchetReading,
    ModelValidatorOutcome,
    ModelVerdictFoldRequest,
)
from omnimarket.nodes.node_validator_verdict_reducer.overlay import example_overlay
from omnimarket.nodes.node_validator_verdict_reducer.protocols import (
    ProtocolVerdictStore,
)

RATCHET_KEY = "canonical_file_shape.baseline_entries"


def make_in_memory_backend(workdir: Path) -> ProtocolVerdictStore:
    return InMemoryVerdictStore()


def _request(pr: int, value: int) -> ModelVerdictFoldRequest:
    return ModelVerdictFoldRequest(
        correlation_id=f"suite-{pr}",
        repo="example/repo",
        pr=pr,
        head=f"{pr:040x}",
        outcomes=(
            ModelValidatorOutcome(
                validator_id="canonical_file_shape", canonical=True, failing_count=0
            ),
        ),
        ratchet_readings=(ModelRatchetReading(key=RATCHET_KEY, value=value),),
    )


def scenario_ratchet_roundtrip(
    factory: Callable[[Path], ProtocolVerdictStore], workdir: Path
) -> object:
    """Seed 5, shrink to 4, try 6; both backends must hold 4 and refuse the growth."""
    store = factory(workdir)
    overlay = example_overlay()
    verdicts = []
    for pr, value in ((1, 5), (2, 4), (3, 6)):
        handler = HandlerValidatorVerdictReducer(overlay=overlay, store=store)
        verdicts.append(handler.handle(_request(pr, value)).verdict.verdict.value)
    return {"verdicts": verdicts, "baseline": store.load().baselines[RATCHET_KEY].value}
