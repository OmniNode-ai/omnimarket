# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error events of node_dev_running_revision_effect through its handler and an in-memory source."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_running_revision_compute.models import (
    EnumRunningRevisionRefusalReason,
    EnumRunningRevisionStatus,
    ModelRunningRevisionPolicy,
)
from omnimarket.nodes.node_dev_running_revision_effect.adapters import (
    InMemoryRunningRevisionSource,
)
from omnimarket.nodes.node_dev_running_revision_effect.handlers import (
    HandlerDevRunningRevisionRead,
)
from omnimarket.nodes.node_dev_running_revision_effect.models import (
    EnumDevRunningRevisionFailureReason,
    ModelDevRunningRevisionReadCommand,
    ModelDevRunningRevisionReadCompleted,
    ModelDevRunningRevisionReadFailed,
)
from omnimarket.nodes.node_dev_running_revision_effect.protocols.protocol_running_revision_source import (
    ProtocolRunningRevisionSource,
    RunningRevisionSourceError,
)
from omnimarket.nodes.node_dev_running_revision_effect.runtime_dev_running_revision import (
    main,
)
from tests.unit.nodes.node_dev_running_revision_compute.conftest import (
    NOW,
    build,
    facts,
    history,
    merge,
    sha,
)

pytestmark = pytest.mark.unit

INFRA = history("omnibase_infra", sha(100), ())


def _handler(
    source: ProtocolRunningRevisionSource, policy: ModelRunningRevisionPolicy
) -> HandlerDevRunningRevisionRead:
    return HandlerDevRunningRevisionRead(source, policy, lambda: NOW)


async def test_golden_a_behind_runtime_is_completed_with_the_computes_reading(
    policy: ModelRunningRevisionPolicy,
) -> None:
    runtime = build("b1", 60, {"omnimarket": 1, "omnibase_infra": 100})
    omnimarket = history(
        "omnimarket", sha(0), (merge(1, 6000), merge(2, 5400), merge(3, 4000))
    )
    source = InMemoryRunningRevisionSource(facts((runtime,), (omnimarket, INFRA)))
    correlation_id = uuid4()
    result = await _handler(source, policy).handle(
        ModelDevRunningRevisionReadCommand(correlation_id=correlation_id)
    )
    assert isinstance(result, ModelDevRunningRevisionReadCompleted)
    assert result.correlation_id == correlation_id
    assert result.observed_at == NOW
    assert result.reading.status is EnumRunningRevisionStatus.BEHIND
    assert result.reading.correlation_id == correlation_id
    assert result.reading.red is True


async def test_error_an_unreadable_source_is_failed_and_never_a_reading(
    policy: ModelRunningRevisionPolicy,
) -> None:
    """The failing control: a source that cannot read must not come out as a completed reading."""
    source = InMemoryRunningRevisionSource(
        error=RunningRevisionSourceError("provenance read exited 255")
    )
    result = await _handler(source, policy).handle(
        ModelDevRunningRevisionReadCommand(correlation_id=uuid4())
    )
    assert isinstance(result, ModelDevRunningRevisionReadFailed)
    assert result.reason is EnumDevRunningRevisionFailureReason.SOURCE_UNREADABLE
    assert result.detail == "provenance read exited 255"
    assert result.refused is None


async def test_error_contradicting_facts_are_failed_with_the_refusal(
    policy: ModelRunningRevisionPolicy,
) -> None:
    unknown = history("somewhere_else", sha(0), (merge(1, 3000),))
    source = InMemoryRunningRevisionSource(facts((), (unknown,)))
    result = await _handler(source, policy).handle(
        ModelDevRunningRevisionReadCommand(correlation_id=uuid4())
    )
    assert isinstance(result, ModelDevRunningRevisionReadFailed)
    assert result.reason is EnumDevRunningRevisionFailureReason.FACTS_REFUSED
    assert result.refused is not None
    assert result.refused.reason is EnumRunningRevisionRefusalReason.UNKNOWN_REPOSITORY


def test_the_in_memory_source_needs_exactly_one_of_facts_or_error() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        InMemoryRunningRevisionSource()


def test_the_in_memory_source_satisfies_the_source_protocol() -> None:
    assert isinstance(
        InMemoryRunningRevisionSource(error=RunningRevisionSourceError("x")),
        ProtocolRunningRevisionSource,
    )


def test_the_clock_used_is_the_handlers_not_the_facts() -> None:
    assert datetime(2026, 11, 12, 16, 0, tzinfo=UTC) == NOW


def test_the_cli_exits_3_when_the_overlay_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert main([]) == 3
