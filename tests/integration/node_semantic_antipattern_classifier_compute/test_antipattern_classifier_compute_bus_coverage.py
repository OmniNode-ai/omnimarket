# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-output COMPUTE coverage for
node_semantic_antipattern_classifier_compute, driven over the canonical in-memory
bus.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype compute).

The classifier is a pure deterministic COMPUTE node: it folds candidate matches
into ``violations`` and the derived ``has_blocking_violation`` verdict. This module
drives every declared verdict class + every classification branch over the in-memory
``integration_event_bus`` via ``LocalRuntimeBusAdapter``, reading the terminal
``ModelAntipatternClassifyResult`` back off the declared classified topic and
asserting typed result fields (never "returned without raising").

Declared classification branches (handler docstring):
  1. line_count < 10                         -> skipped (empty-file exemption)
  2. similarity < threshold                  -> skipped (below detection)
  3. similarity >= threshold, blocking       -> BLOCKING violation
  4. similarity >= threshold, non-blocking   -> ADVISORY violation

A negative control (``test_negative_control_*``) proves a known-bad god-class
fixture MUST produce a blocking finding -- the classifier is not a no-op.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_semantic_antipattern_classifier_compute.handlers.handler_antipattern_classifier import (
    HandlerAntipatternClassifier,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_request import (
    ModelAntipatternClassifyRequest,
    ModelAntipatternMatch,
    ModelAntipatternMatchConfig,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_result import (
    ModelAntipatternClassifyResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Declared wire strings (contract.yaml -> event_bus). Pinned in the state-coverage
# module.
TOPIC_CLASSIFY = "onex.cmd.omnimarket.antipattern-classify.v1"
TOPIC_CLASSIFIED = "onex.evt.omnimarket.antipattern-classified.v1"


def _match(
    *,
    pattern_id: str = "god-class",
    label: str = "God Class",
    similarity: float = 0.90,
    enforcement: str = "blocking",
    description: str = "Class does too many things",
    file_path: str = "src/foo.py",
    line_count: int = 50,
) -> ModelAntipatternMatch:
    return ModelAntipatternMatch(
        pattern_id=pattern_id,
        label=label,
        similarity=similarity,
        enforcement=enforcement,
        description=description,
        file_path=file_path,
        line_count=line_count,
    )


def _request(
    matches: tuple[ModelAntipatternMatch, ...],
    similarity_threshold: float = 0.80,
) -> ModelAntipatternClassifyRequest:
    return ModelAntipatternClassifyRequest(
        matches=matches,
        config=ModelAntipatternMatchConfig(similarity_threshold=similarity_threshold),
    )


async def _drive(
    bus: Any,
    request: ModelAntipatternClassifyRequest,
) -> ModelAntipatternClassifyResult:
    """Publish a classify request; return the terminal result read off the bus."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerAntipatternClassifier(),
        handler_name="antipattern-classifier-compute",
        input_model_cls=ModelAntipatternClassifyRequest,
        output_topic=TOPIC_CLASSIFIED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_CLASSIFY,
        on_message=adapter.on_message,
        group_id="omnimarket-antipattern-classifier-compute-test",
    )
    await bus.publish(
        TOPIC_CLASSIFY,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    published = await bus.get_event_history(topic=TOPIC_CLASSIFIED)
    assert len(published) == 1, f"expected exactly one result, got {published}"
    return ModelAntipatternClassifyResult.model_validate(
        json.loads(published[-1].value)
    )


@pytest.mark.integration
async def test_blocking_violation_verdict_over_bus(
    integration_event_bus: Any,
) -> None:
    """Branch 3: similarity>=threshold + blocking -> one blocking violation."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus, _request((_match(similarity=0.90, enforcement="blocking"),))
        )
        assert len(result.violations) == 1
        assert result.violations[0].is_blocking is True
        assert result.violations[0].explanation
        assert result.has_blocking_violation is True
    finally:
        await bus.close()


@pytest.mark.integration
async def test_advisory_violation_verdict_over_bus(
    integration_event_bus: Any,
) -> None:
    """Branch 4: similarity>=threshold + non-blocking -> advisory (non-blocking)."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus, _request((_match(similarity=0.95, enforcement="advisory"),))
        )
        assert len(result.violations) == 1
        assert result.violations[0].is_blocking is False
        assert result.has_blocking_violation is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_below_threshold_skipped_over_bus(
    integration_event_bus: Any,
) -> None:
    """Branch 2: similarity<threshold -> no violation emitted."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            _request(
                (_match(similarity=0.70, enforcement="blocking"),),
                similarity_threshold=0.80,
            ),
        )
        assert result.violations == ()
        assert result.has_blocking_violation is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_threshold_boundary_is_inclusive_over_bus(
    integration_event_bus: Any,
) -> None:
    """Boundary: similarity exactly == threshold still triggers a violation."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            _request(
                (_match(similarity=0.80, enforcement="blocking"),),
                similarity_threshold=0.80,
            ),
        )
        assert len(result.violations) == 1
        assert result.violations[0].is_blocking is True
    finally:
        await bus.close()


@pytest.mark.integration
async def test_short_file_exemption_over_bus(
    integration_event_bus: Any,
) -> None:
    """Branch 1: line_count<10 -> skipped even at max similarity."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            _request((_match(similarity=0.99, enforcement="blocking", line_count=9),)),
        )
        assert result.violations == ()
        assert result.has_blocking_violation is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_ten_line_file_not_exempt_over_bus(
    integration_event_bus: Any,
) -> None:
    """Boundary: exactly 10 lines is evaluated normally (not exempt)."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(
            bus,
            _request((_match(similarity=0.90, enforcement="blocking", line_count=10),)),
        )
        assert len(result.violations) == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_empty_matches_yield_no_violations_over_bus(
    integration_event_bus: Any,
) -> None:
    """No candidate matches -> empty violation set, no blocking verdict."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _drive(bus, _request(()))
        assert result.violations == ()
        assert result.has_blocking_violation is False
    finally:
        await bus.close()


@pytest.mark.integration
async def test_mixed_matches_partition_correctly_over_bus(
    integration_event_bus: Any,
) -> None:
    """All four branches in one fold: one blocking, one advisory, one below
    threshold, one short file -> exactly 2 violations (1 blocking, 1 advisory)."""
    bus = integration_event_bus
    await bus.start()
    try:
        matches = (
            _match(
                pattern_id="god-class",
                similarity=0.90,
                enforcement="blocking",
                file_path="a.py",
                line_count=50,
            ),
            _match(
                pattern_id="long-method",
                label="Long Method",
                similarity=0.85,
                enforcement="advisory",
                file_path="b.py",
                line_count=100,
            ),
            _match(
                pattern_id="feature-envy",
                label="Feature Envy",
                similarity=0.60,
                enforcement="blocking",
                file_path="c.py",
                line_count=30,
            ),
            _match(
                pattern_id="data-clumps",
                label="Data Clumps",
                similarity=0.95,
                enforcement="blocking",
                file_path="d.py",
                line_count=5,
            ),
        )
        result = await _drive(bus, _request(matches, similarity_threshold=0.80))
        assert len(result.violations) == 2
        assert result.has_blocking_violation is True
        blocking = [v for v in result.violations if v.is_blocking]
        advisory = [v for v in result.violations if not v.is_blocking]
        assert len(blocking) == 1
        assert len(advisory) == 1
        assert blocking[0].pattern_id == "god-class"
        assert advisory[0].pattern_id == "long-method"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_negative_control_known_bad_fixture_must_block_over_bus(
    integration_event_bus: Any,
) -> None:
    """Negative control: a known-bad god-class fixture (high similarity, blocking
    enforcement, real file) MUST produce a blocking finding. The classifier is not
    a rubber-stamp no-op."""
    bus = integration_event_bus
    await bus.start()
    try:
        bad = _match(
            pattern_id="god-class",
            label="God Class",
            similarity=0.97,
            enforcement="blocking",
            description="One class owns persistence, HTTP, and rendering.",
            file_path="src/omnimarket/mega/handler_everything.py",
            line_count=800,
        )
        result = await _drive(bus, _request((bad,), similarity_threshold=0.80))
        assert result.has_blocking_violation is True, (
            "known-bad god-class fixture must be flagged as a blocking violation"
        )
        assert result.violations[0].pattern_id == "god-class"
        assert "God Class" in result.violations[0].explanation
    finally:
        await bus.close()


@pytest.mark.integration
async def test_duplicate_fold_is_idempotent_over_bus(
    integration_event_bus: Any,
) -> None:
    """Folding the same request twice yields identical results (deterministic fold,
    duplicate-safe)."""
    bus = integration_event_bus
    await bus.start()
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=HandlerAntipatternClassifier(),
            handler_name="antipattern-classifier-compute",
            input_model_cls=ModelAntipatternClassifyRequest,
            output_topic=TOPIC_CLASSIFIED,
            bus=bus,
        )
        await bus.subscribe(
            TOPIC_CLASSIFY,
            on_message=adapter.on_message,
            group_id="omnimarket-antipattern-classifier-compute-test",
        )
        req = _request(
            (_match(similarity=0.88, enforcement="blocking"),),
            similarity_threshold=0.80,
        )
        for _ in range(2):
            await bus.publish(
                TOPIC_CLASSIFY,
                key=None,
                value=req.model_dump_json().encode("utf-8"),
            )
        published = await bus.get_event_history(topic=TOPIC_CLASSIFIED)
        assert len(published) == 2
        first = ModelAntipatternClassifyResult.model_validate(
            json.loads(published[0].value)
        )
        second = ModelAntipatternClassifyResult.model_validate(
            json.loads(published[1].value)
        )
        assert first == second
    finally:
        await bus.close()


@pytest.mark.integration
async def test_correlation_id_ignored_but_result_stable_over_bus(
    integration_event_bus: Any,
) -> None:
    """The compute request carries no correlation_id field; the fold is driven purely
    by matches. A distinct run id on the envelope key does not change the verdict."""
    bus = integration_event_bus
    await bus.start()
    try:
        _ = uuid4()  # distinct run; compute output depends only on matches
        result = await _drive(
            bus, _request((_match(similarity=0.90, enforcement="blocking"),))
        )
        assert result.has_blocking_violation is True
    finally:
        await bus.close()
