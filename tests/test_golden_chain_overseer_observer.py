# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_overseer_observer Phase-0 stubs.

Verifies SideEffectObserver and EvidenceEvaluator are importable,
their null implementations pass isinstance checks against the base classes,
and their basic contracts hold.

OMN-12951: EvidenceEvaluator and SideEffectObserver must NOT be
typing.Protocol classes. The runtime handler resolver instantiates
handler_cls zero-arg; a Protocol target raises
"TypeError: Protocols cannot be instantiated" and crash-loops
bootstrap on infra builds predating the OMN-12501 quarantine guard.

Related:
    - OMN-8506: stub side-effect observer + evidence evaluator interfaces
    - OMN-12951: port from typing.Protocol to concrete ABC base classes
"""

from __future__ import annotations

import inspect
import typing

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


# OMN-12951: Protocol-instantiation crash guard tests
# These tests verify the durable fix: EvidenceEvaluator and SideEffectObserver
# must NOT be typing.Protocol subclasses so the runtime can safely inspect them.


@pytest.mark.unit
def test_evidence_evaluator_is_not_typing_protocol() -> None:
    """EvidenceEvaluator must not be a typing.Protocol (OMN-12951).

    A typing.Protocol raises TypeError on zero-arg instantiation, crashing
    infra 0.37.0 bootstrap when handler_wiring lacks the quarantine guard.
    """
    protocol_meta = type(typing.Protocol)
    assert not isinstance(EvidenceEvaluator, protocol_meta) or not getattr(
        EvidenceEvaluator, "_is_protocol", False
    ), (
        "EvidenceEvaluator must not be a typing.Protocol — "
        "the runtime instantiates handler_cls zero-arg and Protocols cannot be instantiated"
    )


@pytest.mark.unit
def test_side_effect_observer_is_not_typing_protocol() -> None:
    """SideEffectObserver must not be a typing.Protocol (OMN-12951).

    Same crash vector as EvidenceEvaluator.
    """
    assert not getattr(SideEffectObserver, "_is_protocol", False), (
        "SideEffectObserver must not be a typing.Protocol — "
        "the runtime instantiates handler_cls zero-arg and Protocols cannot be instantiated"
    )


@pytest.mark.unit
def test_evidence_evaluator_is_abstract() -> None:
    """EvidenceEvaluator is an ABC — direct instantiation raises TypeError."""
    with pytest.raises(TypeError):
        EvidenceEvaluator()  # type: ignore[abstract]


@pytest.mark.unit
def test_side_effect_observer_is_abstract() -> None:
    """SideEffectObserver is an ABC — direct instantiation raises TypeError."""
    with pytest.raises(TypeError):
        SideEffectObserver()  # type: ignore[abstract]


@pytest.mark.unit
def test_null_evidence_evaluator_is_concrete() -> None:
    """NullEvidenceEvaluator is concrete and inherits from EvidenceEvaluator."""
    assert not inspect.isabstract(NullEvidenceEvaluator)
    ev = NullEvidenceEvaluator()
    assert isinstance(ev, EvidenceEvaluator)


@pytest.mark.unit
def test_null_side_effect_observer_is_concrete() -> None:
    """NullSideEffectObserver is concrete and inherits from SideEffectObserver."""
    assert not inspect.isabstract(NullSideEffectObserver)
    obs = NullSideEffectObserver()
    assert isinstance(obs, SideEffectObserver)
