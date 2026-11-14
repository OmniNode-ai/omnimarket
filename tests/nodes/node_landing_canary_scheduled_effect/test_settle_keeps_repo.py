# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled landing canary settles through the repository object that opened its run (OMN-24353).

On the in-memory backend a settle through any other repository object reads a pull request that object
never opened, and its state read hits an assertion. These tests drive the runtime tick through the real
open and settle on that backend, and show the different-repository settle refused as the control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.landing_canary_scheduled_tick import (
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
NODE = "node_landing_canary_scheduled_effect"
CANARY_NODE = "node_landing_canary_effect"
CANARY_EXAMPLE = REPO / f"src/omnimarket/nodes/{CANARY_NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 14, 1, 40, tzinfo=UTC)


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def overlays(tmp_path: Path) -> dict[str, str]:
    """The schedule overlay and the canary's own example overlay (backend inmemory), as a runtime holds them."""
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
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
    (tmp_path / CANARY_NODE).mkdir()
    (tmp_path / CANARY_NODE / "overlay.yaml").write_text(
        CANARY_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
        "EXAMPLE_HOST_NAME": "fixture-host",
    }


def test_a_tick_that_settles_a_pending_run_returns_the_run_event(
    tmp_path: Path,
) -> None:
    """AC1: the due tick opens the run and settles it on the in-memory backend without an assertion."""
    handler = HandlerLandingCanaryScheduled(
        environ=overlays(tmp_path),
        settle_run=partial(settle_canary, appender=InMemoryLedger()),
    )
    event = handler.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name) == ("landing_canary", NODE)
    assert event.outcome == "failed"
    assert event.detail.startswith("the landing canary could not read the landing: ")
    assert "fixture" in event.detail


def test_the_settle_goes_through_the_repository_that_opened_the_run(
    tmp_path: Path,
) -> None:
    """AC2: open and settle share one repository object; a settle with no repo passed uses the run's."""
    env = overlays(tmp_path)
    repo = InMemoryCanaryRepo(clock=lambda: T0)
    run = open_canary(
        canary_overlay_id=CANARY_NODE, environ=env, clock=lambda: T0, repo=repo
    )
    assert run.repo is repo
    assert len(repo.opened_requests) == 1
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0 + timedelta(seconds=10),
        run=run,
        appender=InMemoryLedger(),
    )
    assert outcome is not None
    assert "fixture" in outcome.detail


def test_control_a_settle_with_a_different_repository_is_refused(
    tmp_path: Path,
) -> None:
    """AC2 control: a repository object that did not open the run holds no pull request, so its state read asserts."""
    env = overlays(tmp_path)
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0,
        repo=InMemoryCanaryRepo(clock=lambda: T0),
    )
    assert run.opened is not None
    with pytest.raises(AssertionError):
        settle_canary(
            canary_overlay_id=CANARY_NODE,
            environ=env,
            clock=lambda: T0 + timedelta(seconds=10),
            run=run,
            repo=InMemoryCanaryRepo(clock=lambda: T0),
            appender=InMemoryLedger(),
        )
