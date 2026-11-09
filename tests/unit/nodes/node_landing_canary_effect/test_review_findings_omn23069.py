# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings of omnimarket#5323 on the landing canary (OMN-23069), one test group per finding."""

from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.rules_unit_roots import WORKTREE_SEGMENT
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_landing_canary_effect.adapters.canary_repo import (
    ScmCanaryRepo,
)
from omnimarket.nodes.node_landing_canary_effect.handlers.handler_landing_canary import (
    HandlerLandingCanary,
)
from omnimarket.nodes.node_landing_canary_effect.handlers.schedule import (
    LandingCanaryScheduler,
    load_schedule,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    EnumCanaryEnding,
    ModelCanaryOpened,
    ModelCanaryOpenRequest,
    ModelCanaryState,
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_landing_canary_effect.protocols import CanaryRepoError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.unit.nodes.node_landing_canary_effect.canary_support import (
    HOST,
    RUN_AT,
    FakeClock,
    World,
    operational_overlay,
    overlay,
)
from tests.unit.nodes.node_landing_canary_effect.test_canary_stall import (
    piece9_reading,
)
from tests.unit.nodes.node_landing_canary_effect.test_schedule_root import (
    OVERLAY_ROOT,
    REGISTRY_ROOT,
    WORKTREE_ROOT,
    RecordingRunner,
    venv_root,
)
from tests.unit.nodes.node_landing_canary_effect.test_scm_canary_repo import (
    REQUEST,
    Recorder,
    done,
    repo,
)

pytestmark = pytest.mark.unit

COMMAND = ModelLandingCanaryCommand(correlation_id="canary-23069", write=True)


# Finding 1: fixture-generated evidence is never an operational reading.


def test_f1_a_fixture_backed_run_is_never_tagged_operational() -> None:
    world, ledger = World(), InMemoryLedger()
    assert overlay().publish.drill is False
    assert overlay().target.backend == "inmemory"
    world.handler(ledger).handle(COMMAND)
    assert "tag=drill" in ledger.lines[0]
    assert "tag=operational" not in ledger.lines[0]
    assert piece9_reading(ledger, now=RUN_AT + timedelta(hours=2)) is (
        EnumReadingVerdict.NOT_WORKING
    )


def test_f1_a_real_backend_run_is_still_operational() -> None:
    world, ledger = World(), InMemoryLedger()
    world.handler(ledger, operational_overlay()).handle(COMMAND)
    assert "tag=operational" in ledger.lines[0]
    assert piece9_reading(ledger, now=RUN_AT + timedelta(hours=2)) is (
        EnumReadingVerdict.WORKING
    )


# Finding 2: the worktree-root guard covers the launchd agent.


def launchd_scheduler(
    tmp_path: Path, *, environment: dict[str, str] | None = None
) -> tuple[LandingCanaryScheduler, RecordingRunner]:
    base = overlay()
    host = base.hosts[0].model_copy(
        update={
            "scheduler": "launchd",
            "environment": environment or base.hosts[0].environment,
        }
    )
    runner = RecordingRunner()
    return (
        LandingCanaryScheduler(
            schedule=load_schedule(),
            overlay=base.model_copy(update={"hosts": (host,)}),
            home=tmp_path,
            run=runner,
        ),
        runner,
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_f2_a_launchd_install_from_a_worktree_is_refused(
    tmp_path: Path, dry_run: bool
) -> None:
    sched, runner = launchd_scheduler(tmp_path)
    result = sched.install(
        HOST, omni_root=WORKTREE_ROOT, overlay_root=OVERLAY_ROOT, dry_run=dry_run
    )
    assert result.exit_code == 1
    assert f"runs from {WORKTREE_SEGMENT}" in result.stderr
    assert not (tmp_path / "Library").exists()
    assert runner.commands == []


def test_f2_a_launchd_environment_value_in_a_worktree_is_refused(
    tmp_path: Path,
) -> None:
    sched, _ = launchd_scheduler(
        tmp_path,
        environment={"EXAMPLE_LEDGER_APPEND_COMMAND": f"{WORKTREE_ROOT}/bin/append"},
    )
    result = sched.install(
        HOST, omni_root=REGISTRY_ROOT, overlay_root=OVERLAY_ROOT, dry_run=True
    )
    assert result.exit_code == 1
    assert f"runs from {WORKTREE_SEGMENT}" in result.stderr


def test_f2_a_launchd_install_from_the_registry_root_still_renders(
    tmp_path: Path,
) -> None:
    sched, _ = launchd_scheduler(tmp_path)
    result = sched.install(
        HOST, omni_root=REGISTRY_ROOT, overlay_root=OVERLAY_ROOT, dry_run=True
    )
    assert result.exit_code == 0, result.stderr
    assert WORKTREE_SEGMENT not in result.stdout


# Finding 3: the landing time runs from the pull request's opening, not from the end of the create command.


class SlowOpenRepo:
    """A repository whose open takes a minute and whose pull request merges a set time after the open began."""

    def __init__(
        self,
        clock: FakeClock,
        opened_at: datetime | None = None,
        open_seconds: int = 60,
        merge_seconds: int = 3630,
    ) -> None:
        self.clock = clock
        self.opened_at = opened_at
        self.open_seconds = open_seconds
        self.merge_seconds = merge_seconds
        self.began = clock()

    def open(self, request: ModelCanaryOpenRequest) -> ModelCanaryOpened:
        self.began = self.clock()
        self.clock.now += timedelta(seconds=self.open_seconds)
        if self.opened_at is None:
            return ModelCanaryOpened(number=1, head_sha="a" * 40)
        return ModelCanaryOpened(number=1, head_sha="a" * 40, opened_at=self.opened_at)

    def state(self, number: int) -> ModelCanaryState:
        merge_at = self.began + timedelta(seconds=self.merge_seconds)
        if self.clock() >= merge_at:
            return ModelCanaryState(merged=True, merged_at=merge_at)
        return ModelCanaryState(merged=False)


def slow_handler(repo_: SlowOpenRepo, ledger: InMemoryLedger) -> HandlerLandingCanary:
    return HandlerLandingCanary(
        overlay=operational_overlay(),
        host=HOST,
        repo=repo_,
        appender=ledger,
        clock=repo_.clock,
        sleep=repo_.clock.sleep,
    )


def test_f3_a_slow_create_response_does_not_shorten_the_landing_time() -> None:
    # Merged 3,630 s after the create began: late against the 3,600 s bar, though 3,570 s after it returned.
    clock = FakeClock()
    ledger = InMemoryLedger()
    result = slow_handler(SlowOpenRepo(clock), ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.elapsed_seconds is not None
    assert result.elapsed_seconds >= 3600
    assert "verdict=WORKING" not in ledger.lines[0]


def test_f3_the_opening_instant_the_repository_reports_is_used() -> None:
    # Reported open at +10 s and a merge at +3,605 s: 3,595 s after the pull request opened, inside the bar.
    clock = FakeClock()
    reported = RUN_AT + timedelta(seconds=10)
    result = slow_handler(
        SlowOpenRepo(clock, opened_at=reported, merge_seconds=3605), InMemoryLedger()
    ).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.elapsed_seconds == 3595


def test_f3_the_scm_repository_reports_the_instant_before_the_create_command() -> None:
    clock = FakeClock()

    class AdvancingRecorder(Recorder):
        def __call__(
            self, argv: Sequence[str], cwd: Path | None, timeout: int
        ) -> subprocess.CompletedProcess[str]:
            answer = super().__call__(argv, cwd, timeout)
            if "pr-create" in " ".join(argv):
                clock.now += timedelta(seconds=60)
            return answer

    run = AdvancingRecorder(
        {
            "rev-parse": done(out="f" * 40 + "\n"),
            "pr-create": done(out="example-org/example-landing-canary#77\n"),
        }
    )
    scm = overlay().target.scm
    assert scm is not None
    with tempfile.TemporaryDirectory() as clone:
        repository = ScmCanaryRepo(
            scm,
            overlay().target.repo,
            {
                scm.clone_dir_env: clone,
                scm.pr_create_command_env: "onex scm pr-create",
                scm.pr_state_command_env: "x",
            },
            run,
            clock=clock,
        )
        opened = repository.open(REQUEST)
    assert opened.opened_at == RUN_AT


# Finding 4: the systemd rollback stops the service the timer already started.


def test_f4_uninstall_stops_the_service_as_well_as_the_timer(tmp_path: Path) -> None:
    runner = RecordingRunner()
    sched = LandingCanaryScheduler(
        schedule=load_schedule(), overlay=overlay(), home=tmp_path, run=runner
    )
    sched.install(HOST, omni_root=venv_root(tmp_path), overlay_root=OVERLAY_ROOT)
    runner.commands.clear()
    result = sched.uninstall(HOST)
    assert result.exit_code == 0
    assert [
        "systemctl",
        "--user",
        "stop",
        "onex-landing-canary-daily.service",
    ] in runner.commands


class FailingRunner(RecordingRunner):
    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        code = 1 if self.failing in command else 0
        return subprocess.CompletedProcess(list(command), code, "", "failed")


@pytest.mark.parametrize("failing", ["disable", "stop"])
def test_f4_a_failure_to_stop_the_schedule_is_reported_and_keeps_the_units(
    tmp_path: Path, failing: str
) -> None:
    sched = LandingCanaryScheduler(
        schedule=load_schedule(),
        overlay=overlay(),
        home=tmp_path,
        run=RecordingRunner(),
    )
    sched.install(HOST, omni_root=venv_root(tmp_path), overlay_root=OVERLAY_ROOT)
    failing_sched = LandingCanaryScheduler(
        schedule=load_schedule(),
        overlay=overlay(),
        home=tmp_path,
        run=FailingRunner(failing),
    )
    result = failing_sched.uninstall(HOST)
    assert result.exit_code != 0
    assert len(list((tmp_path / ".config/systemd/user").iterdir())) == 2


# Finding 5: a committed symlink cannot redirect the marker write outside the clone.


def test_f5_a_canary_directory_symlink_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    clone, outside = tmp_path / "clone", tmp_path / "outside"
    clone.mkdir()
    outside.mkdir()
    (clone / ".canary").symlink_to(outside, target_is_directory=True)
    run = Recorder({"rev-parse": done(out="f" * 40)})
    with pytest.raises(CanaryRepoError, match=r"symlink|outside"):
        repo(clone, run).open(REQUEST)
    assert list(outside.iterdir()) == []
    assert not any(" add " in f" {' '.join(argv)} " for argv, _ in run.commands)


def test_f5_a_marker_symlink_is_refused(tmp_path: Path) -> None:
    clone, outside = tmp_path / "clone", tmp_path / "outside"
    (clone / ".canary").mkdir(parents=True)
    outside.mkdir()
    (clone / ".canary" / f"{REQUEST.nonce}.txt").symlink_to(outside / "target.txt")
    run = Recorder({"rev-parse": done(out="f" * 40)})
    with pytest.raises(CanaryRepoError):
        repo(clone, run).open(REQUEST)
    assert list(outside.iterdir()) == []


# Finding 6: overlapping runs on one clone are serialized.


def test_f6_a_run_that_finds_the_clone_in_use_waits_then_fails_typed(
    tmp_path: Path,
) -> None:
    scm = overlay().target.scm
    assert scm is not None
    run = Recorder({"rev-parse": done(out="f" * 40)})
    held = os.open(tmp_path, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        repository = ScmCanaryRepo(
            scm,
            overlay().target.repo,
            {
                scm.clone_dir_env: str(tmp_path),
                scm.pr_create_command_env: "onex scm pr-create",
                scm.pr_state_command_env: "x",
            },
            run,
            lock_wait_seconds=0,
        )
        with pytest.raises(CanaryRepoError, match="clone-busy"):
            repository.open(REQUEST)
        assert run.commands == []
    finally:
        os.close(held)


def test_f6_the_clone_is_released_after_a_run_even_when_it_fails(
    tmp_path: Path,
) -> None:
    run = Recorder({"rev-parse": done(out="f" * 40), " push ": done(1)})
    with pytest.raises(CanaryRepoError, match="git-push-exit-1"):
        repo(tmp_path, run).open(REQUEST)
    held = os.open(tmp_path, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(held)


# Finding 7: the wait and the schedule leave room to publish a terminal reading.


def test_f7_a_poll_interval_over_the_bound_does_not_sleep_past_the_bound() -> None:
    base = operational_overlay()
    long_poll = base.model_copy(
        update={
            "target": base.target.model_copy(
                update={"poll_seconds": base.target.landing_bound_seconds * 2}
            )
        }
    )
    world, ledger = World(), InMemoryLedger()
    world.break_landing_tick_disabled()
    result = world.handler(ledger, long_poll).handle(COMMAND)
    waited = (world.clock.now - RUN_AT).total_seconds()
    assert waited <= base.target.landing_bound_seconds
    assert result.ending is EnumCanaryEnding.STALLED
    assert ledger.lines


def test_f7_an_install_whose_timeout_leaves_no_room_after_the_bound_is_refused(
    tmp_path: Path,
) -> None:
    base = overlay()
    tight = base.model_copy(
        update={
            "target": base.target.model_copy(
                update={"landing_bound_seconds": load_schedule().timeout_seconds}
            )
        }
    )
    sched = LandingCanaryScheduler(
        schedule=load_schedule(), overlay=tight, home=tmp_path, run=RecordingRunner()
    )
    result = sched.install(
        HOST, omni_root=REGISTRY_ROOT, overlay_root=OVERLAY_ROOT, dry_run=True
    )
    assert result.exit_code == 1
    assert "timeout" in result.stderr


# Finding 8: filesystem failures cross the port as the typed failure and publish UNKNOWN.


def test_f8_a_marker_directory_that_cannot_be_made_is_a_typed_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / ".canary").write_text("not a directory", encoding="utf-8")
    run = Recorder({"rev-parse": done(out="f" * 40)})
    with pytest.raises(CanaryRepoError, match="filesystem"):
        repo(tmp_path, run).open(REQUEST)


def test_f8_a_body_file_that_cannot_be_made_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)
    run = Recorder({"rev-parse": done(out="f" * 40)})
    with pytest.raises(CanaryRepoError, match="filesystem"):
        repo(tmp_path, run).open(REQUEST)


def test_f8_a_filesystem_failure_publishes_an_unknown_reading(tmp_path: Path) -> None:
    (tmp_path / ".canary").write_text("not a directory", encoding="utf-8")
    clock, ledger = FakeClock(), InMemoryLedger()
    handler = HandlerLandingCanary(
        overlay=operational_overlay(),
        host=HOST,
        repo=repo(tmp_path, Recorder({"rev-parse": done(out="f" * 40)})),
        appender=ledger,
        clock=clock,
        sleep=clock.sleep,
    )
    result = handler.handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.ending is EnumCanaryEnding.OPEN_FAILED
    assert result.ledger == "written"
    assert "verdict=UNKNOWN" in ledger.lines[0]
