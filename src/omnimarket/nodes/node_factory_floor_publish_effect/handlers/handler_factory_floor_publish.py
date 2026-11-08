# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refresh the factory floor's inputs and record, then publish the page and its data to the serving host.

One run executes the contract's input steps in order (the lab host advertisements, the friction report, the
process-health series, and last the factory floor record itself), then copies the page, the status of every
step, the process-health series when there is one, and the record to the directory a static server serves, over
ssh, one file at a time: each is written to a temporary name and renamed into place, so a reader never sees a
partial file and a failed copy leaves the previous file untouched. A step that is not required may fail; the
status file says so and the page shows that source as unread rather than empty. The host, the directory, the
URL and the bus lane are deployment facts from the overlay; the paths, names and time limits are the contract's.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    HandlerDelegationReport,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError as PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    Runner as Runner,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    run_process as run_process,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_fleet_observation_source import (
    HandlerFleetObservationSource,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_friction_rollup_source import (
    HandlerFrictionRollupSource,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_process_health_payload import (
    HandlerProcessHealthPayload,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReportRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelDeclaredLines,
    ModelFactoryFloorPublishContract,
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishResult,
    ModelFactoryFloorPublishRuntime,
    ModelPublishHistory,
    ModelPublishStep,
    ModelPublishStepResult,
    ModelPublishTarget,
    group_steps,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_fleet_observation_source import (
    ModelFleetObservationSourceRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_process_health_payload import (
    ModelProcessHealthPayloadRequest,
)

NODE_NAME = "node_factory_floor_publish_effect"
NODE_DIR = Path(__file__).resolve().parents[1]
OWN_CONTRACT = NODE_DIR / "contract.yaml"

SSH_CONNECTION_ERROR = 255
SSH_CONNECT_RETRIES = 2
SSH_RETRY_SECONDS = 10.0


def load_runtime(contract_path: Path = OWN_CONTRACT) -> ModelFactoryFloorPublishRuntime:
    """The ``runtime`` block of this node's contract."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelFactoryFloorPublishContract.model_validate(raw).runtime


def remote_write_command(remote_dir: str, name: str, *, exclusive: bool = False) -> str:
    """The remote shell command that stores stdin as ``remote_dir/name`` atomically.

    The bytes go to a dot-named temporary file in the final file's own directory (``name`` may carry
    sub-directories, which are created) and are renamed over the final name; when the write fails the temporary
    file is removed and the previous final file is left as it was. ``exclusive`` refuses to replace an existing final
    file (the append-only history), leaving it as it was.
    """
    final = PurePosixPath(remote_dir) / name
    tmp = final.parent / f".{final.name}.tmp"
    q_dir, q_final, q_tmp = (shlex.quote(str(p)) for p in (final.parent, final, tmp))
    guard = f"[ ! -e {q_final} ] && " if exclusive else ""
    move = "mv -n" if exclusive else "mv -f"
    return (
        f"umask 022 && mkdir -p {q_dir} && {guard}"
        f"{{ cat > {q_tmp} && {move} {q_tmp} {q_final}; }} || {{ rm -f {q_tmp}; exit 1; }}"
    )


def snapshot_name(
    history: ModelPublishHistory, record_name: str, moment: datetime
) -> str:
    """``<subdirectory>/<stem>-<UTC timestamp><suffix>`` for the record file ``record_name`` published at ``moment``."""
    stem, dot, ext = PurePosixPath(record_name).name.rpartition(".")
    if not dot:
        stem, ext = ext, ""
    stamp = moment.astimezone(UTC).strftime(history.timestamp_format)
    return f"{history.subdirectory}/{stem}-{stamp}{dot}{ext}"


def _snapshot_time(
    history: ModelPublishHistory, record_name: str, file_name: str
) -> datetime | None:
    """The UTC time a history file name carries, or ``None`` when it is not one of this record's snapshots."""
    stem, dot, ext = PurePosixPath(record_name).name.rpartition(".")
    if not dot:
        stem, ext = ext, ""
    head, tail = f"{stem}-", f"{dot}{ext}"
    if not (file_name.startswith(head) and file_name.endswith(tail)):
        return None
    stamp = file_name[len(head) : len(file_name) - len(tail)]
    try:
        return datetime.strptime(stamp, history.timestamp_format).replace(tzinfo=UTC)
    except ValueError:
        return None


def prunable_snapshots(
    history: ModelPublishHistory,
    record_name: str,
    file_names: Sequence[str],
    now: datetime,
) -> tuple[str, ...]:
    """The snapshot file names to remove: past the window, oldest first, only while the count exceeds ``max_count``.

    A name that is not a snapshot of this record is never listed, and a snapshot inside the window never is.
    """
    cutoff = now.astimezone(UTC) - timedelta(days=history.retention.days)
    dated = sorted(
        (t, n)
        for n in file_names
        if (t := _snapshot_time(history, record_name, n)) is not None
    )
    excess = len(dated) - history.retention.max_count
    return tuple(n for t, n in dated if t < cutoff)[: max(excess, 0)]


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _within_seconds[T](seconds: float, work: Callable[[], T], message: str) -> T:
    """The result of ``work``, or ``TimeoutError(message)`` once ``seconds`` have passed.

    The work runs in a daemon thread so a stuck read or a backtracking pattern cannot hold the publication: the caller
    reports the step failed and goes on. The thread is left to finish on its own; it holds no lock the cycle needs.
    """
    outcome: list[T] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:
            failure.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(message)
    if failure:
        raise failure[0]
    return outcome[0]


def _tail(raw: bytes) -> str:
    return " | ".join(raw.decode("utf-8", "replace").strip().splitlines()[-3:])


def ssh_argv(
    rt: ModelFactoryFloorPublishRuntime,
    overlay: ModelFactoryFloorPublishOverlay,
    remote_command: str,
) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={rt.transport.connect_timeout_seconds}",
        "--",
        overlay.ssh_target,
        remote_command,
    ]


class PhaseBudget:
    """The seconds one phase of a run may still use (OMN-22699): every call in the phase is clamped to what is left."""

    def __init__(
        self, clock: Callable[[], float], seconds: int, phase: str, cap: int
    ) -> None:
        self._clock = clock
        self._end = clock() + seconds
        self._phase = phase
        self._cap = cap

    def remaining(self) -> float:
        return self._end - self._clock()

    def timeout(self) -> int:
        """The limit for the next call: the transfer limit or what the phase has left, whichever is less."""
        left = math.ceil(self.remaining())
        if left <= 0:
            raise PublishRefusedError(
                f"the {self._phase} budget of this cycle is spent; the previous files on the server are untouched"
            )
        return min(self._cap, left)


class CopyOutcome:
    """Where one file went: the hosts that stored it and, for each that did not, why (OMN-23081)."""

    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.failed: dict[str, str] = {}


class DeliveryTally:
    """What one run delivered to each serving host: a host that missed any file is named with its first reason."""

    def __init__(self, targets: Sequence[str]) -> None:
        self._targets = tuple(targets)
        self.failed: dict[str, str] = {}

    def record(self, outcome: CopyOutcome) -> None:
        for target, reason in outcome.failed.items():
            self.failed.setdefault(target, reason)

    def served_to(self) -> tuple[str, ...]:
        return tuple(t for t in self._targets if t not in self.failed)


def local_copy_root(
    overlay: ModelFactoryFloorPublishOverlay, home: Path
) -> Path | None:
    """The directory the publishing machine keeps its own copy of every published file in, or ``None`` when the overlay names none."""
    if overlay.local_copy_relpath is None:
        return None
    return home / overlay.local_copy_relpath


def _write_local_copy(
    local_root: Path, name: str, payload: bytes, *, exclusive: bool
) -> None:
    path = local_root / name
    if exclusive and path.exists():
        return
    write_atomic(path, payload)


def copy_to_server(
    run: Runner,
    rt: ModelFactoryFloorPublishRuntime,
    overlay: ModelFactoryFloorPublishOverlay,
    name: str,
    payload: bytes,
    *,
    root: str | None = None,
    history: bool = False,
    exclusive: bool = False,
    budget: PhaseBudget | None = None,
    local_root: Path | None = None,
    unreachable: set[str] | None = None,
) -> CopyOutcome:
    """Store ``payload`` as ``name`` on the publishing machine and on every serving host over ssh; a failed host keeps its old file.

    The local copy (``local_root``, when the overlay names one) is written first, so it holds the file even when every
    host is down. Each serving host is tried on its own: one host failing never stops another from being served, and a
    host whose connection did not open is skipped for the rest of the run (``unreachable``). The call raises
    ``PublishRefusedError`` only when no serving host stored the file. ``root`` writes under one explicit directory of
    a single-host overlay (the access include); ``history`` writes under each host's own history root.

    With a ``budget`` each attempt and each wait between attempts is clamped to what the phase has left.
    """
    outcome = CopyOutcome()
    targets = overlay.targets
    if root is not None and len(targets) > 1:
        raise ValueError(
            "an explicit root names a path on one host: pass a single-host overlay"
        )
    if local_root is not None and root is None:
        try:
            _write_local_copy(local_root, name, payload, exclusive=exclusive)
        except OSError as exc:
            outcome.failed["local"] = f"writing {name} to the local copy failed: {exc}"
    down = unreachable if unreachable is not None else set()
    fanned = len(targets) > 1

    def limit() -> int:
        return (
            budget.timeout()
            if budget is not None
            else rt.transport.transfer_timeout_seconds
        )

    def one(target: ModelPublishTarget) -> str | None:
        """The reason this host did not store the file, or ``None`` when it did."""
        if target.ssh_target in down:
            return "unreachable earlier in this run; skipped"
        view = overlay.for_target(target)
        directory = root or (target.history_root if history else target.remote_dir)
        argv = ssh_argv(
            rt, view, remote_write_command(directory, name, exclusive=exclusive)
        )
        try:
            done = run(argv, payload, limit())
            # A fanned-out publish does not retry a connection that did not open: the other host is served now and this one on the
            # next run, so a down host costs one connect timeout, not a retry ladder per file.
            for _ in range(0 if fanned else SSH_CONNECT_RETRIES):
                if done.returncode != SSH_CONNECTION_ERROR:
                    break
                # ssh's own exit 255 is a connection that did not open (2026-11-02 18:23Z: "Network is unreachable" for a few
                # seconds on the launching Mac refused a whole 10-minute run); the remote command did not run, so it is tried again.
                wait = SSH_RETRY_SECONDS
                if budget is not None:
                    wait = max(0.0, min(wait, budget.remaining()))
                time.sleep(wait)
                done = run(argv, payload, limit())
        except PublishRefusedError as exc:
            return str(exc)
        if done.returncode == 0:
            return None
        if done.returncode == SSH_CONNECTION_ERROR:
            down.add(target.ssh_target)
        return (
            f"publishing {name} failed with exit {done.returncode}: {_tail(done.stderr)}; "
            "the previous file on the server is untouched"
        )

    if fanned:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            reasons = list(pool.map(one, targets))
    else:
        reasons = [one(targets[0])]
    for target, reason in zip(targets, reasons, strict=True):
        if reason is None:
            outcome.delivered.append(target.ssh_target)
        else:
            outcome.failed[target.ssh_target] = reason
    if not outcome.delivered:
        raise PublishRefusedError(
            str(reasons[0])
            if not fanned
            else "; ".join(
                f"{t.ssh_target}: {r}" for t, r in zip(targets, reasons, strict=True)
            )
        )
    return outcome


class HandlerFactoryFloorPublish:
    """Boundary of the publish: the input steps, the reads and the copies happen here."""

    def __init__(
        self,
        runner: Runner = run_process,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        unreachable: set[str] | None = None,
    ) -> None:
        self._run = runner
        self._clock = clock
        self._wall_clock = wall_clock
        # The serving hosts whose connection did not open in this run: later copies skip them (OMN-23081).
        self._unreachable: set[str] = unreachable if unreachable is not None else set()
        self._tally = DeliveryTally(())
        self._local_root: Path | None = None
        self._served: (
            tuple[
                ModelFactoryFloorPublishRuntime,
                ModelFactoryFloorPublishOverlay,
                dict[str, Any],
            ]
            | None
        ) = None

    def handle(
        self,
        request: ModelFactoryFloorPublishRequest,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ModelFactoryFloorPublishResult:
        """Raises ``PublishRefusedError`` or ``NodeOverlayError`` naming what is missing."""
        env = os.environ if environ is None else environ
        rt = load_runtime()
        overlay = load_structured_overlay(
            NODE_NAME,
            ModelFactoryFloorPublishOverlay,
            env,
            explicit=request.overlay,
            file_env=rt.overlay_file_env,
        )
        home_value = (request.home or env.get(rt.home_env, "")).strip()
        if not home_value:
            raise PublishRefusedError(
                f"{rt.home_env} is not set (fail fast, no default): export it or pass --omni-home"
            )
        home = Path(home_value)
        now = request.now or datetime.now(UTC)
        self._local_root = local_copy_root(overlay, home)
        self._tally = DeliveryTally([t.ssh_target for t in overlay.targets])

        cycle_start = self._clock()
        phases: dict[str, float] = {}
        results: list[ModelPublishStepResult] = []
        steps_start = self._clock()
        if not request.skip_refresh:
            for group in self._groups(rt.steps):
                for step, result in zip(
                    group, self._run_group(group, home, overlay, rt, env), strict=True
                ):
                    results.append(result)
                    if result.status == "failed" and step.required:
                        raise PublishRefusedError(
                            f"the required step {step.name} failed ({result.detail}): {result.diagnostic}"
                        )

        phases["input_steps"] = self._clock() - steps_start
        data = self._read_record(home / rt.source.relpath)
        record = json.loads(data)
        generated_at = str(record["generated_at"])
        results = self._flag_undrawn(
            results, rt, self._declared_fragments(rt, env), record["lines"]
        )
        page = (NODE_DIR / rt.page_relpath).read_bytes()
        simple_page = (NODE_DIR / rt.simple_page_relpath).read_bytes()
        simple_data = self._read_simple(home / rt.simple_data_relpath)
        simple_reads = self._read_simple(home / rt.simple_reads_relpath)
        compare_page = (NODE_DIR / rt.compare_page_relpath).read_bytes()
        compare_data = self._read_simple(home / rt.compare_data_relpath)
        floor_page = (NODE_DIR / rt.floor_page_relpath).read_bytes()
        theme_css = [
            (name, (NODE_DIR / rt.theme_css_relpath / name).read_bytes())
            for name in rt.names.theme_css
        ]
        icons = [
            (name, (NODE_DIR / rt.icons_relpath / name).read_bytes())
            for name in rt.names.icons
        ]
        health_payload = HandlerProcessHealthPayload().handle(
            ModelProcessHealthPayloadRequest(
                source_path=str(home / rt.process_health_relpath)
            )
        )
        health = health_payload.summary
        delegation_start = self._clock()
        if not request.skip_refresh or request.refresh_delegation:
            reading = HandlerDelegationReport(self._run).handle(
                ModelDelegationReportRequest(
                    probe=True, timeout_seconds=rt.cycle.delegation_timeout_seconds
                ),
                environ=env,
            )
            write_atomic(
                home / rt.delegation_relpath, json.dumps(reading.report).encode()
            )
        delegation = self._read_delegation(home / rt.delegation_relpath)
        phases["delegation"] = self._clock() - delegation_start
        status_document: dict[str, Any] = {
            "cycle_seconds_before_copy": round(self._clock() - cycle_start, 3),
            "cycle_bound_seconds": rt.cycle.bound_seconds,
            "phase_seconds": {k: round(v, 3) for k, v in phases.items()},
            "published_at": now.astimezone(UTC).isoformat(),
            "steps": [r.model_dump(exclude={"diagnostic"}) for r in results],
            "simple_floor": "published" if simple_data is not None else "absent",
            "floor_compare": "published" if compare_data is not None else "absent",
            "process_health": "published" if health is not None else "absent",
            "process_health_reason": health_payload.reason,
            "delegation": "published" if delegation is not None else "absent",
            "lines_declared": self._declared_lines(rt, env),
            "serve_urls": list(overlay.serve_urls),
            "local_copy": overlay.local_copy_relpath or "",
        }
        status = self._status_bytes(status_document)

        # The record goes last: a reader that gets the new record always finds a page that reads it.
        files: list[tuple[str, bytes]] = [
            *theme_css,
            *icons,
            (rt.names.floor_page, floor_page),
            (rt.names.page, page),
            (rt.names.simple_page, simple_page),
            (rt.names.compare_page, compare_page),
            (rt.names.status, status),
        ]
        if simple_reads is not None:
            files.append((rt.names.simple_reads, simple_reads))
        if simple_data is not None:
            files.append((rt.names.simple_data, simple_data))
        if compare_data is not None:
            files.append((rt.names.compare_data, compare_data))
        if health is not None:
            if health_payload.detail is not None:
                files.append((health_payload.detail_file, health_payload.detail))
            files.append((rt.names.process_health, health))
        if delegation is not None:
            files.append((rt.names.delegation, delegation))
        files.append((rt.names.data, data))
        # The snapshot is stored before the live record, so a record that is shown has always been kept.
        snapshot = snapshot_name(rt.history, rt.names.data, now)
        # The local copy is complete before any host is tried, so it holds the whole publish even when every host is down.
        self._keep_local_copy([(snapshot, data), *files], snapshot)
        copy_start = self._clock()
        copy_budget = PhaseBudget(
            self._clock,
            rt.cycle.copy_budget_seconds,
            "copy",
            rt.transport.transfer_timeout_seconds,
        )
        try:
            self._tally.record(
                copy_to_server(
                    self._run,
                    rt,
                    overlay,
                    snapshot,
                    data,
                    history=True,
                    exclusive=True,
                    budget=copy_budget,
                    unreachable=self._unreachable,
                )
            )
            for name, payload in files:
                self._copy(rt, overlay, name, payload, copy_budget)
        finally:
            phases["copy"] = self._clock() - copy_start
        prune_start = self._clock()
        try:
            pruned = self._prune_history(rt, overlay, now)
        finally:
            phases["history_prune"] = self._clock() - prune_start

        # The page and the record are served. The canary status is published from them, so its floor age is the page's.
        after: list[ModelPublishStepResult] = []
        after_start = self._clock()
        for after_step in rt.after_publish_steps:
            after_result = self._step(
                after_step, home, overlay, rt.plugin_root_env, env
            )
            after.append(after_result)
            if after_result.status == "failed" and after_step.required:
                phases["after_publish"] = self._clock() - after_start
                failed_timing = self._timing(cycle_start, phases, results, after)
                if request.state_dir:
                    self._write_evidence(
                        Path(request.state_dir) / rt.evidence_file,
                        now,
                        generated_at,
                        hashlib.sha256(data).hexdigest(),
                        tuple(name for name, _ in files),
                        failed_timing,
                    )
                self._write_final_status(
                    rt, overlay, status_document, failed_timing, after
                )
                raise PublishRefusedError(
                    f"the page was served but the required step {after_step.name} failed "
                    f"({after_result.detail}): {after_result.diagnostic}"
                )

        phases["after_publish"] = self._clock() - after_start
        digest = hashlib.sha256(data).hexdigest()
        timing = self._timing(cycle_start, phases, results, after)
        if request.state_dir:
            self._write_evidence(
                Path(request.state_dir) / rt.evidence_file,
                now,
                generated_at,
                digest,
                tuple(name for name, _ in files),
                timing,
            )
        self._write_final_status(rt, overlay, status_document, timing, after)
        return ModelFactoryFloorPublishResult(
            published=tuple(name for name, _ in files),
            generated_at=generated_at,
            data_sha256=digest,
            serve_url=overlay.serve_url,
            steps=tuple(results),
            cycle_seconds=timing["cycle_seconds"],
            phase_seconds={k: round(v, 3) for k, v in phases.items()},
            after_publish=tuple(after),
            process_health_published=health is not None,
            delegation_published=delegation is not None,
            history_snapshot=snapshot,
            history_pruned=pruned,
            serve_urls=overlay.serve_urls,
            served_to=self._tally.served_to(),
            failed_targets=dict(self._tally.failed),
            local_copy=overlay.local_copy_relpath or "",
        )

    def _keep_local_copy(
        self, files: Sequence[tuple[str, bytes]], snapshot: str
    ) -> None:
        """Write every file of this publish under the overlay's local copy directory; a failure is named in the tally, never raised."""
        if self._local_root is None:
            return
        try:
            for name, payload in files:
                _write_local_copy(
                    self._local_root,
                    name,
                    payload,
                    exclusive=name == snapshot,
                )
        except OSError as exc:
            self._tally.failed.setdefault(
                "local", f"the local copy could not be written: {exc}"
            )

    def _prune_history(
        self,
        rt: ModelFactoryFloorPublishRuntime,
        overlay: ModelFactoryFloorPublishOverlay,
        now: datetime,
    ) -> tuple[str, ...]:
        """Remove the past-window snapshots the retention allows on each serving host; only files inside the history subdirectory are named.

        A host that cannot be pruned is named in the tally and does not stop the others; the call raises only when no host could be pruned.
        """
        pruned: list[str] = []
        reasons: list[str] = []
        pruned_any = False
        for target in overlay.targets:
            if target.ssh_target in self._unreachable:
                reasons.append(f"{target.ssh_target}: unreachable; nothing was pruned")
                continue
            try:
                pruned.extend(self._prune_target(rt, overlay.for_target(target), now))
                pruned_any = True
            except PublishRefusedError as exc:
                reasons.append(f"{target.ssh_target}: {exc}")
                self._tally.failed.setdefault(target.ssh_target, str(exc))
        if not pruned_any and reasons:
            raise PublishRefusedError("; ".join(reasons))
        return tuple(dict.fromkeys(pruned))

    def _prune_target(
        self,
        rt: ModelFactoryFloorPublishRuntime,
        overlay: ModelFactoryFloorPublishOverlay,
        now: datetime,
    ) -> tuple[str, ...]:
        directory = str(PurePosixPath(overlay.history_root) / rt.history.subdirectory)
        budget = PhaseBudget(
            self._clock,
            rt.cycle.history_prune_budget_seconds,
            "history prune",
            rt.transport.transfer_timeout_seconds,
        )
        listing = self._run(
            ssh_argv(rt, overlay, f"ls -1 -- {shlex.quote(directory)}"),
            None,
            budget.timeout(),
        )
        if listing.returncode != 0:
            if listing.returncode == SSH_CONNECTION_ERROR:
                self._unreachable.add(overlay.ssh_target)
            raise PublishRefusedError(
                f"listing the history failed with exit {listing.returncode}: {_tail(listing.stderr)}; "
                "nothing was pruned"
            )
        found = listing.stdout.decode("utf-8", "replace").split("\n")
        doomed = prunable_snapshots(rt.history, rt.names.data, found, now)
        if not doomed:
            return ()
        paths = " ".join(shlex.quote(f"{directory}/{n}") for n in doomed)
        done = self._run(
            ssh_argv(rt, overlay, f"rm -f -- {paths}"),
            None,
            budget.timeout(),
        )
        if done.returncode != 0:
            raise PublishRefusedError(
                f"pruning the history failed with exit {done.returncode}: {_tail(done.stderr)}"
            )
        return doomed

    @staticmethod
    def _groups(steps: Sequence[ModelPublishStep]) -> list[list[ModelPublishStep]]:
        """The steps in contract order, consecutive ``concurrent`` ones in one group."""
        return group_steps(steps)

    def _run_group(
        self,
        group: Sequence[ModelPublishStep],
        home: Path,
        overlay: ModelFactoryFloorPublishOverlay,
        rt: ModelFactoryFloorPublishRuntime,
        env: Mapping[str, str],
    ) -> list[ModelPublishStepResult]:
        """The results of one group, in the group's order; a group of several runs its steps in threads."""
        if len(group) == 1:
            return [self._step(group[0], home, overlay, rt.plugin_root_env, env)]
        with ThreadPoolExecutor(max_workers=len(group)) as pool:
            futures = [
                pool.submit(self._step, step, home, overlay, rt.plugin_root_env, env)
                for step in group
            ]
            return [future.result() for future in futures]

    def _timing(
        self,
        cycle_start: float,
        phases: Mapping[str, float],
        results: Sequence[ModelPublishStepResult],
        after: Sequence[ModelPublishStepResult],
    ) -> dict[str, Any]:
        """The cycle's measured seconds: the total, each phase, and each step with the slowest named."""
        steps = {r.name: r.seconds for r in (*results, *after)}
        slowest = max(steps, key=lambda name: steps[name], default="")
        return {
            "cycle_seconds": round(self._clock() - cycle_start, 3),
            "phase_seconds": {k: round(v, 3) for k, v in phases.items()},
            "step_seconds": steps,
            "slowest_step": slowest,
        }

    def _step(
        self,
        step: ModelPublishStep,
        home: Path,
        overlay: ModelFactoryFloorPublishOverlay,
        plugin_root_env: str,
        env: Mapping[str, str],
    ) -> ModelPublishStepResult:
        """One step, with the seconds it took."""
        started = self._clock()
        result = self._run_step(step, home, overlay, plugin_root_env, env)
        return result.model_copy(update={"seconds": round(self._clock() - started, 3)})

    def _run_step(
        self,
        step: ModelPublishStep,
        home: Path,
        overlay: ModelFactoryFloorPublishOverlay,
        plugin_root_env: str,
        env: Mapping[str, str],
    ) -> ModelPublishStepResult:
        def fill(values: Sequence[str]) -> list[str]:
            plugin_root = env.get(plugin_root_env, "").strip()
            if not plugin_root and any("{plugin_root}" in v for v in values):
                raise PublishRefusedError(
                    f"{plugin_root_env} is not set (fail fast, no default): export the plugin root"
                )
            return [
                v.replace("{home}", str(home))
                .replace("{bus_lane}", overlay.bus_lane)
                .replace("{plugin_root}", plugin_root)
                for v in values
            ]

        def failed(detail: str, diagnostic: str = "") -> ModelPublishStepResult:
            return ModelPublishStepResult(
                name=step.name, status="failed", detail=detail, diagnostic=diagnostic
            )

        if step.fresh_relpath is not None and step.fresh_seconds is not None:
            try:
                age = time.time() - (home / step.fresh_relpath).stat().st_mtime
            except OSError:
                age = step.fresh_seconds
            if 0 <= age < step.fresh_seconds:
                return ModelPublishStepResult(name=step.name, status="ok")
        try:
            if step.kind == "runner_fleet":
                if step.output_relpath is None:
                    raise ValueError("a fleet source step names an output")
                reading = (
                    HandlerFleetObservationSource(self._run)
                    .handle(
                        ModelFleetObservationSourceRequest(
                            timeout_seconds=step.timeout_seconds
                        ),
                        environ=env,
                    )
                    .document
                )
                write_atomic(home / step.output_relpath, json.dumps(reading).encode())
                if reading["status"] != "read":
                    return failed("producer unread", str(reading["reason"]))
                return ModelPublishStepResult(name=step.name, status="ok")
            if step.kind == "friction_rollup":
                if step.rollup is None or step.output_relpath is None:
                    raise ValueError(
                        "a friction rollup step names its settings and an output"
                    )
                settings = step.rollup
                report = _within_seconds(
                    step.timeout_seconds,
                    lambda: HandlerFrictionRollupSource().handle(
                        settings, now=self._wall_clock(), environ=env
                    ),
                    f"the friction rollup ran past its {step.timeout_seconds} s budget",
                )
                write_atomic(
                    home / step.output_relpath,
                    json.dumps(report, sort_keys=True).encode("utf-8"),
                )
                return ModelPublishStepResult(name=step.name, status="ok")
            if step.kind == "copy":
                if step.source_relpath is None or step.output_relpath is None:
                    raise ValueError("a copy step names a source and an output")
                raw = (home / step.source_relpath).read_bytes()
                document = json.loads(raw)
                for key in step.select:
                    if not isinstance(document, dict) or key not in document:
                        raise ValueError(f"the source has no {'.'.join(step.select)}")
                    document = document[key]
                if step.select:
                    raw = json.dumps(document, sort_keys=True).encode("utf-8")
                write_atomic(home / step.output_relpath, raw)
                return ModelPublishStepResult(name=step.name, status="ok")
            if step.kind == "module":
                if step.module is None:
                    raise ValueError("a module step names a module")
                argv = [sys.executable, "-m", step.module, *fill(step.args)]
            else:
                argv = fill(step.argv)
            done = self._run(argv, None, step.timeout_seconds)
        except (OSError, ValueError, PublishRefusedError) as exc:
            return failed(type(exc).__name__, str(exc))
        if done.returncode != 0:
            return failed(f"exit {done.returncode}", _tail(done.stderr))
        if step.kind == "capture":
            if step.output_relpath is None:
                return failed("ValueError", "a capture step names an output")
            try:
                parsed = json.loads(done.stdout)
            except ValueError:
                return failed("output is not JSON", _tail(done.stdout))
            if not isinstance(parsed, list):
                return failed("output is not a JSON list")
            write_atomic(home / step.output_relpath, done.stdout)
        return ModelPublishStepResult(name=step.name, status="ok")

    @staticmethod
    def _read_record(path: Path) -> bytes:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PublishRefusedError(
                f"the factory floor record {path} is unreadable ({type(exc).__name__}); "
                "run node_factory_floor_effect first"
            ) from exc
        try:
            record = json.loads(data)
        except ValueError as exc:
            raise PublishRefusedError(
                f"the factory floor record {path} is not JSON"
            ) from exc
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("generated_at"), str)
            or not isinstance(record.get("lines"), list)
        ):
            raise PublishRefusedError(
                f"the factory floor record {path} lacks generated_at or lines; refusing to publish it"
            )
        return data

    @staticmethod
    def _read_simple(path: Path) -> bytes | None:
        """A file of the simple floor, or ``None`` when absent or not JSON (the page then draws it as absent, in red)."""
        try:
            data = path.read_bytes()
            json.loads(data)
        except (OSError, ValueError):
            return None
        return data

    @staticmethod
    def _read_delegation(path: Path) -> bytes | None:
        """The delegation counts, or ``None`` when absent or not that shape (the page then draws the card unread)."""
        try:
            data = path.read_bytes()
            record = json.loads(data)
        except (OSError, ValueError):
            return None
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("generated_at"), str)
            or not isinstance(record.get("outcomes"), list)
        ):
            return None
        return data

    @staticmethod
    def _declared_fragments(
        rt: ModelFactoryFloorPublishRuntime, env: Mapping[str, str]
    ) -> tuple[str, ...] | None:
        """The plan texts the factory floor effect's overlay declares as lines, or ``None`` when that is unread."""
        try:
            declared = load_structured_overlay(
                rt.lines_overlay.overlay_id,
                ModelDeclaredLines,
                env,
                file_env=rt.lines_overlay.file_env,
            )
        except NodeOverlayError:
            return None
        return declared.plan_path_contains

    @staticmethod
    def _declared_lines(
        rt: ModelFactoryFloorPublishRuntime, env: Mapping[str, str]
    ) -> dict[str, object]:
        """Which plans the factory floor effect's overlay declares as lines, or why that is unread."""
        tokens = HandlerFactoryFloorPublish._declared_fragments(rt, env)
        if tokens is None:
            return {
                "state": "unread",
                "reason": "the factory floor effect's overlay is missing or invalid",
            }
        return {"state": "read", "plan_path_contains": list(tokens)}

    @staticmethod
    def _flag_undrawn(
        results: list[ModelPublishStepResult],
        rt: ModelFactoryFloorPublishRuntime,
        declared: tuple[str, ...] | None,
        lines: Sequence[object],
    ) -> list[ModelPublishStepResult]:
        """Mark the record step not ok, naming the declared lines the record does not draw."""
        drawn = [line for line in lines if isinstance(line, dict)]
        missing = [
            token
            for token in declared or ()
            if not any(token in str(line.get("plan_path", "")) for line in drawn)
        ]
        if not missing:
            return results
        flagged = ModelPublishStepResult(
            name=rt.record_step,
            status="failed",
            detail="declared lines not drawn: " + ", ".join(missing),
            seconds=next((r.seconds for r in results if r.name == rt.record_step), 0.0),
        )
        if any(r.name == rt.record_step for r in results):
            return [flagged if r.name == rt.record_step else r for r in results]
        return [*results, flagged]

    def _copy(
        self,
        rt: ModelFactoryFloorPublishRuntime,
        overlay: ModelFactoryFloorPublishOverlay,
        name: str,
        payload: bytes,
        budget: PhaseBudget | None = None,
    ) -> None:
        self._tally.record(
            copy_to_server(
                self._run,
                rt,
                overlay,
                name,
                payload,
                budget=budget,
                unreachable=self._unreachable,
            )
        )

    @staticmethod
    def _status_bytes(document: Mapping[str, Any]) -> bytes:
        return json.dumps(document, sort_keys=True).encode("utf-8")

    def _write_final_status(
        self,
        rt: ModelFactoryFloorPublishRuntime,
        overlay: ModelFactoryFloorPublishOverlay,
        document: dict[str, Any],
        timing: Mapping[str, Any],
        after: Sequence[ModelPublishStepResult],
    ) -> None:
        """Serve the status again with the whole cycle's seconds, which the first status could not hold (OMN-22699).

        The first status goes out with the pages, before the copies and the after-publish steps ran. This one names
        every phase, the after-publish steps, the slowest step and the cycle total (taken before this write, which is
        held to ``cycle.final_status_seconds`` inside the bound). It is kept for ``record_tick``.
        """
        document.update(
            {
                "cycle_seconds": timing["cycle_seconds"],
                "phase_seconds": timing["phase_seconds"],
                "after_publish_steps": [
                    r.model_dump(exclude={"diagnostic"}) for r in after
                ],
                "slowest_step": timing["slowest_step"],
                "served_to": list(self._tally.served_to()),
                "failed_targets": dict(self._tally.failed),
            }
        )
        self._served = (rt, overlay, document)
        budget = PhaseBudget(
            self._clock,
            rt.cycle.final_status_seconds,
            "final status",
            rt.transport.transfer_timeout_seconds,
        )
        self._tally.record(
            copy_to_server(
                self._run,
                rt,
                overlay,
                rt.names.status,
                self._status_bytes(document),
                budget=budget,
                unreachable=self._unreachable,
            )
        )

    def record_tick(self, tick_seconds: Mapping[str, float]) -> None:
        """Serve the status once more with the seconds of the whole tick (operator access, the publish, the reports site).

        The cycle total is the publish run alone; the tick also runs the operator-access gate before it and the
        reports site after it, which no step of the cycle times. Nothing is written when no publish was served.
        """
        if self._served is None:
            return
        rt, overlay, document = self._served
        document["tick_seconds"] = {k: round(v, 3) for k, v in tick_seconds.items()}
        budget = PhaseBudget(
            self._clock,
            rt.cycle.final_status_seconds,
            "tick status",
            rt.transport.transfer_timeout_seconds,
        )
        self._tally.record(
            copy_to_server(
                self._run,
                rt,
                overlay,
                rt.names.status,
                self._status_bytes(document),
                budget=budget,
                unreachable=self._unreachable,
            )
        )

    @staticmethod
    def _write_evidence(
        path: Path,
        now: datetime,
        generated_at: str,
        digest: str,
        names: tuple[str, ...],
        timing: Mapping[str, Any] | None = None,
    ) -> None:
        write_atomic(
            path,
            (
                json.dumps(
                    {
                        "published_at": now.astimezone(UTC).isoformat(),
                        "record_generated_at": generated_at,
                        "data_sha256": digest,
                        "published": list(names),
                        **(timing or {}),
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )


__all__ = [
    "NODE_DIR",
    "CopyOutcome",
    "DeliveryTally",
    "HandlerFactoryFloorPublish",
    "PublishRefusedError",
    "copy_to_server",
    "load_runtime",
    "local_copy_root",
    "prunable_snapshots",
    "remote_write_command",
    "run_process",
    "snapshot_name",
]
