# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the daily landing canary on the runtime schedule (OMN-24117)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
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
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.nodes.node_landing_canary_effect.adapters.canary_repo import (
    InMemoryCanaryRepo,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelCanaryOpened,
)
from omnimarket.nodes.node_landing_canary_scheduled_effect.handlers.handler_landing_canary_scheduled import (
    HandlerLandingCanaryScheduled,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_landing_canary_scheduled_effect"
CANARY_NODE = "node_landing_canary_effect"
CANARY_EXAMPLE = REPO / f"src/omnimarket/nodes/{CANARY_NODE}/overlay.example.yaml"
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DONE = ModelLandingCanaryTickOutcome(
    False, "verdict=working ending=merged elapsed_s=10 bound_s=3600"
)


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


def environ(tmp_path: Path) -> dict[str, str]:
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
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def canary_environ(tmp_path: Path) -> dict[str, str]:
    """The landing canary's own overlay, from the checked-in example."""
    (tmp_path / CANARY_NODE).mkdir()
    (tmp_path / CANARY_NODE / "overlay.yaml").write_text(
        CANARY_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _RecordingOpen:
    """The injected open: returns a pending run and records the overlay id it was given."""

    def __init__(self, run: ModelCanaryPendingRun) -> None:
        self._run = run
        self.calls: list[str] = []

    def __call__(
        self,
        *,
        canary_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
    ) -> ModelCanaryPendingRun:
        self.calls.append(canary_overlay_id)
        return self._run


class _RecordingSettle:
    """The injected settle: returns the fixed outcome and records the overlay id it was given."""

    def __init__(
        self,
        outcome: ModelLandingCanaryTickOutcome
        | None
        | list[ModelLandingCanaryTickOutcome | None],
    ) -> None:
        self._outcomes = outcome if isinstance(outcome, list) else [outcome]
        self.calls: list[str] = []

    def __call__(
        self,
        *,
        canary_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
        run: ModelCanaryPendingRun,
    ) -> ModelLandingCanaryTickOutcome | None:
        self.calls.append(canary_overlay_id)
        if len(self._outcomes) > 1:
            return self._outcomes.pop(0)
        return self._outcomes[0]


def _raising(error: Exception) -> Any:
    def open(
        *,
        canary_overlay_id: str,
        environ: Mapping[str, str] | None,
        clock: Callable[[], datetime],
    ) -> ModelCanaryPendingRun:
        raise error

    return open


def _pending() -> ModelCanaryPendingRun:
    return ModelCanaryPendingRun(
        ModelCanaryOpened(number=1, head_sha="a" * 40, opened_at=T0), "", T0
    )


def test_golden_a_due_tick_opens_and_settles_to_none(tmp_path: Path) -> None:
    opened = _RecordingOpen(_pending())
    settled = _RecordingSettle(None)
    h = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path), open_run=opened, settle_run=settled
    )
    assert h.handle(tick(T0)) is None
    assert opened.calls == [CANARY_NODE]
    assert settled.calls == [CANARY_NODE]


def test_golden_a_later_tick_settles_and_returns_its_detail(tmp_path: Path) -> None:
    opened = _RecordingOpen(_pending())
    settled = _RecordingSettle([None, DONE])
    h = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path), open_run=opened, settle_run=settled
    )
    assert h.handle(tick(T0)) is None
    event = h.handle(tick(T0 + timedelta(seconds=10)))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "landing_canary",
        NODE,
        "completed",
    )
    assert event.detail == DONE.detail
    assert h.handle(tick(T0 + timedelta(seconds=11))) is None
    assert opened.calls == [CANARY_NODE]
    assert settled.calls == [CANARY_NODE, CANARY_NODE]


def test_golden_a_tick_inside_the_interval_returns_none(tmp_path: Path) -> None:
    h = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path),
        open_run=_RecordingOpen(_pending()),
        settle_run=_RecordingSettle(None),
    )
    assert h.handle(tick(T0)) is None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None


def test_error_an_unreadable_canary_overlay_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path),
        open_run=_raising(NodeOverlayError("boom: the overlay is invalid")),
        settle_run=_RecordingSettle(None),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the landing canary overlay could not be read",
    )


def test_error_a_source_not_bound_is_a_failed_run(tmp_path: Path) -> None:
    h = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path),
        open_run=_raising(ValueError("the host env is not set")),
        settle_run=_RecordingSettle(None),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "the landing canary source is not bound in the runtime",
    )


def test_error_a_failed_settle_outcome_carries_its_detail(tmp_path: Path) -> None:
    failed = ModelLandingCanaryTickOutcome(
        True, "the landing canary run failed: open-failed:clone-busy"
    )
    h = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path),
        open_run=_RecordingOpen(_pending()),
        settle_run=_RecordingSettle(failed),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == failed.detail


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = HandlerLandingCanaryScheduled(
        environ={},
        open_run=_RecordingOpen(_pending()),
        settle_run=_RecordingSettle(None),
    )
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    event = HandlerLandingCanaryScheduled(
        environ=environ(tmp_path),
        open_run=_RecordingOpen(_pending()),
        settle_run=_RecordingSettle(DONE),
    ).handle(tick(T0 + timedelta(seconds=301)))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("landing_canary")
    assert held is not None
    assert held.last_outcome == "completed"
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


def _settled(
    tmp_path: Path, *, landing_tick_enabled: bool, settle_after_seconds: int
) -> ModelLandingCanaryTickOutcome | None:
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    now = [T0]
    repo = InMemoryCanaryRepo(
        clock=lambda: now[0],
        land_after_seconds=0,
        landing_tick_enabled=landing_tick_enabled,
    )
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
        repo=repo,
    )
    assert run.opened is not None
    now[0] = T0 + timedelta(seconds=settle_after_seconds)
    return settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: now[0],
        run=run,
        repo=repo,
        appender=InMemoryLedger(),
    )


def test_real_chain_a_landed_fixture_reads_failed_by_name(tmp_path: Path) -> None:
    """The in-memory backend is a fixture: a merge inside the bound is not a landing reading."""
    outcome = _settled(tmp_path, landing_tick_enabled=True, settle_after_seconds=10)
    assert outcome is not None
    assert outcome.failed is True
    assert outcome.detail.startswith("the landing canary could not read the landing: ")
    assert "fixture" in outcome.detail


def test_real_chain_a_stalled_landing_is_a_completed_run_that_reads_not_working(
    tmp_path: Path,
) -> None:
    """Failing control: the same chain with the landing tick off and the bound run out names the stall."""
    outcome = _settled(tmp_path, landing_tick_enabled=False, settle_after_seconds=7200)
    assert outcome == ModelLandingCanaryTickOutcome(
        False, "verdict=NOT_WORKING ending=stalled elapsed_s=7200 bound_s=3600"
    )


def test_real_chain_a_pull_request_not_merged_before_the_bound_settles_to_none(
    tmp_path: Path,
) -> None:
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    repo = InMemoryCanaryRepo(
        clock=lambda: T0, land_after_seconds=7200, landing_tick_enabled=True
    )
    run = open_canary(
        canary_overlay_id=CANARY_NODE, environ=env, clock=lambda: T0, repo=repo
    )
    assert run.opened is not None
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0 + timedelta(seconds=10),
        run=run,
        repo=repo,
        appender=InMemoryLedger(),
    )
    assert outcome is None


def test_real_chain_settle_resumes_the_opening_repo(tmp_path: Path) -> None:
    """A settle with no repo reuses the repository the open built."""
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: datetime.now(UTC),
    )
    assert run.opened is not None
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: datetime.now(UTC),
        run=run,
        appender=InMemoryLedger(),
    )
    assert outcome is not None
    assert outcome.failed is True
    assert "fixture" in outcome.detail


def test_real_chain_settle_state_error_does_not_raise(tmp_path: Path) -> None:
    """A state error on the opening repo is swallowed, not raised."""
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    repo = InMemoryCanaryRepo(clock=lambda: T0)
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0,
        repo=repo,
    )
    assert run.repo is repo
    repo.state_error = "state-unreadable"
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0 + timedelta(seconds=7200),
        run=run,
        appender=InMemoryLedger(),
    )
    assert outcome is not None
    assert isinstance(outcome, ModelLandingCanaryTickOutcome)


def test_real_chain_open_carries_the_repo_it_used(tmp_path: Path) -> None:
    """The pending run carries the repository it used to open."""
    env = canary_environ(tmp_path)
    env["EXAMPLE_HOST_NAME"] = "fixture-host"
    repo = InMemoryCanaryRepo(clock=lambda: T0)
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0,
        repo=repo,
    )
    assert run.repo is repo
    bare = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: T0,
    )
    assert bare.repo is not None


# OMN-24353: the settle goes through the repository object that opened the run.
SETTLE_T0 = datetime(2026, 11, 14, 1, 40, tzinfo=UTC)


def settle_overlays(tmp_path: Path) -> dict[str, str]:
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
        environ=settle_overlays(tmp_path),
        settle_run=partial(settle_canary, appender=InMemoryLedger()),
    )
    event = handler.handle(tick(SETTLE_T0))
    assert event is not None
    assert (event.job_id, event.node_name) == ("landing_canary", NODE)
    assert event.outcome == "failed"
    assert event.detail.startswith("the landing canary could not read the landing: ")
    assert "fixture" in event.detail


def test_the_settle_goes_through_the_repository_that_opened_the_run(
    tmp_path: Path,
) -> None:
    """AC2: open and settle share one repository object; a settle with no repo passed uses the run's."""
    env = settle_overlays(tmp_path)
    repo = InMemoryCanaryRepo(clock=lambda: SETTLE_T0)
    run = open_canary(
        canary_overlay_id=CANARY_NODE, environ=env, clock=lambda: SETTLE_T0, repo=repo
    )
    assert run.repo is repo
    assert len(repo.opened_requests) == 1
    outcome = settle_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: SETTLE_T0 + timedelta(seconds=10),
        run=run,
        appender=InMemoryLedger(),
    )
    assert outcome is not None
    assert "fixture" in outcome.detail


def test_control_a_settle_with_a_different_repository_is_refused(
    tmp_path: Path,
) -> None:
    """AC2 control: a repository object that did not open the run holds no pull request, so its state read asserts."""
    env = settle_overlays(tmp_path)
    run = open_canary(
        canary_overlay_id=CANARY_NODE,
        environ=env,
        clock=lambda: SETTLE_T0,
        repo=InMemoryCanaryRepo(clock=lambda: SETTLE_T0),
    )
    assert run.opened is not None
    with pytest.raises(AssertionError):
        settle_canary(
            canary_overlay_id=CANARY_NODE,
            environ=env,
            clock=lambda: SETTLE_T0 + timedelta(seconds=10),
            run=run,
            repo=InMemoryCanaryRepo(clock=lambda: SETTLE_T0),
            appender=InMemoryLedger(),
        )
