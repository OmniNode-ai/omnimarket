# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A settle after an open reads the repository the open used, not a fresh one (OMN-24117)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.landing_canary_scheduled_tick import (
    ModelCanaryPendingRun,
    ModelLandingCanaryTickOutcome,
    open_canary,
    settle_canary,
)
from omnimarket.nodes.node_landing_canary_effect.adapters.canary_repo import (
    InMemoryCanaryRepo,
)
from omnimarket.nodes.node_landing_canary_scheduled_effect.handlers.handler_landing_canary_scheduled import (
    HandlerLandingCanaryScheduled,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CANARY_NODE = "node_landing_canary_effect"
CANARY_EXAMPLE = REPO / f"src/omnimarket/nodes/{CANARY_NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
SCHEDULED_NODE = "node_landing_canary_scheduled_effect"
FIXTURE_DETAIL = "the landing canary could not read the landing: "


def canary_environ(tmp_path: Path) -> dict[str, str]:
    """The landing canary's own overlay, from the checked-in example."""
    (tmp_path / CANARY_NODE).mkdir()
    (tmp_path / CANARY_NODE / "overlay.yaml").write_text(
        CANARY_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_golden_settle_after_open_on_the_in_memory_backend_does_not_assert(
    tmp_path: Path,
) -> None:
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    overlay = yaml.safe_load(CANARY_EXAMPLE.read_text(encoding="utf-8"))
    assert overlay["target"]["backend"] == "inmemory"
    now = [T0]
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
    )
    assert run.opened is not None
    assert run.repo is not None
    now[0] = T0 + timedelta(seconds=10)
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
        run=run,
        appender=InMemoryLedger(),
    )
    assert outcome is not None
    assert outcome.failed is True
    assert outcome.detail.startswith(FIXTURE_DETAIL)


def test_golden_a_second_settle_reads_the_same_repository(tmp_path: Path) -> None:
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    now = [T0]
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
    )
    assert run.opened is not None
    assert run.repo is not None
    now[0] = T0 + timedelta(seconds=10)
    first = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
        run=run,
        appender=InMemoryLedger(),
    )
    second = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
        run=run,
        appender=InMemoryLedger(),
    )
    assert first is not None
    assert second is not None
    assert first.detail.startswith(FIXTURE_DETAIL)
    assert second.detail.startswith(FIXTURE_DETAIL)


def test_control_a_settle_through_a_fresh_repository_asserts(tmp_path: Path) -> None:
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    now = [T0]
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
    )
    assert run.opened is not None
    now[0] = T0 + timedelta(seconds=10)
    fresh = InMemoryCanaryRepo(clock=lambda: now[0])
    with pytest.raises(AssertionError):
        settle_canary(
            canary_overlay_id=CANARY_NODE,
            environ=env,
            clock=lambda: now[0],
            run=run,
            repo=fresh,
            appender=InMemoryLedger(),
        )


def test_error_a_run_whose_open_failed_still_settles_without_a_repository_object(
    tmp_path: Path,
) -> None:
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    run = ModelCanaryPendingRun(None, "clone-busy", T0)
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0,
        run=run,
        appender=InMemoryLedger(),
    )
    assert isinstance(outcome, ModelLandingCanaryTickOutcome)
    assert outcome.failed is True


def test_error_the_handler_ends_an_unbound_source_as_a_failed_event_not_an_assertion(
    tmp_path: Path,
) -> None:
    """The handler as the runtime wires it (no repository injected): a due tick opens and settles."""
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    (tmp_path / SCHEDULED_NODE).mkdir()
    (tmp_path / SCHEDULED_NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "landing_canary",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "canary_overlay_id": CANARY_NODE,
            }
        ),
        encoding="utf-8",
    )
    handler = HandlerLandingCanaryScheduled(environ=env)
    event = handler.handle(
        ModelRuntimeTick(
            now=T0,
            tick_id=uuid4(),
            sequence_number=1,
            scheduled_at=T0,
            correlation_id=uuid4(),
            scheduler_id="runtime-fixture",
            tick_interval_ms=1000,
        )
    )
    assert event is not None
    # No ledger host is named in this environment, so the settle ends as a typed failed run event
    # (never an AssertionError out of the handler, which the runtime logged once per tick).
    assert event.outcome == "failed"
    assert event.detail == "the landing canary source is not bound in the runtime"


def test_control_a_run_with_no_repository_object_builds_a_fresh_one_and_asserts(
    tmp_path: Path,
) -> None:
    """The old behaviour: settle with no repository kept builds an unopened in-memory one."""
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    run = open_canary(canary_overlay_id=CANARY_NODE, environ=env, clock=lambda: T0)
    with pytest.raises(AssertionError):
        settle_canary(
            canary_overlay_id=CANARY_NODE,
            environ=env,
            clock=lambda: T0 + timedelta(seconds=10),
            run=run._replace(repo=None),
            appender=InMemoryLedger(),
        )
