# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_overseer_observer Phase-0 stubs.

Verifies SideEffectObserver and EvidenceEvaluator are importable,
their null implementations satisfy the Protocol isinstance checks,
and their basic contracts hold.

Related:
    - OMN-8506: stub side-effect observer + evidence evaluator interfaces
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_overseer_observer.handlers.handler_evidence_evaluator import (
    EvidenceEvaluator,
    NullEvidenceEvaluator,
)
from omnimarket.nodes.node_overseer_observer.handlers.handler_side_effect_observer import (
    NullSideEffectObserver,
    SideEffectObserver,
)


@pytest.mark.unit
def test_side_effect_observer_importable() -> None:
    assert SideEffectObserver is not None
    assert NullSideEffectObserver is not None


@pytest.mark.unit
def test_null_side_effect_observer_isinstance() -> None:
    obs = NullSideEffectObserver()
    assert isinstance(obs, SideEffectObserver)


@pytest.mark.unit
def test_null_side_effect_observer_record_and_get() -> None:
    obs = NullSideEffectObserver()
    assert obs.get_emissions() == []
    obs.record_emission(topic="onex.evt.test.v1", payload={"key": "val"})
    emissions = obs.get_emissions()
    assert len(emissions) == 1
    assert emissions[0]["topic"] == "onex.evt.test.v1"
    assert emissions[0]["payload"] == {"key": "val"}


@pytest.mark.unit
def test_null_side_effect_observer_get_returns_copy() -> None:
    obs = NullSideEffectObserver()
    obs.record_emission(topic="onex.evt.test.v1", payload={"k": "v"})
    first = obs.get_emissions()
    second = obs.get_emissions()
    assert first == second
    assert first is not second
    first[0]["payload"]["k"] = "changed"
    latest = obs.get_emissions()
    assert latest[0]["payload"]["k"] == "v"


@pytest.mark.unit
def test_evidence_evaluator_importable() -> None:
    assert EvidenceEvaluator is not None
    assert NullEvidenceEvaluator is not None


@pytest.mark.unit
def test_null_evidence_evaluator_isinstance() -> None:
    ev = NullEvidenceEvaluator()
    assert isinstance(ev, EvidenceEvaluator)


@pytest.mark.unit
def test_null_evidence_evaluator_always_passes() -> None:
    ev = NullEvidenceEvaluator()
    result = ev.evaluate(
        dod_evidence=[{"type": "pytest", "check": "uv run pytest"}],
        observed=[],
    )
    assert result is True


@pytest.mark.unit
def test_null_evidence_evaluator_empty_inputs() -> None:
    ev = NullEvidenceEvaluator()
    assert ev.evaluate(dod_evidence=[], observed=[]) is True
