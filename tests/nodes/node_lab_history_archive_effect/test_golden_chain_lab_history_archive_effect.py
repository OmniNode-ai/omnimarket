# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_lab_history_archive_effect (OMN-17427).

The invariant: a local file is deleted only after Drive reports the size and md5 of the
staged part that holds it, and never when it is protected, fresh, changed since staging,
or on a host that carries the landing controller role.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import ValidationError

from omnimarket.nodes.node_lab_history_archive_effect.handlers import (
    handler_lab_history_archive as handler_module,
)
from omnimarket.nodes.node_lab_history_archive_effect.handlers.handler_lab_history_archive import (
    HandlerLabHistoryArchive,
    group_parts,
    is_protected,
)
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    EnumLabHistoryArchivePartStatus as PartStatus,
)
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    EnumLabHistoryArchiveRefusal as Refusal,
)
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    EnumLabHistoryArchiveVerdict as Verdict,
)
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    ModelArchiveBudget,
    ModelLabHistoryArchiveDeployment,
    ModelLabHistoryArchiveRequest,
)

from .conftest import AS_OF, FakeDrive, RecordingRunner, distinct_devices, write

pytestmark = pytest.mark.unit


class ListSink:
    def __init__(self, events: list) -> None:
        self.events = events

    def emit(self, event: object) -> None:
        self.events.append(event)


def make(
    deployment: ModelLabHistoryArchiveDeployment,
    drive: FakeDrive,
    *,
    host: str = "h101",
    runner: RecordingRunner | None = None,
    events: list | None = None,
    **extra: object,
) -> HandlerLabHistoryArchive:
    kwargs: dict = {
        "deployment": deployment,
        "uploader": drive,
        "device_of": distinct_devices,
        "hostname": lambda: host,
        "now": lambda: AS_OF,
        "sink": ListSink(events) if events is not None else None,
    }
    if runner is not None:
        kwargs["runner"] = runner
    kwargs.update(extra)
    return HandlerLabHistoryArchive(**kwargs)


def run(h: HandlerLabHistoryArchive, **req: object):
    return h.handle(ModelLabHistoryArchiveRequest(as_of=AS_OF, **req))


def test_old_history_is_archived_verified_then_deleted(lab, deployment, drive) -> None:
    events: list = []
    runner = RecordingRunner()
    result = run(make(deployment, drive, runner=runner, events=events))

    assert result.verdict == Verdict.ARCHIVED
    assert [e.status for e in result.parts] == [PartStatus.ARCHIVED] * 2
    assert events == list(result.parts)
    # old, unprotected files are gone; fresh, memory and the live ledger stay
    assert not (lab["sessions"] / "a.jsonl").exists()
    assert not (lab["sessions"] / "b.jsonl").exists()
    assert not (lab["reports"] / "r1.md").exists()
    assert not (
        lab["reports"] / "archive" / "ROLLING_WORK_LEDGER_2026-10-12-split.md"
    ).exists()
    assert (lab["sessions"] / "fresh.jsonl").exists()
    assert (lab["sessions"] / "memory" / "notes.md").exists()
    assert (lab["sessions"] / "MEMORY.md").exists()
    assert (lab["reports"] / "ROLLING_WORK_LEDGER.md").exists()
    # the part and its manifest reached host/source under the folder, at the byte cap
    part = result.parts[0]
    assert part.drive_path.startswith("OmniNode Lab Archive/h101/sessions/")
    assert {u[2] for u in drive.uploads} == {2_000_000}
    names = [n for (_, n) in drive.objects]
    assert sum(n.endswith(".tar.gz") for n in names) == 2
    assert sum(n.endswith(".manifest.json") for n in names) == 2
    manifest = json.loads(
        next(
            v
            for (_, n), v in drive.objects.items()
            if n.endswith("sessions-20261017T0600Z-001.manifest.json")
        )
    )
    assert sorted(f["path"] for f in manifest["files"]) == ["a.jsonl", "b.jsonl"]
    assert part.bytes_freed == 7000
    assert part.files_deleted == 2
    assert result.bytes_freed == 7000 + len(b"report") + len(b"detail")
    # staging is empty again (apart from the lock) and every command ran niced
    assert [p.name for p in lab["staging"].iterdir()] == [".lab-history-archive.lock"]
    assert all(a[0] == "nice" for a in runner.argvs)
    assert any("tar" in a for a in runner.argvs)


def test_staged_tarball_holds_the_archived_bytes(lab, deployment, drive) -> None:
    run(make(deployment, drive))
    blob = next(
        v
        for (_, n), v in drive.objects.items()
        if n.startswith("sessions") and n.endswith(".tar.gz")
    )
    out = lab["staging"].parent / "x.tar.gz"
    out.write_bytes(blob)
    with tarfile.open(out) as tf:
        member = tf.extractfile("a.jsonl")
        assert member is not None
        assert member.read() == b"A" * 4000


def test_a_part_drive_reports_wrongly_deletes_nothing_and_stops_the_run(
    lab, deployment, drive
) -> None:
    drive.corrupt_md5 = True
    result = run(make(deployment, drive))

    assert result.verdict == Verdict.FAILED
    assert [e.status for e in result.parts] == [
        PartStatus.VERIFY_FAILED
    ]  # the reports source never starts
    assert (lab["sessions"] / "a.jsonl").exists()
    assert (lab["sessions"] / "b.jsonl").exists()
    assert (lab["reports"] / "r1.md").exists()
    assert "md5" in result.parts[0].detail
    assert [p.name for p in lab["staging"].iterdir()] == [".lab-history-archive.lock"]


def test_a_missing_remote_object_fails_verification(lab, deployment, drive) -> None:
    drive.hide_objects = True
    result = run(make(deployment, drive))
    assert result.parts[0].status == PartStatus.VERIFY_FAILED
    assert (lab["sessions"] / "a.jsonl").exists()


def test_an_upload_failure_deletes_nothing(lab, deployment, drive) -> None:
    drive.fail_upload = True
    result = run(make(deployment, drive))
    assert result.verdict == Verdict.FAILED
    assert result.parts[0].status == PartStatus.UPLOAD_FAILED
    assert (lab["sessions"] / "a.jsonl").exists()


def test_a_file_changed_after_staging_is_kept(lab, deployment, drive) -> None:
    target = lab["sessions"] / "a.jsonl"
    drive.on_upload = lambda local: (
        write(target, b"appended later" * 10, AS_OF.timestamp())
        if local.name.startswith("sessions")
        else None
    )
    result = run(make(deployment, drive))
    first = result.parts[0]
    assert first.status == PartStatus.ARCHIVED
    assert first.files_kept_changed == 1
    assert first.files_deleted == 1
    assert target.exists()
    assert not (lab["sessions"] / "b.jsonl").exists()


def test_dry_run_lists_parts_and_touches_nothing(lab, deployment, drive) -> None:
    runner = RecordingRunner()
    result = run(make(deployment, drive, runner=runner), dry_run=True)
    assert result.verdict == Verdict.DRY_RUN
    assert {p.status for p in result.parts} == {PartStatus.DRY_RUN}
    assert drive.uploads == []
    assert runner.argvs == []
    assert (lab["sessions"] / "a.jsonl").exists()


def test_nothing_old_means_nothing_to_archive(lab, deployment, drive) -> None:
    result = HandlerLabHistoryArchive(
        deployment=deployment,
        uploader=drive,
        device_of=distinct_devices,
        hostname=lambda: "h101",
    ).handle(ModelLabHistoryArchiveRequest(as_of=AS_OF - dt.timedelta(days=30)))
    assert result.verdict == Verdict.NOTHING_TO_ARCHIVE


def test_retention_never_goes_below_seven_days(lab, deployment, drive) -> None:
    with pytest.raises(ValidationError):
        ModelLabHistoryArchiveRequest(retention_days=3)
    write(
        lab["sessions"] / "four-days.jsonl",
        b"x",
        (AS_OF - dt.timedelta(days=4)).timestamp(),
    )
    run(make(deployment, drive))
    assert (lab["sessions"] / "four-days.jsonl").exists()


def test_the_landing_controller_host_is_refused_before_any_file_is_read(
    lab, deployment, drive, monkeypatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("a file was read on the landing controller host")

    monkeypatch.setattr(handler_module, "eligible_files", boom)
    for name in ("h200", "h200.lab"):
        result = run(make(deployment, drive, host=name))
        assert result.verdict == Verdict.REFUSED
        assert result.refusal == Refusal.HOST_IS_LANDING_CONTROLLER
        assert result.host == "h200"
    assert drive.uploads == []
    assert (lab["sessions"] / "a.jsonl").exists()


def test_a_host_the_overlay_does_not_list_is_refused(lab, deployment, drive) -> None:
    result = run(make(deployment, drive, host="mystery"))
    assert result.refusal == Refusal.HOST_ROLE_UNKNOWN
    assert (lab["sessions"] / "a.jsonl").exists()


def test_no_overlay_is_a_refusal_not_a_guess(lab, drive, monkeypatch) -> None:
    monkeypatch.delenv("OMNIMARKET_LAB_HISTORY_ARCHIVE_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    h = HandlerLabHistoryArchive(uploader=drive, hostname=lambda: "h101")
    result = h.handle(ModelLabHistoryArchiveRequest(as_of=AS_OF))
    assert result.refusal == Refusal.OVERLAY_INVALID


def test_staging_on_a_source_drive_or_inside_a_source_is_refused(
    lab, deployment, drive
) -> None:
    same = make(deployment, drive, device_of=lambda _: 1)
    assert run(same).refusal == Refusal.STAGING_ON_SOURCE_DEVICE
    inside = deployment.model_copy(update={"staging_dir": lab["sessions"] / "stage"})
    assert run(make(inside, drive)).refusal == Refusal.STAGING_INSIDE_SOURCE
    assert (lab["sessions"] / "a.jsonl").exists()


def test_a_missing_uploader_credential_is_a_typed_refusal(
    lab, deployment, drive
) -> None:
    drive.ready = (
        "CREDENTIAL_MISSING",
        "no rclone remote named lab-archive is configured",
    )
    result = run(make(deployment, drive))
    assert result.refusal == Refusal.CREDENTIAL_MISSING
    assert drive.uploads == []
    assert (lab["sessions"] / "a.jsonl").exists()


def test_a_second_run_cannot_start_while_one_holds_the_lock(
    lab, deployment, drive
) -> None:
    import fcntl

    lab["staging"].mkdir(parents=True)
    with (lab["staging"] / ".lab-history-archive.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = run(make(deployment, drive))
    assert result.refusal == Refusal.ALREADY_RUNNING


def test_parts_split_on_the_byte_bar_and_an_oversize_file_is_alone() -> None:
    entries = [("a", 40, 1), ("b", 40, 1), ("c", 40, 1), ("big", 500, 1), ("d", 10, 1)]
    parts = [[e[0] for e in g] for g in group_parts(entries, 100)]
    assert parts == [["a", "b"], ["c"], ["big"], ["d"]]


def test_the_part_and_time_budgets_bound_a_run(lab, deployment, drive) -> None:
    for i in range(5):
        write(
            lab["reports"] / f"extra{i}.md",
            b"x" * 50,
            lab["reports"].stat().st_mtime - 1e6,
        )
    small = deployment.model_copy(
        update={"budget": ModelArchiveBudget(part_max_bytes=60, max_parts_per_run=2)}
    )
    result = run(make(small, drive))
    assert len(result.parts) == 2
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(100.0))
    timed = deployment.model_copy(
        update={"budget": ModelArchiveBudget(max_run_seconds=5)}
    )
    result2 = run(make(timed, drive, monotonic=lambda: float(next(ticks))))
    assert len(result2.parts) == 1


def test_protected_paths() -> None:
    assert is_protected(Path("ROLLING_WORK_LEDGER.md"))
    assert is_protected(Path("x/memory/y.md"))
    assert is_protected(Path("x/memory-notes/y.md"))
    assert is_protected(Path("p/MEMORY.md"))
    assert is_protected(Path("p/secret.env"), ("*.env",))
    assert not is_protected(Path("archive/ROLLING_WORK_LEDGER_2026-10-12-split.md"))


def test_symlinks_are_neither_archived_nor_followed(
    lab, deployment, drive, tmp_path
) -> None:
    outside = write(tmp_path / "outside.jsonl", b"outside", 1.0)
    (lab["sessions"] / "link.jsonl").symlink_to(outside)
    run(make(deployment, drive))
    assert outside.exists()
    assert (lab["sessions"] / "link.jsonl").is_symlink()


# -- the nightly tick ---------------------------------------------------------


def tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=60000,
    )


def test_a_tick_runs_only_in_the_eastern_off_peak_window_once_per_interval(
    lab, deployment, drive
) -> None:
    clock = [AS_OF]
    h = make(deployment, drive, now=lambda: clock[0])
    # 06:00Z is 02:00 EDT: inside the 01:00-05:00 window
    first = h.handle(tick(clock[0]))
    assert first is not None
    assert first.verdict == Verdict.ARCHIVED
    clock[0] = AS_OF + dt.timedelta(minutes=30)
    assert h.handle(tick(clock[0])) is None  # inside the interval
    clock[0] = AS_OF + dt.timedelta(hours=14)  # 20:00Z = 16:00 EDT, peak
    assert h.handle(tick(clock[0])) is None
    clock[0] = AS_OF + dt.timedelta(hours=25)  # next night 03:00 EDT
    again = h.handle(tick(clock[0]))
    assert again is not None
    assert again.verdict == Verdict.NOTHING_TO_ARCHIVE


def test_a_tick_on_the_landing_controller_host_is_refused_in_window(
    lab, deployment, drive
) -> None:
    h = make(deployment, drive, host="h200")
    result = h.handle(tick(AS_OF))
    assert result is not None
    assert result.refusal == Refusal.HOST_IS_LANDING_CONTROLLER
    assert (lab["sessions"] / "a.jsonl").exists()
