# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the typed, category-weighted user-correction event (OMN-12846).

The correction event distinguishes a *context-selection failure*
(MISUNDERSTANDING) from a *new requirement* (NEW_INFORMATION), and is linked to
the context pack / factor subset that was in play. Category and failure axis are
both first-class fields — never collapsed into a single rolled-up score.

Anti-sycophancy invariant: the correction-rate signal feeds context selection
ONLY; it must never be wired into any agent-output scoring/reward surface.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_correction_failure_axis import EnumCorrectionFailureAxis
from omnibase_core.enums.enum_user_correction_category import EnumUserCorrectionCategory
from pydantic import ValidationError

from omnimarket.intelligence.aggregation import (
    context_selection_failure_count,
)
from omnimarket.intelligence.events import ModelUserCorrectionEvent

_VALID_HASH = "sha256:" + "a" * 64
_VALID_FACTOR_HASH = "sha256:" + "b" * 64

_OBSERVER_NODE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_user_correction_observer_effect"
)
# Resolve relative to the package source tree (tests live alongside src).
_OBSERVER_CONTRACT_PATH = _OBSERVER_NODE_DIR / "contract.yaml"
_OBSERVER_HANDLER_PATH = _OBSERVER_NODE_DIR / "handler_user_correction_observer.py"


def _make_event(**overrides: object) -> ModelUserCorrectionEvent:
    base: dict[str, object] = {
        "session_id": "sess-123",
        "correlation_id": uuid4(),
        "category": EnumUserCorrectionCategory.CLARIFICATION,
        "failure_axis": EnumCorrectionFailureAxis.MISUNDERSTANDING,
        "context_pack_hash": _VALID_HASH,
        "factor_subset_hash": _VALID_FACTOR_HASH,
        "emitted_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ModelUserCorrectionEvent(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_user_correction_event_categories_not_flattened() -> None:
    """category and failure_axis are distinct fields; no rolled-up score/weight."""
    event = _make_event()

    assert event.category is EnumUserCorrectionCategory.CLARIFICATION
    assert event.failure_axis is EnumCorrectionFailureAxis.MISUNDERSTANDING

    fields = set(ModelUserCorrectionEvent.model_fields)
    assert "category" in fields
    assert "failure_axis" in fields
    # No single collapsed numeric signal that flattens the two axes.
    assert "score" not in fields
    assert "weight" not in fields
    assert "correction_score" not in fields
    assert "correction_weight" not in fields


@pytest.mark.unit
def test_user_correction_event_requires_context_pack_hash() -> None:
    """Empty or absent context_pack_hash is rejected (orphan-signal guard)."""
    with pytest.raises(ValidationError):
        _make_event(context_pack_hash="")

    with pytest.raises(ValidationError):
        ModelUserCorrectionEvent(  # type: ignore[call-arg]
            session_id="sess-123",
            correlation_id=uuid4(),
            category=EnumUserCorrectionCategory.CLARIFICATION,
            failure_axis=EnumCorrectionFailureAxis.MISUNDERSTANDING,
            factor_subset_hash=_VALID_FACTOR_HASH,
            emitted_at=datetime.now(UTC),
        )

    # factor_subset_hash is equally mandatory.
    with pytest.raises(ValidationError):
        _make_event(factor_subset_hash="")


@pytest.mark.unit
def test_event_is_frozen() -> None:
    """The event is immutable once constructed."""
    event = _make_event()
    with pytest.raises(ValidationError):
        event.category = EnumUserCorrectionCategory.STYLE  # type: ignore[misc]


@pytest.mark.unit
def test_new_information_axis_excluded_from_context_failure_rate() -> None:
    """MISUNDERSTANDING counts toward the context-failure tally; NEW_INFORMATION does not."""
    misunderstanding = _make_event(
        failure_axis=EnumCorrectionFailureAxis.MISUNDERSTANDING
    )
    new_information = _make_event(
        failure_axis=EnumCorrectionFailureAxis.NEW_INFORMATION,
        category=EnumUserCorrectionCategory.SCOPE_EXPANSION,
    )

    # Per-event derived flag.
    assert misunderstanding.counts_toward_context_failure is True
    assert new_information.counts_toward_context_failure is False

    # Aggregation counts only the MISUNDERSTANDING-axis events.
    events = [misunderstanding, new_information, misunderstanding]
    assert context_selection_failure_count(events) == 2


@pytest.mark.unit
def test_correction_rate_not_wired_to_agent_output_reward() -> None:
    """The aggregation must not import any agent-output scoring/reward surface.

    Anti-sycophancy guard: the correction signal feeds context selection only.
    """
    aggregation_src = (
        _OBSERVER_NODE_DIR.parents[1] / "intelligence" / "aggregation.py"
    ).read_text()
    tree = ast.parse(aggregation_src)

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)

    forbidden_substrings = ("reward", "rlhf", "agent_output", "scoring", "sycophan")
    for module_name in imported_modules:
        lowered = module_name.lower()
        for token in forbidden_substrings:
            assert token not in lowered, (
                f"aggregation imports a forbidden surface: {module_name}"
            )


@pytest.mark.unit
def test_topic_declared_in_contract_not_hardcoded() -> None:
    """The publish topic resolves from contract.yaml; no hardcoded topic in handler."""
    contract = yaml.safe_load(_OBSERVER_CONTRACT_PATH.read_text())
    publish_topics = contract["event_bus"]["publish_topics"]
    assert publish_topics, "observer contract must declare a publish topic"
    correction_topics = [t for t in publish_topics if "correction" in t]
    assert correction_topics, (
        f"contract must declare a user-correction publish topic: {publish_topics}"
    )

    # The handler source must contain no literal onex.{evt,cmd}.* topic string.
    handler_src = _OBSERVER_HANDLER_PATH.read_text()
    tree = ast.parse(handler_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            assert not value.startswith("onex.evt."), (
                f"handler hardcodes a topic literal: {value!r}"
            )
            assert not value.startswith("onex.cmd."), (
                f"handler hardcodes a topic literal: {value!r}"
            )
