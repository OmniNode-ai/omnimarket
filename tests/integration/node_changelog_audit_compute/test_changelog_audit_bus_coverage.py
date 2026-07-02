# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_changelog_audit_compute, driven
over the canonical in-memory bus.

OMN-13674 (cluster wave-sweep-audit-compute). The COMPUTE handler
``HandlerChangelogAuditCompute`` is dispatched through ``LocalRuntimeBusAdapter``
over ``EventBusInmemory`` (via the ``integration_event_bus`` fixture): a
``ModelChangelogAuditRequest`` lands on the contract-declared command topic
``onex.cmd.omnimarket.changelog-audit-start.v1`` and the runtime auto-emits the
``ModelChangelogAuditResult`` onto the contract-declared terminal topic
``onex.evt.omnimarket.changelog-audit-completed.v1``.

COMPUTE DoD:
  * every declared ``entry_type`` verdict class reached -- breaking / feature /
    fix / chore / unknown -- asserted on the terminal-event payload summary;
  * every mode/flag branch exercised: the ``since_date`` cutoff filter, the
    ``dependencies`` inclusion filter (set and ``None``), and the
    missing-changelog-content skip;
  * a negative control: a changelog whose breaking entry pre-dates the
    ``since_date`` cutoff MUST be excluded, and the breaking entry after the
    cutoff MUST be classified ``breaking``.

The handler is pure (no filesystem or network I/O); the caller supplies all
changelog markdown inline, so nothing touches Kafka or disk.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_changelog_audit_compute.handlers.handler_changelog_audit_compute import (
    HandlerChangelogAuditCompute,
)
from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_request import (
    ModelChangelogAuditRequest,
)
from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_result import (
    ModelChangelogAuditResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_changelog_audit_compute/contract.yaml).
_START_TOPIC = "onex.cmd.omnimarket.changelog-audit-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.changelog-audit-completed.v1"

# A changelog exercising every entry_type classification track. Each block is
# under a dated version heading so entries carry a resolvable date.
_ALL_TYPES_CHANGELOG = """
## [2.0.0] - 2026-03-01
### Breaking Changes
- Remove the deprecated projection endpoint
### Added
- feat: add a new dashboard widget
### Fixed
- fix: correct the null projection pointer
### Changed
- chore: bump the ruff lint config
### Notes
- misc internal cleanup of the runtime docs
""".strip()


async def _run_over_bus(
    bus: Any, request: ModelChangelogAuditRequest
) -> ModelChangelogAuditResult:
    """Publish a changelog-audit request onto the declared command topic and
    return the terminal ``ModelChangelogAuditResult`` parsed off the declared
    terminal topic."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerChangelogAuditCompute(),
        handler_name="changelog-audit-compute",
        input_model_cls=ModelChangelogAuditRequest,
        output_topic=_COMPLETED_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-changelog-audit-test",
    )
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    history = await bus.get_event_history(topic=_COMPLETED_TOPIC)
    assert len(history) == 1, f"expected 1 terminal event on {_COMPLETED_TOPIC}"
    return ModelChangelogAuditResult.model_validate(json.loads(history[-1].value))


@pytest.mark.integration
async def test_changelog_audit_every_entry_type_over_bus(
    integration_event_bus: Any,
) -> None:
    """Every declared entry_type verdict class is reached and counted."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelChangelogAuditRequest(
                repos=["omnimarket"],
                since_date="2026-01-01",
                changelog_contents={"omnimarket": _ALL_TYPES_CHANGELOG},
            ),
        )
        assert result.summary == {
            "breaking": 1,
            "feature": 1,
            "fix": 1,
            "chore": 1,
            "unknown": 1,
        }
        reached = {entry.entry_type for entry in result.entries}
        assert reached == {"breaking", "feature", "fix", "chore", "unknown"}
    finally:
        await bus.close()


@pytest.mark.integration
async def test_changelog_audit_since_date_cutoff_over_bus(
    integration_event_bus: Any,
) -> None:
    """Negative control: an entry that pre-dates ``since_date`` is excluded while
    the post-cutoff breaking entry is classified ``breaking``."""
    changelog = """
## [2.0.0] - 2026-03-01
### Breaking Changes
- breaking: drop the legacy contract topic

## [1.0.0] - 2025-06-01
### Breaking Changes
- breaking: an old change that must not appear
""".strip()
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelChangelogAuditRequest(
                repos=["omnimarket"],
                since_date="2026-01-01",
                changelog_contents={"omnimarket": changelog},
            ),
        )
        assert len(result.entries) == 1
        only = result.entries[0]
        assert only.entry_type == "breaking"
        assert only.version == "2.0.0"
        assert "drop the legacy contract topic" in only.description
        # The pre-cutoff entry MUST NOT survive the filter.
        assert all("old change" not in e.description for e in result.entries)
        assert result.summary["breaking"] == 1
    finally:
        await bus.close()


@pytest.mark.integration
async def test_changelog_audit_dependency_filter_over_bus(
    integration_event_bus: Any,
) -> None:
    """The ``dependencies`` filter keeps only entries mentioning a named package."""
    changelog = """
## [3.0.0] - 2026-04-01
### Changed
- chore: bump pydantic to 2.9 for the model config contract
- chore: bump an unrelated internal helper
""".strip()
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelChangelogAuditRequest(
                repos=["omnimarket"],
                since_date="2026-01-01",
                dependencies=["pydantic"],
                changelog_contents={"omnimarket": changelog},
            ),
        )
        assert len(result.entries) == 1
        assert result.entries[0].affects_dependencies == ["pydantic"]
        assert "pydantic" in result.entries[0].description
    finally:
        await bus.close()


@pytest.mark.integration
async def test_changelog_audit_no_dependency_filter_keeps_all_over_bus(
    integration_event_bus: Any,
) -> None:
    """With ``dependencies=None`` (default) no dependency filtering is applied."""
    changelog = """
## [3.0.0] - 2026-04-01
### Changed
- chore: bump pydantic to 2.9
- chore: bump an unrelated internal helper
""".strip()
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelChangelogAuditRequest(
                repos=["omnimarket"],
                since_date="2026-01-01",
                changelog_contents={"omnimarket": changelog},
            ),
        )
        assert len(result.entries) == 2
        assert all(e.affects_dependencies == [] for e in result.entries)
    finally:
        await bus.close()


@pytest.mark.integration
async def test_changelog_audit_missing_content_skips_repo_over_bus(
    integration_event_bus: Any,
) -> None:
    """A repo with no supplied changelog content yields no entries and zero counts."""
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelChangelogAuditRequest(
                repos=["omnimarket", "omnibase_core"],
                since_date="2026-01-01",
                changelog_contents={"omnimarket": ""},
            ),
        )
        assert result.entries == []
        assert result.summary == {
            "breaking": 0,
            "feature": 0,
            "fix": 0,
            "chore": 0,
            "unknown": 0,
        }
    finally:
        await bus.close()


@pytest.mark.integration
async def test_changelog_audit_pure_handler_matches_bus_result(
    integration_event_bus: Any,
) -> None:
    """The in-process pure return equals the bus-transited terminal payload,
    proving the runtime adapter does not mutate the COMPUTE result."""
    request = ModelChangelogAuditRequest(
        repos=["omnimarket"],
        since_date="2026-01-01",
        changelog_contents={"omnimarket": _ALL_TYPES_CHANGELOG},
    )
    direct = HandlerChangelogAuditCompute().handle(request)
    assert direct.summary["breaking"] == 1

    bus = integration_event_bus
    await bus.start()
    try:
        transited = await _run_over_bus(bus, request)
        assert transited.model_dump() == direct.model_dump()
    finally:
        await bus.close()
