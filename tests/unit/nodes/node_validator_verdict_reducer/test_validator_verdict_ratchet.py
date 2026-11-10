# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-23078): a ratchet baseline in the reducer moves only in its allowed direction."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.models.model_validator_verdict import (
    EnumPrVerdict,
    EnumRatchetDirection,
    ModelRatchetReading,
    ModelValidatorOutcome,
    ModelVerdictFoldRequest,
    ModelVerdictFoldResult,
    ModelVerdictState,
)
from omnimarket.nodes.node_validator_verdict_reducer.adapters import two_backend
from omnimarket.nodes.node_validator_verdict_reducer.adapters.verdict_store import (
    InMemoryVerdictStore,
)
from omnimarket.nodes.node_validator_verdict_reducer.handlers.handler_validator_verdict_reducer import (
    REASON_AGAINST_DIRECTION,
    REASON_UNDECLARED,
    HandlerValidatorVerdictReducer,
    fold,
)
from omnimarket.nodes.node_validator_verdict_reducer.overlay import (
    ModelRatchetDeclaration,
    ModelVerdictReducerOverlay,
    example_overlay,
)
from omnimarket.nodes.node_validator_verdict_reducer.protocols import (
    ProtocolVerdictStore,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.adapters.verdict_store import (
    FileVerdictStore,
    make_reducer_file_backend,
    store_from_overlay,
)

pytestmark = pytest.mark.unit

KEY = "canonical_file_shape.baseline_entries"


def request(
    pr: int, *readings: tuple[str, int], failing: int = 0
) -> ModelVerdictFoldRequest:
    return ModelVerdictFoldRequest(
        correlation_id=f"c-{pr}",
        repo="example/repo",
        pr=pr,
        head=f"h{pr}",
        outcomes=(
            ModelValidatorOutcome(
                validator_id="canonical_file_shape",
                canonical=True,
                failing_count=failing,
            ),
        ),
        ratchet_readings=tuple(
            ModelRatchetReading(key=k, value=v) for k, v in readings
        ),
    )


def run(
    *requests: ModelVerdictFoldRequest,
    overlay: ModelVerdictReducerOverlay | None = None,
) -> tuple[list[ModelVerdictFoldResult], ModelVerdictState]:
    overlay = overlay or example_overlay()
    state = ModelVerdictState()
    results: list[ModelVerdictFoldResult] = []
    for r in requests:
        result = fold(state, r, overlay)
        state = result.state
        results.append(result)
    return results, state


def test_ratchet_first_reading_seeds_then_shrink_moves_it() -> None:
    results, state = run(request(1, (KEY, 5)), request(2, (KEY, 3)))
    assert [r.verdict.verdict for r in results] == [EnumPrVerdict.PASS] * 2
    assert state.baselines[KEY].value == 3
    assert [(m.before, m.after) for r in results for m in r.moves] == [
        (None, 5),
        (5, 3),
    ]


def test_ratchet_planted_growth_is_refused_and_the_baseline_holds() -> None:
    results, state = run(request(1, (KEY, 5)), request(2, (KEY, 6)))
    refused = results[1].verdict
    assert refused.verdict is EnumPrVerdict.FAIL
    assert [
        (r.key, r.reason, r.baseline, r.proposed) for r in refused.refused_ratchets
    ] == [(KEY, REASON_AGAINST_DIRECTION, 5, 6)]
    assert results[1].moves == ()
    assert state.baselines[KEY].value == 5


def test_ratchet_an_equal_reading_passes_without_a_move() -> None:
    results, state = run(request(1, (KEY, 5)), request(2, (KEY, 5)))
    assert results[1].verdict.verdict is EnumPrVerdict.PASS
    assert results[1].moves == ()
    assert state.baselines[KEY].value == 5


def test_ratchet_grow_only_refuses_a_shrink() -> None:
    overlay = example_overlay().model_copy(
        update={
            "ratchets": (
                ModelRatchetDeclaration(
                    key="covered", direction=EnumRatchetDirection.GROW_ONLY
                ),
            )
        }
    )
    results, state = run(
        request(1, ("covered", 10)), request(2, ("covered", 9)), overlay=overlay
    )
    assert results[1].verdict.verdict is EnumPrVerdict.FAIL
    assert state.baselines["covered"].value == 10


def test_ratchet_an_undeclared_key_is_refused_not_seeded() -> None:
    results, state = run(request(1, ("unknown.key", 1)))
    assert results[0].verdict.refused_ratchets[0].reason == REASON_UNDECLARED
    assert "unknown.key" not in state.baselines


def test_ratchet_refusal_survives_a_file_store_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    overlay = example_overlay()
    HandlerValidatorVerdictReducer(
        overlay=overlay, store=FileVerdictStore(path)
    ).handle(request(1, (KEY, 4)))
    second = HandlerValidatorVerdictReducer(
        overlay=overlay, store=FileVerdictStore(path)
    )
    assert second.handle(request(2, (KEY, 7))).verdict.verdict is EnumPrVerdict.FAIL
    assert FileVerdictStore(path).load().baselines[KEY].value == 4


@pytest.mark.parametrize(
    "factory", [two_backend.make_in_memory_backend, make_reducer_file_backend]
)
def test_ratchet_roundtrip_is_the_same_on_both_backends(
    factory: Callable[[Path], ProtocolVerdictStore], tmp_path: Path
) -> None:
    assert two_backend.scenario_ratchet_roundtrip(factory, tmp_path) == {
        "verdicts": ["pass", "pass", "fail"],
        "baseline": 4,
    }


def test_verdict_fails_on_a_failing_validator_and_counts_canonical_validators() -> None:
    results, state = run(request(1, failing=2))
    assert results[0].verdict.failing_validators == ("canonical_file_shape",)
    assert results[0].verdict.verdict is EnumPrVerdict.FAIL
    assert state.canonical_validator_count == 1


def test_store_from_overlay_file_backend_needs_its_path(tmp_path: Path) -> None:
    overlay = example_overlay()
    assert isinstance(store_from_overlay(overlay, {}), InMemoryVerdictStore)
    file_overlay = overlay.model_copy(
        update={"store": overlay.store.model_copy(update={"backend": "file"})}
    )
    with pytest.raises(ValueError, match=overlay.store.path_env):
        store_from_overlay(file_overlay, {})
    store = store_from_overlay(
        file_overlay, {overlay.store.path_env: str(tmp_path / "s.json")}
    )
    assert isinstance(store, FileVerdictStore)
