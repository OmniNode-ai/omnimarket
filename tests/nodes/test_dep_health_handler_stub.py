# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDepHealthSweep stub (TDD — written before implementation)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_handler_instantiates_without_event_bus() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
        HandlerDepHealthSweep,
    )

    handler = HandlerDepHealthSweep()
    assert isinstance(handler, HandlerDepHealthSweep)


@pytest.mark.unit
def test_handler_handle_returns_result() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
        HandlerDepHealthSweep,
    )
    from omnimarket.nodes.node_dependency_health_sweep.models import (
        ModelDepHealthSweepRequest,
        ModelDepHealthSweepResult,
    )

    handler = HandlerDepHealthSweep()
    result = handler.handle(ModelDepHealthSweepRequest(repo_roots=[], dry_run=True))
    assert isinstance(result, ModelDepHealthSweepResult)
    assert result.status in {"clean", "findings", "error"}


@pytest.mark.unit
def test_handler_handle_returns_clean_status_for_empty_roots() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
        HandlerDepHealthSweep,
    )
    from omnimarket.nodes.node_dependency_health_sweep.models import (
        ModelDepHealthSweepRequest,
    )

    handler = HandlerDepHealthSweep()
    result = handler.handle(ModelDepHealthSweepRequest(repo_roots=[], dry_run=True))
    assert result.status == "clean"
    assert result.findings == []
    assert result.run_id != ""
    assert result.graphify_version == "unknown"


@pytest.mark.unit
def test_handler_uses_supplied_run_id() -> None:
    from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
        HandlerDepHealthSweep,
    )
    from omnimarket.nodes.node_dependency_health_sweep.models import (
        ModelDepHealthSweepRequest,
    )

    handler = HandlerDepHealthSweep()
    result = handler.handle(
        ModelDepHealthSweepRequest(
            repo_roots=[],
            dry_run=True,
            run_id="my-stable-run-id",
        )
    )
    assert result.run_id == "my-stable-run-id"
