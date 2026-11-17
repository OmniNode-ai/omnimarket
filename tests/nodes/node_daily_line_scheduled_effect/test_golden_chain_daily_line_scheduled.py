# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the daily line on the runtime schedule (OMN-25163)."""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.schedule_run_store import InMemoryScheduleRunStore
from omnimarket.models.model_daily_line import (
    DailyLinePartName,
    ModelDailyLinePart,
    ModelDailyLinePublication,
)
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    HandlerDailyLineRun,
    load_runtime,
)
from omnimarket.nodes.node_daily_line_scheduled_effect import (
    NodeDailyLineScheduledEffect,
)
from omnimarket.nodes.node_daily_line_scheduled_effect.handlers.handler_daily_line_scheduled import (
    HandlerDailyLineScheduled,
)
from omnimarket.nodes.node_daily_line_scheduled_effect.models import (
    ModelDailyLineScheduleOverlay,
)
from omnimarket.nodes.node_schedule_run_reducer.handlers.handler_schedule_run_reducer import (
    HandlerScheduleRunReducer,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
NODE = "node_daily_line_scheduled_effect"


def tick(at: datetime, scheduled_at: datetime | None = None) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at if scheduled_at is None else scheduled_at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def environ(tmp_path: Path) -> dict[str, str]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "daily_line",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


class _Runner:
    def __init__(
        self,
        line: str = "Daily line published.",
        unknown_parts: tuple[DailyLinePartName, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.calls = 0
        self._error = error
        self._publication = ModelDailyLinePublication(
            generated_at=T0,
            line=line,
            parts=tuple(
                ModelDailyLinePart(
                    name=name,
                    text="UNKNOWN" if name in unknown_parts else "read",
                    read=name not in unknown_parts,
                )
                for name in cast(
                    tuple[DailyLinePartName, ...], ("piece1", "j1", "claims")
                )
            ),
            unknown_parts=unknown_parts,
        )

    def run(self) -> ModelDailyLinePublication:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._publication


def test_golden_a_due_tick_runs_and_returns_a_completed_event(tmp_path: Path) -> None:
    runner = _Runner("Daily line published.\nmore")
    builds = 0

    def build() -> HandlerDailyLineRun:
        nonlocal builds
        builds += 1
        return cast(HandlerDailyLineRun, runner)

    # Three distinct instants: the slot the scheduler aimed at, the tick's now and the handler's clock.
    slot, now, fired = T0 - timedelta(seconds=2), T0, T0 + timedelta(seconds=5)
    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path), build_runner=build, clock=lambda: fired
    )
    request = tick(now, scheduled_at=slot)
    event = h.handle(request)
    assert event is not None
    assert (event.job_id, event.node_name) == ("daily_line", NODE)
    assert (event.outcome, event.detail) == ("completed", "Daily line published.")
    assert event.tick_id == str(request.tick_id)
    assert event.scheduled_for == now
    assert event.fired_at == fired
    assert len({slot, now, fired}) == 3
    assert builds == runner.calls == 1


def test_golden_the_gate_runs_once_per_interval(tmp_path: Path) -> None:
    runner = _Runner()
    builds = 0

    def build() -> HandlerDailyLineRun:
        nonlocal builds
        builds += 1
        return cast(HandlerDailyLineRun, runner)

    gate = ScheduleGate(
        NODE,
        declaration=ModelDailyLineScheduleOverlay(
            job_id="daily_line", run_interval_seconds=300, grace_seconds=60
        ),
    )
    h = HandlerDailyLineScheduled(gate=gate, build_runner=build)
    assert h.handle(tick(T0)) is not None
    assert h.handle(tick(T0 + timedelta(seconds=299))) is None
    assert builds == runner.calls == 1
    assert h.handle(tick(T0 + timedelta(seconds=300))) is not None
    assert builds == runner.calls == 2


def test_error_no_overlay_refuses_once_and_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def build() -> HandlerDailyLineRun:
        pytest.fail("a missing overlay must not build a runner")

    h = HandlerDailyLineScheduled(environ={}, build_runner=build)
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert NODE in errors[0].message


def test_error_a_build_key_error_is_a_failed_run_naming_omni_home(
    tmp_path: Path,
) -> None:
    def build() -> HandlerDailyLineRun:
        raise KeyError("OMNI_HOME")

    h = HandlerDailyLineScheduled(environ=environ(tmp_path), build_runner=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", "KeyError: 'OMNI_HOME'")
    assert h.handle(tick(T0 + timedelta(seconds=1))) is None


@pytest.mark.parametrize("error_type", [KeyError, RuntimeError, OSError, ValueError])
@pytest.mark.parametrize("during_build", [True, False])
def test_error_build_or_run_failures_are_named_and_truncated(
    tmp_path: Path, error_type: type[Exception], during_build: bool
) -> None:
    message = "the ledger append failed: " + "x" * 400
    error = error_type(message)
    runner = _Runner(error=error)

    def build() -> HandlerDailyLineRun:
        if during_build:
            raise error
        return cast(HandlerDailyLineRun, runner)

    h = HandlerDailyLineScheduled(environ=environ(tmp_path), build_runner=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == f"{error_type.__name__}: {error}"[:300]
    assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert runner.calls == (0 if during_build else 1)


def test_golden_unknown_parts_appear_after_the_truncated_first_line(
    tmp_path: Path,
) -> None:
    runner = _Runner("x" * 400 + "\nmore", unknown_parts=("piece1", "claims"))
    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, runner),
    )
    event = h.handle(tick(T0))
    assert event is not None
    suffix = " (unknown: piece1,claims)"
    assert event.outcome == "completed"
    assert len(event.detail) == 300
    assert event.detail == "x" * (300 - len(suffix)) + suffix


def test_seam_the_run_event_folds_into_the_reducer_and_the_topics_match(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    event = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, runner),
    ).handle(tick(T0))
    assert event is not None
    store = InMemoryScheduleRunStore()
    assert HandlerScheduleRunReducer(store).handle(event).applied is True
    held = store.get("daily_line")
    assert held is not None
    assert held.last_outcome == "completed"
    assert (held.fire_count, held.failure_count) == (1, 0)
    producer = yaml.safe_load(
        (REPO / f"src/omnimarket/nodes/{NODE}/contract.yaml").read_text("utf-8")
    )
    reducer = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_schedule_run_reducer/contract.yaml"
        ).read_text("utf-8")
    )
    assert producer["terminal_event"] == reducer["input_subscriptions"][0]["topic"]


# --- review of head f71be4527990: builder, broad failure, empty line, overlay shape ------------------------------

FAKE_BUS = """
import json, sys
mode = sys.argv[1]
if mode == "read":
    request = json.loads(sys.stdin.read())
    print(json.dumps({"correlation_id": request["correlation_id"], "status": "ok",
                      "query": request["query"], "matched": 1, "lines": []}))
else:
    rows = sys.stdin.read()
    with open(sys.argv[2], "a", encoding="utf-8") as sink:
        sink.write(rows)
    print(json.dumps({"status": "accepted", "message": "ok"}))
"""


def runtime_environ(tmp_path: Path, *, with_home: bool) -> tuple[dict[str, str], Path]:
    """The overlay roots plus the deployment variables the default builder reads, over a stand-in bus."""
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    env = environ(overlays)
    script = tmp_path / "fake_bus.py"
    script.write_text(FAKE_BUS, encoding="utf-8")
    sink = tmp_path / "appended.txt"
    runtime = load_runtime()
    env[runtime.ledger_read_command_env] = f"{sys.executable} {script} read"
    env[runtime.ledger_append_command_env] = f"{sys.executable} {script} append {sink}"
    env[runtime.host_env] = "lab-host"
    if with_home:
        env[runtime.home_env] = str(tmp_path / "registry")
    return env, sink


def test_default_builder_with_omni_home_runs_the_real_runner(tmp_path: Path) -> None:
    env, sink = runtime_environ(tmp_path, with_home=True)
    event = HandlerDailyLineScheduled(environ=env).handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.job_id) == ("completed", "daily_line")
    published = tmp_path / "registry" / load_runtime().published_relpath
    assert published.is_file()
    assert " | STATUS | lane=node_daily_line_effect | " in sink.read_text("utf-8")


def test_default_builder_without_omni_home_is_a_failed_event_naming_it(
    tmp_path: Path,
) -> None:
    env, _ = runtime_environ(tmp_path, with_home=False)
    h = HandlerDailyLineScheduled(environ=env)
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == ("failed", "KeyError: 'OMNI_HOME'")


def test_default_builder_of_the_no_arg_node_reads_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, sink = runtime_environ(tmp_path, with_home=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    event = NodeDailyLineScheduledEffect().handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert sink.read_text("utf-8")


def test_default_builder_failure_is_failed_not_an_uncaught_type_error(
    tmp_path: Path,
) -> None:
    def build() -> HandlerDailyLineRun:
        raise TypeError("build_runner() takes 1 positional argument but 2 were given")

    h = HandlerDailyLineScheduled(environ=environ(tmp_path), build_runner=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail.startswith("TypeError: build_runner()")


class _OddError(Exception):
    """An exception class the handler does not name anywhere."""


@pytest.mark.parametrize(
    "error", [TypeError("t"), AttributeError("a"), _OddError("odd"), LookupError("l")]
)
@pytest.mark.parametrize("during_build", [True, False])
def test_any_exception_from_build_or_run_is_a_failed_event(
    tmp_path: Path, error: Exception, during_build: bool
) -> None:
    runner = _Runner(error=error)

    def build() -> HandlerDailyLineRun:
        if during_build:
            raise error
        return cast(HandlerDailyLineRun, runner)

    h = HandlerDailyLineScheduled(environ=environ(tmp_path), build_runner=build)
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert event.detail == f"{type(error).__name__}: {error}"
    # The interval was spent: the next tick inside it does not run again.
    assert h.handle(tick(T0 + timedelta(seconds=1))) is None


def test_failed_event_when_the_detail_cannot_be_built(tmp_path: Path) -> None:
    class _BadPublication:
        unknown_parts = ("piece1",)

        @property
        def line(self) -> str:
            raise ValueError("the line cannot be read")

    class _BadRunner:
        def run(self) -> object:
            return _BadPublication()

    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, _BadRunner()),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert (event.outcome, event.detail) == (
        "failed",
        "ValueError: the line cannot be read",
    )


@pytest.mark.parametrize("line", ["\n", "\n\nsecond"])
def test_empty_first_line_is_a_failed_event(tmp_path: Path, line: str) -> None:
    runner = _Runner(line)
    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, runner),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "empty" in event.detail


def test_empty_line_that_the_model_never_lets_through_is_a_failed_event_not_index_error(
    tmp_path: Path,
) -> None:
    # ModelDailyLinePublication.line has min_length=1, so a validated publication never holds ""; a runner stand-in or a
    # model_construct one can, and splitlines()[0] on "" raised IndexError outside the try, after the gate was marked.
    publication = ModelDailyLinePublication.model_construct(
        generated_at=T0, line="", parts=(), unknown_parts=()
    )

    class _EmptyRunner:
        def run(self) -> ModelDailyLinePublication:
            return publication

    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, _EmptyRunner()),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "empty" in event.detail


def test_empty_line_is_refused_by_the_publication_model() -> None:
    with pytest.raises(ValueError, match="at least 1 character"):
        ModelDailyLinePublication(generated_at=T0, line="", parts=(), unknown_parts=())


def test_completed_detail_never_exceeds_300_characters(tmp_path: Path) -> None:
    runner = _Runner(
        "y" * 500,
        unknown_parts=cast(tuple[DailyLinePartName, ...], ("piece1", "j1", "claims")),
    )
    h = HandlerDailyLineScheduled(
        environ=environ(tmp_path),
        build_runner=lambda: cast(HandlerDailyLineRun, runner),
    )
    event = h.handle(tick(T0))
    assert event is not None
    assert len(event.detail) == 300
    assert event.detail.endswith(" (unknown: piece1,j1,claims)")


def test_overlay_that_is_not_the_daily_line_overlay_is_refused_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = ScheduleGate(
        NODE,
        declaration=ModelScheduleDeclaration(
            job_id="daily_line", run_interval_seconds=300, grace_seconds=60
        ),
    )

    def build() -> HandlerDailyLineRun:
        pytest.fail("a foreign overlay must not build a runner")

    h = HandlerDailyLineScheduled(gate=gate, build_runner=build)
    with caplog.at_level(logging.ERROR):
        assert h.handle(tick(T0)) is None
        assert h.handle(tick(T0 + timedelta(seconds=1))) is None
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


def test_overlay_grace_seconds_is_required_and_not_negative() -> None:
    ok = ModelDailyLineScheduleOverlay(
        job_id="daily_line", run_interval_seconds=300, grace_seconds=0
    )
    assert ok.grace_seconds == 0
    with pytest.raises(ValueError, match="grace_seconds"):
        ModelDailyLineScheduleOverlay(
            job_id="daily_line", run_interval_seconds=300, grace_seconds=-1
        )
    with pytest.raises(ValueError, match="grace_seconds"):
        ModelDailyLineScheduleOverlay.model_validate(
            {"job_id": "daily_line", "run_interval_seconds": 300}
        )


def test_empty_environ_does_not_fall_through_to_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """environ={} is an environment with no overlay roots, not 'use os.environ' (ScheduleGate checks `is None`)."""
    roots = environ(tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", roots["ONEX_SKILL_OVERLAY_ROOTS"])
    assert os.environ["ONEX_SKILL_OVERLAY_ROOTS"]
    h = HandlerDailyLineScheduled(
        environ={},
        build_runner=lambda: pytest.fail("an empty environment has no overlay"),  # type: ignore[arg-type,return-value]
    )
    assert h.handle(tick(T0)) is None
