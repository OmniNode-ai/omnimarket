# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive lab history older than seven days to Drive, then delete the local copy (OMN-17427).

Per declared source root: files not modified within the retention window are grouped
into parts, each staged as a compressed tarball on the staging drive, uploaded under a
bytes-per-second cap with nice and an IO class, and read back from Drive: the part is
verified only when Drive reports the same size and md5 as the staged file. Only then
is a source file deleted, and only if it still has the size and mtime it had when it
was staged. Nothing is deleted on any failure, and the run stops at the first failed
part. The live ledger and memory files are never archived. A host whose roles include
a refused role (the landing controller) is refused before any file is read.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import socket
import time
import zoneinfo
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, overload

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from ..adapters import RcloneDriveUploader, subprocess_runner, throttle_prefix
from ..models import (
    EnumLabHistoryArchivePartStatus,
    EnumLabHistoryArchiveRefusal,
    EnumLabHistoryArchiveVerdict,
    ModelArchiveHost,
    ModelArchiveSchedule,
    ModelArchiveSource,
    ModelLabHistoryArchiveDeployment,
    ModelLabHistoryArchivePartEvent,
    ModelLabHistoryArchiveRequest,
    ModelLabHistoryArchiveResult,
)
from ..overlay import LabHistoryArchiveConfigurationError, load_deployment_overlay
from ..protocols import (
    DriveUploadError,
    ProtocolCommandRunner,
    ProtocolDriveUploader,
    ProtocolPartEventSink,
)

LIVE_LEDGER_NAME = "ROLLING_WORK_LEDGER.md"
MEMORY_DIR = re.compile(r"^memory(-.*)?$")
MEMORY_FILE = "MEMORY.md"
STAGED_NAME = re.compile(r".+-\d{8}T\d{4}Z-\d{3}\.(tar\.gz|manifest\.json|list)$")
LOCK_NAME = ".lab-history-archive.lock"
TAR_TIMEOUT_S = 3600

DeviceOf = Callable[[Path], int]
_Entry = tuple[str, int, int]  # relative path, size, mtime_ns


def _gethostname() -> str:
    return socket.gethostname()


def _device_of(path: Path) -> int:
    return os.stat(path).st_dev


def is_protected(relative: Path, extra: tuple[str, ...] = ()) -> bool:
    """The live ledger and anything under a memory directory are never archived."""
    if relative.name in (LIVE_LEDGER_NAME, MEMORY_FILE):
        return True
    if any(MEMORY_DIR.match(part) for part in relative.parts[:-1]):
        return True
    posix = relative.as_posix()
    return any(
        fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(relative.name, pat)
        for pat in extra
    )


def _matches(relative: Path, patterns: tuple[str, ...]) -> bool:
    posix = relative.as_posix()
    return any(
        fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(relative.name, pat)
        for pat in patterns
    )


def eligible_files(
    source: ModelArchiveSource,
    *,
    cutoff: dt.datetime,
    protect: tuple[str, ...],
    skip_under: Path | None = None,
) -> list[_Entry]:
    """Regular, non-symlink files under the root last modified before the cutoff."""
    cutoff_ns = int(cutoff.timestamp() * 1e9)
    found: list[_Entry] = []
    for dirpath, dirnames, filenames in os.walk(source.root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not (here / d).is_symlink()
            and (skip_under is None or (here / d) != skip_under)
        )
        for fname in sorted(filenames):
            path = here / fname
            if path.is_symlink():
                continue
            relative = path.relative_to(source.root)
            if is_protected(relative, protect):
                continue
            if not _matches(relative, source.include) or _matches(
                relative, source.exclude
            ):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if not path.is_file() or st.st_mtime_ns >= cutoff_ns:
                continue
            found.append((relative.as_posix(), st.st_size, st.st_mtime_ns))
    return found


def group_parts(entries: list[_Entry], part_max_bytes: int) -> Iterator[list[_Entry]]:
    """Consecutive files up to the byte bar; a file over the bar is a part alone."""
    current: list[_Entry] = []
    size = 0
    for entry in entries:
        if current and size + entry[1] > part_max_bytes:
            yield current
            current, size = [], 0
        current.append(entry)
        size += entry[1]
    if current:
        yield current


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class HandlerLabHistoryArchive:
    """handle(request) archives one pass; handle(tick) gates the nightly schedule first."""

    def __init__(
        self,
        *,
        deployment: ModelLabHistoryArchiveDeployment | None = None,
        uploader: ProtocolDriveUploader | None = None,
        runner: ProtocolCommandRunner = subprocess_runner,
        device_of: DeviceOf | None = None,
        hostname: Callable[[], str] = _gethostname,
        now: Callable[[], dt.datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sink: ProtocolPartEventSink | None = None,
    ) -> None:
        self._deployment = deployment
        self._uploader = uploader
        self._runner = runner
        self._device_of: DeviceOf = device_of or (lambda p: _device_of(p))
        self._hostname = hostname
        self._now = now
        self._monotonic = monotonic
        self._sink = sink
        self._last_scheduled_run: dt.datetime | None = None

    # -- entry ---------------------------------------------------------------

    @overload
    def handle(
        self, request: ModelLabHistoryArchiveRequest
    ) -> ModelLabHistoryArchiveResult: ...

    @overload
    def handle(
        self, request: ModelRuntimeTick
    ) -> ModelLabHistoryArchiveResult | None: ...

    def handle(
        self, request: ModelLabHistoryArchiveRequest | ModelRuntimeTick
    ) -> ModelLabHistoryArchiveResult | None:
        if isinstance(request, ModelRuntimeTick):
            gated = self._gate_scheduled_run(request)
            if gated is None:
                return None
            request = gated
        host_name = request.host_name or self._hostname()
        try:
            deployment = self._deployment or load_deployment_overlay(
                request.overlay_path
            )
        except LabHistoryArchiveConfigurationError as exc:
            return self._refused(
                EnumLabHistoryArchiveRefusal.OVERLAY_INVALID, host_name, str(exc)
            )
        if deployment is None:
            return self._refused(
                EnumLabHistoryArchiveRefusal.OVERLAY_INVALID,
                host_name,
                "no deployment overlay: the host role cannot be read",
            )
        host = self._host(deployment, host_name)
        if host is None:
            return self._refused(
                EnumLabHistoryArchiveRefusal.HOST_ROLE_UNKNOWN,
                host_name,
                "this host is not listed in the overlay, so its role is unknown",
            )
        barred = sorted(set(host.roles) & set(deployment.refuse_roles))
        if barred:
            return self._refused(
                EnumLabHistoryArchiveRefusal.HOST_IS_LANDING_CONTROLLER,
                host.id,
                f"host carries the refused role {', '.join(barred)}",
            )
        as_of = (request.as_of or (self._now or _utc_now)()).astimezone(dt.UTC)
        cutoff = as_of - dt.timedelta(days=request.retention_days)
        placement = self._placement_refusal(deployment, host.id)
        if placement is not None:
            return placement
        uploader = self._uploader or RcloneDriveUploader(
            remote=deployment.drive.remote,
            budget=deployment.budget,
            runner=self._runner,
        )
        if not request.dry_run:
            not_ready = uploader.check_ready()
            if not_ready is not None:
                code, detail = not_ready
                return self._refused(
                    EnumLabHistoryArchiveRefusal(code), host.id, detail, cutoff
                )
        return self._run(deployment, host.id, uploader, as_of, cutoff, request.dry_run)

    # -- schedule ------------------------------------------------------------

    def _gate_scheduled_run(
        self, tick: ModelRuntimeTick
    ) -> ModelLabHistoryArchiveRequest | None:
        """A request to run now, or None when the tick is outside the window or interval."""
        try:
            deployment = self._deployment or load_deployment_overlay()
        except LabHistoryArchiveConfigurationError:
            deployment = None
        schedule = deployment.schedule if deployment else ModelArchiveSchedule()
        now = (self._now or (lambda: tick.now))().astimezone(dt.UTC)
        local = now.astimezone(zoneinfo.ZoneInfo(schedule.timezone)).time()
        if not _in_window(local, schedule.window_start, schedule.window_end):
            return None
        interval = dt.timedelta(seconds=schedule.run_interval_seconds)
        last = self._last_scheduled_run
        if last is not None and (now - last) < interval:
            return None
        self._last_scheduled_run = now
        return ModelLabHistoryArchiveRequest(as_of=now, dry_run=schedule.dry_run)

    # -- checks --------------------------------------------------------------

    @staticmethod
    def _host(
        deployment: ModelLabHistoryArchiveDeployment, name: str
    ) -> ModelArchiveHost | None:
        wanted = name.strip().lower()
        for host in deployment.hosts:
            if wanted == host.id.lower() or wanted in (
                h.lower() for h in host.hostnames
            ):
                return host
        return None

    def _placement_refusal(
        self, deployment: ModelLabHistoryArchiveDeployment, host: str
    ) -> ModelLabHistoryArchiveResult | None:
        staging = deployment.staging_dir.resolve()
        for source in deployment.sources:
            root = source.root.resolve()
            if staging == root or root in staging.parents or staging in root.parents:
                return self._refused(
                    EnumLabHistoryArchiveRefusal.STAGING_INSIDE_SOURCE,
                    host,
                    f"staging overlaps source {source.name}",
                )
            if not root.is_dir():
                continue
            staging.mkdir(parents=True, exist_ok=True)
            if self._device_of(staging) == self._device_of(root):
                return self._refused(
                    EnumLabHistoryArchiveRefusal.STAGING_ON_SOURCE_DEVICE,
                    host,
                    f"staging is on the same drive as source {source.name}",
                )
        return None

    @staticmethod
    def _refused(
        code: EnumLabHistoryArchiveRefusal,
        host: str,
        detail: str,
        cutoff: dt.datetime | None = None,
    ) -> ModelLabHistoryArchiveResult:
        return ModelLabHistoryArchiveResult(
            verdict=EnumLabHistoryArchiveVerdict.REFUSED,
            host=host,
            refusal=code,
            detail=detail,
            cutoff=cutoff,
        )

    # -- the run -------------------------------------------------------------

    def _run(
        self,
        deployment: ModelLabHistoryArchiveDeployment,
        host: str,
        uploader: ProtocolDriveUploader,
        as_of: dt.datetime,
        cutoff: dt.datetime,
        dry_run: bool,
    ) -> ModelLabHistoryArchiveResult:
        staging = deployment.staging_dir
        staging.mkdir(parents=True, exist_ok=True)
        lock = (staging / LOCK_NAME).open("a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return self._refused(
                    EnumLabHistoryArchiveRefusal.ALREADY_RUNNING,
                    host,
                    "another archive run holds the staging lock",
                    cutoff,
                )
            return self._run_locked(deployment, host, uploader, as_of, cutoff, dry_run)
        finally:
            lock.close()

    def _run_locked(
        self,
        deployment: ModelLabHistoryArchiveDeployment,
        host: str,
        uploader: ProtocolDriveUploader,
        as_of: dt.datetime,
        cutoff: dt.datetime,
        dry_run: bool,
    ) -> ModelLabHistoryArchiveResult:
        budget = deployment.budget
        staging = deployment.staging_dir
        # Staged copies are rebuilt from sources that are still intact, so a leftover
        # of a crashed run is dropped; only our own part names are touched.
        for leftover in staging.iterdir():
            if leftover.is_file() and STAGED_NAME.match(leftover.name):
                leftover.unlink()
        stamp = as_of.strftime("%Y%m%dT%H%MZ")
        started = self._monotonic()
        events: list[ModelLabHistoryArchivePartEvent] = []
        halted = False
        parts_started = 0
        for source in deployment.sources:
            if halted or not source.root.is_dir():
                continue
            entries = eligible_files(
                source,
                cutoff=cutoff,
                protect=deployment.protect,
                skip_under=staging if staging.is_relative_to(source.root) else None,
            )
            for seq, group in enumerate(
                group_parts(entries, budget.part_max_bytes), start=1
            ):
                if (
                    parts_started >= budget.max_parts_per_run
                    or self._monotonic() - started >= budget.max_run_seconds
                ):
                    halted = True
                    break
                parts_started += 1
                name = f"{source.name}-{stamp}-{seq:03d}"
                if dry_run:
                    event = ModelLabHistoryArchivePartEvent(
                        host=host,
                        source=source.name,
                        part=f"{name}.tar.gz",
                        status=EnumLabHistoryArchivePartStatus.DRY_RUN,
                        files=len(group),
                        bytes_source=sum(e[1] for e in group),
                    )
                else:
                    event = self._archive_part(
                        deployment, host, uploader, source, name, group
                    )
                events.append(event)
                if self._sink is not None:
                    self._sink.emit(event)
                if event.status not in (
                    EnumLabHistoryArchivePartStatus.ARCHIVED,
                    EnumLabHistoryArchivePartStatus.DRY_RUN,
                ):
                    halted = True
                    break
        if not events:
            verdict = EnumLabHistoryArchiveVerdict.NOTHING_TO_ARCHIVE
        elif dry_run:
            verdict = EnumLabHistoryArchiveVerdict.DRY_RUN
        elif any(e.status != EnumLabHistoryArchivePartStatus.ARCHIVED for e in events):
            verdict = EnumLabHistoryArchiveVerdict.FAILED
        else:
            verdict = EnumLabHistoryArchiveVerdict.ARCHIVED
        return ModelLabHistoryArchiveResult(
            verdict=verdict, host=host, cutoff=cutoff, parts=tuple(events)
        )

    # -- one part ------------------------------------------------------------

    def _archive_part(
        self,
        deployment: ModelLabHistoryArchiveDeployment,
        host: str,
        uploader: ProtocolDriveUploader,
        source: ModelArchiveSource,
        name: str,
        group: list[_Entry],
    ) -> ModelLabHistoryArchivePartEvent:
        budget = deployment.budget
        staging = deployment.staging_dir
        tarball = staging / f"{name}.tar.gz"
        manifest = staging / f"{name}.manifest.json"
        listing = staging / f"{name}.list"
        remote_dir = f"{deployment.drive.folder}/{host}/{source.name}"
        base: dict[str, Any] = {
            "host": host,
            "source": source.name,
            "part": tarball.name,
            "files": len(group),
            "bytes_source": sum(e[1] for e in group),
            "drive_path": f"{remote_dir}/{tarball.name}",
        }

        def failed(
            status: EnumLabHistoryArchivePartStatus, detail: str
        ) -> ModelLabHistoryArchivePartEvent:
            return ModelLabHistoryArchivePartEvent(status=status, detail=detail, **base)

        try:
            listing.write_bytes(b"".join(p.encode() + b"\0" for p, _, _ in group))
            manifest.write_text(
                json.dumps(
                    {
                        "host": host,
                        "source": source.name,
                        "root": str(source.root),
                        "part": tarball.name,
                        "files": [
                            {"path": p, "size": s, "mtime_ns": m} for p, s, m in group
                        ],
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
            prefix = throttle_prefix(budget)
            proc = self._runner(
                [
                    *prefix,
                    "tar",
                    "-czf",
                    str(tarball),
                    "-C",
                    str(source.root),
                    "--null",
                    "-T",
                    str(listing),
                ],
                None,
                TAR_TIMEOUT_S,
            )
            if proc.returncode != 0 or not tarball.is_file():
                return failed(
                    EnumLabHistoryArchivePartStatus.STAGE_FAILED,
                    f"tar exit {proc.returncode}: {proc.stderr.strip()[-200:]}",
                )
            sums = {p.name: (p.stat().st_size, _md5(p)) for p in (tarball, manifest)}
            try:
                for path in (tarball, manifest):
                    uploader.upload(path, remote_dir, budget.bytes_per_second)
            except DriveUploadError as exc:
                return failed(EnumLabHistoryArchivePartStatus.UPLOAD_FAILED, str(exc))
            try:
                for fname, (size, md5) in sums.items():
                    remote = uploader.remote_object(remote_dir, fname)
                    if remote is None:
                        return failed(
                            EnumLabHistoryArchivePartStatus.VERIFY_FAILED,
                            f"Drive does not list {fname} after upload",
                        )
                    if remote.size != size or remote.md5 != md5:
                        return failed(
                            EnumLabHistoryArchivePartStatus.VERIFY_FAILED,
                            f"{fname}: Drive reports size {remote.size} md5 {remote.md5}, "
                            f"staged size {size} md5 {md5}",
                        )
            except DriveUploadError as exc:
                return failed(EnumLabHistoryArchivePartStatus.VERIFY_FAILED, str(exc))
            deleted = kept = freed = 0
            for rel, size, mtime_ns in group:
                path = source.root / rel
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if (
                    path.is_symlink()
                    or st.st_size != size
                    or st.st_mtime_ns != mtime_ns
                ):
                    kept += 1
                    continue
                path.unlink()
                deleted += 1
                freed += size
            return ModelLabHistoryArchivePartEvent(
                status=EnumLabHistoryArchivePartStatus.ARCHIVED,
                bytes_staged=sums[tarball.name][0],
                md5=sums[tarball.name][1],
                files_deleted=deleted,
                files_kept_changed=kept,
                bytes_freed=freed,
                **base,
            )
        finally:
            for leftover in (tarball, manifest, listing):
                leftover.unlink(missing_ok=True)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _in_window(local: dt.time, start: dt.time, end: dt.time) -> bool:
    if start <= end:
        return start <= local < end
    return local >= start or local < end
