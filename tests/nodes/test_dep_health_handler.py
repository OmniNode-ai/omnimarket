# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration tests for HandlerDepHealthSweep (Task 8, OMN-11038).

Tests the handler with real engine components against temp fixtures.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
    HandlerDepHealthSweep,
)
from omnimarket.nodes.node_dependency_health_sweep.models import (
    ModelDepHealthSweepRequest,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# Clean fixture — no orphans, no missing edges
# ---------------------------------------------------------------------------


def test_handler_clean_fixture_returns_clean(tmp_path: Path) -> None:
    """Handler returns status='clean' for a fixture with no findings."""
    # Well-formed contract with matched pub/sub
    _write(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.test.completed.v1"
          subscribe_topics: []
        """,
    )
    _write(
        tmp_path / "node_b" / "contract.yaml",
        """
        name: node_b
        event_bus:
          publish_topics: []
          subscribe_topics:
            - "onex.evt.test.completed.v1"
        """,
    )
    # Two linked Python files (no orphans)
    _write(tmp_path / "src" / "a.py", "from src import b\n")
    _write(tmp_path / "src" / "b.py", "# module b\n")

    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
    )
    result = handler.handle(request)

    assert result.status == "clean"
    assert result.run_id != ""
    assert result.graphify_version != ""


# ---------------------------------------------------------------------------
# Run ID propagation
# ---------------------------------------------------------------------------


def test_handler_uses_supplied_run_id(tmp_path: Path) -> None:
    """Handler uses request.run_id when supplied."""
    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
        run_id="fixed-run-id-abc123",
    )
    result = handler.handle(request)

    assert result.run_id == "fixed-run-id-abc123"


def test_handler_generates_run_id_when_absent(tmp_path: Path) -> None:
    """Handler generates a UUID run_id when request.run_id is None."""
    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
    )
    result = handler.handle(request)

    assert result.run_id != ""
    assert len(result.run_id) > 10  # UUID is 36 chars


# ---------------------------------------------------------------------------
# Findings fixture — orphan cmd topic produces CRITICAL finding
# ---------------------------------------------------------------------------


def test_handler_orphan_cmd_topic_produces_findings(tmp_path: Path) -> None:
    """Handler returns status='findings' when an orphan cmd topic is detected."""
    # Contract with orphan command topic (published, no subscriber)
    _write(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.cmd.test.orphan-command.v1"
          subscribe_topics: []
        """,
    )

    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
    )
    result = handler.handle(request)

    assert result.status == "findings"
    assert len(result.findings) >= 1
    assert any(f.severity.value == "CRITICAL" for f in result.findings)


# ---------------------------------------------------------------------------
# Summary dict population
# ---------------------------------------------------------------------------


def test_handler_summary_counts_by_finding_type(tmp_path: Path) -> None:
    """result.summary contains per-finding-type counts."""
    _write(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.cmd.test.orphan-cmd.v1"
          subscribe_topics: []
        """,
    )

    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
    )
    result = handler.handle(request)

    # Summary should have at least one entry
    assert isinstance(result.summary, dict)
    assert len(result.summary) > 0
    # All values should be positive integers
    for count in result.summary.values():
        assert isinstance(count, int)
        assert count >= 0


def test_handler_applies_severity_threshold_before_result(tmp_path: Path) -> None:
    """A CRITICAL threshold suppresses lower-severity findings in the result."""
    _write(
        tmp_path / "node_a" / "contract.yaml",
        """
        name: node_a
        event_bus:
          publish_topics:
            - "onex.evt.test.orphan-event.v1"
          subscribe_topics: []
        """,
    )

    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        severity_threshold="CRITICAL",
        dry_run=True,
    )
    result = handler.handle(request)

    assert result.status == "clean"
    assert result.findings == []
    assert result.summary == {}


# ---------------------------------------------------------------------------
# Baseline delta
# ---------------------------------------------------------------------------


def test_handler_baseline_delta_none_when_no_baseline(tmp_path: Path) -> None:
    """result.baseline_delta is None when no baseline_path is supplied."""
    handler = HandlerDepHealthSweep()
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
    )
    result = handler.handle(request)

    assert result.baseline_delta is None


# ---------------------------------------------------------------------------
# Event bus emission
# ---------------------------------------------------------------------------


def test_handler_emits_event_to_bus_if_injected(tmp_path: Path) -> None:
    """Handler emits ModelDepHealthSweepCompletedEvent when event_bus is injected."""
    import asyncio

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    bus = EventBusInmemory()
    asyncio.run(bus.start())
    handler = HandlerDepHealthSweep(event_bus=bus)  # type: ignore[arg-type]
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=False,
    )
    handler.handle(request)

    published = asyncio.run(bus.get_event_history())
    assert len(published) >= 1


def test_handler_dry_run_does_not_emit_event(tmp_path: Path) -> None:
    """dry_run=True must suppress event publication even when a bus is wired."""
    import asyncio

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    bus = EventBusInmemory()
    asyncio.run(bus.start())
    handler = HandlerDepHealthSweep(event_bus=bus)  # type: ignore[arg-type]
    request = ModelDepHealthSweepRequest(
        repo_roots=[str(tmp_path)],
        dry_run=True,
    )
    handler.handle(request)

    published = asyncio.run(bus.get_event_history())
    assert published == []
