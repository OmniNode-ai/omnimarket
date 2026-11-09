# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Close out every ended lane on this host, whichever launcher started it (OMN-23375).

A lane's trees and run directory live on the host it ran on, so the host closes them out. A lane has
ended when its context directory holds an end marker older than the grace, when a context directory with
no marker and no live process is older than the orphan age, or when a lane directory has no context
directory at all and is older than the orphan age. Each ended lane's trees go through the lane closeout
handler (unpushed commits to a WIP branch, uncommitted changes to a salvage branch, then removal); the run
directory goes once every tree is gone; one record per lane run says what became of it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.lane_closeout.model_lane_closeout_record import (
    ModelLaneCloseoutRecord,
)
from omnimarket.models.lane_closeout.model_lane_closeout_request import (
    ModelLaneCloseoutRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_request import (
    ModelLaneCloseoutSweepRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_result import (
    ModelLaneCloseoutSweepResult,
)
from omnimarket.models.lane_closeout.model_lane_closeout_tree_outcome import (
    ModelLaneCloseoutTreeOutcome,
)
from omnimarket.models.lane_closeout.model_lane_tree_inventory_request import (
    ModelLaneTreeInventoryRequest,
)
from omnimarket.nodes.node_lane_closeout_effect.handlers.handler_lane_closeout import (
    HandlerLaneCloseout,
)
from omnimarket.nodes.node_lane_tree_inventory_effect.handlers.handler_lane_tree_inventory import (
    HandlerLaneTreeInventory,
)

_RUN_HASH = re.compile(r"-[0-9a-f]{6}$")


@dataclass(frozen=True)
class _Ended:
    lane: str
    run: str
    ended_by: str
    context: Path | None
    workdir: Path | None


def process_cwds() -> list[str] | None:
    """The working directory of every process this user can read; None when the host cannot say."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    found: list[str] = []
    for entry in proc.iterdir():
        if entry.name.isdigit():
            try:
                found.append(os.readlink(entry / "cwd"))
            except OSError:
                continue
    return found


def _pid_alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _inside(cwds: list[str], *paths: Path | None) -> bool:
    for path in paths:
        if path is None:
            continue
        prefix = str(path)
        if any(cwd == prefix or cwd.startswith(prefix + "/") for cwd in cwds):
            return True
    return False


def _newest(*paths: Path) -> float:
    times = []
    for path in paths:
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else 0.0


def _remove_tree(path: Path, root: Path) -> bool:
    """Remove a directory directly under ``root``; a symlink or any other location is never touched."""
    if path.is_symlink() or path.parent != root:
        return not path.exists()
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


class HandlerLaneCloseoutSweep:
    """Find the ended lanes of this host and close each one out."""

    def __init__(
        self, live_cwds: Callable[[], list[str] | None] = process_cwds
    ) -> None:
        self._live_cwds = live_cwds
        self._inventory = HandlerLaneTreeInventory()
        self._closeout = HandlerLaneCloseout()

    def handle(
        self, request: ModelLaneCloseoutSweepRequest
    ) -> ModelLaneCloseoutSweepResult:
        registry_root = Path(request.registry_root)
        state_dir = Path(request.state_dir)
        if not registry_root.is_absolute() or not state_dir.is_absolute():
            raise ValueError(
                f"Expected absolute paths for the registry root and state directory: "
                f"{request.registry_root}, {request.state_dir}"
            )
        settings = request.settings
        runs_root = Path(settings.runs_root).expanduser()
        worktrees = registry_root / settings.worktrees_dir
        records = state_dir / settings.records_dir

        ended, running = self._discover(request, runs_root, worktrees)
        pending = [e for e in ended if not self._complete(records, e.run)]
        live = self._live_cwds()
        closed: list[ModelLaneCloseoutRecord] = []
        skipped_live: list[str] = []
        if not request.read_only:
            for item in pending[: settings.max_lanes_per_sweep]:
                lane_dir = worktrees / item.lane
                if live is None or _inside(live, lane_dir, item.context, item.workdir):
                    skipped_live.append(item.lane)
                    continue
                if item.lane in running:
                    continue
                record = self._close(request, item, runs_root, lane_dir)
                self._write_record(records, record)
                closed.append(record)

        unrecorded = tuple(e.lane for e in ended if not self._recorded(records, e.run))
        return ModelLaneCloseoutSweepResult(
            correlation_id=request.correlation_id,
            host=request.host,
            ended_lanes=len(ended),
            unrecorded=len(unrecorded),
            unrecorded_lanes=unrecorded,
            closed=tuple(closed),
            skipped_running=tuple(sorted(running)),
            skipped_live=tuple(skipped_live),
            read_only=request.read_only,
        )

    # -- reading -----------------------------------------------------------------

    def _lane_of(self, request: ModelLaneCloseoutSweepRequest, run: str) -> str:
        lane = run
        for prefix in request.settings.run_prefixes:
            if lane.startswith(prefix):
                lane = lane[len(prefix) :]
                break
        return _RUN_HASH.sub("", lane)

    def _discover(
        self,
        request: ModelLaneCloseoutSweepRequest,
        runs_root: Path,
        worktrees: Path,
    ) -> tuple[list[_Ended], set[str]]:
        settings = request.settings
        now = request.now
        ended: list[_Ended] = []
        running: set[str] = set()
        known: set[str] = set()
        suffix = settings.ctx_suffix
        contexts = (
            sorted(
                p
                for p in runs_root.iterdir()
                if p.is_dir() and not p.is_symlink() and p.name.endswith(suffix)
            )
            if runs_root.is_dir()
            else []
        )
        for context in contexts:
            run = context.name[: -len(suffix)]
            lane = self._lane_of(request, run)
            known.add(lane)
            if request.lane is not None and lane != request.lane:
                continue
            workdir = runs_root / run
            marker = next(
                (m for m in settings.finished_markers if (context / m).is_file()), None
            )
            if marker is not None:
                ended_at = (context / marker).stat().st_mtime
                if now - ended_at < settings.grace_seconds:
                    continue
                ended.append(_Ended(lane, run, marker, context, workdir))
                continue
            last = _newest(context, context / "run.out", context / "run.pid")
            if now - last >= settings.orphan_age_seconds and not _pid_alive(
                context / "run.pid"
            ):
                ended.append(_Ended(lane, run, "orphan-age", context, workdir))
            else:
                running.add(lane)
        if worktrees.is_dir():
            for lane_dir in sorted(p for p in worktrees.iterdir() if p.is_dir()):
                lane = lane_dir.name
                if lane in known or (request.lane is not None and lane != request.lane):
                    continue
                children = [c for c in lane_dir.iterdir() if c.is_dir()]
                if now - _newest(lane_dir, *children) >= settings.orphan_age_seconds:
                    ended.append(_Ended(lane, lane, "orphan-lane-dir", None, None))
        return ended, running

    def _complete(self, records: Path, run: str) -> bool:
        path = records / f"{run}.json"
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get("complete"))
        except (OSError, ValueError):
            return False

    def _recorded(self, records: Path, run: str) -> bool:
        return (records / f"{run}.json").is_file()

    # -- closing -----------------------------------------------------------------

    def _close(
        self,
        request: ModelLaneCloseoutSweepRequest,
        item: _Ended,
        runs_root: Path,
        lane_dir: Path,
    ) -> ModelLaneCloseoutRecord:
        prefix = request.settings.salvage_branch_prefix
        outcomes: list[ModelLaneCloseoutTreeOutcome] = []
        error = ""
        run_dir_removed = False
        try:
            trees = (
                self._inventory.handle(
                    ModelLaneTreeInventoryRequest(lane_dir=str(lane_dir))
                ).trees
                if lane_dir.is_dir()
                else ()
            )
            for line in trees:
                relative = Path(line.path).relative_to(lane_dir).as_posix()
                tag = re.sub(
                    r"[^A-Za-z0-9._-]+", "-", f"{item.lane}-{item.run}-{relative}"
                ).strip("-.")
                result = self._closeout.handle(
                    ModelLaneCloseoutRequest(
                        correlation_id=request.correlation_id,
                        lane=item.lane,
                        tree=line.path,
                        tag=tag,
                        salvage_branch=f"{prefix}/{tag}",
                    )
                )
                outcomes.append(
                    ModelLaneCloseoutTreeOutcome(
                        tree=result.tree,
                        verdict=result.verdict,
                        pushed_branch=result.wip_push,
                        removed=result.removed,
                        cell=result.cell,
                    )
                )
            if all(o.removed for o in outcomes):
                if lane_dir.is_dir():
                    shutil.rmtree(lane_dir, ignore_errors=True)
                run_dir_removed = True
                for path in (item.workdir, item.context):
                    if path is not None and path.exists():
                        run_dir_removed = (
                            _remove_tree(path, runs_root) and run_dir_removed
                        )
        except (
            Exception
        ) as exc:  # one lane's failure is its record, not the sweep's end
            error = f"{type(exc).__name__}: {exc}"[:300]
        return ModelLaneCloseoutRecord(
            lane=item.lane,
            run=item.run,
            host=request.host,
            closed_at=datetime.fromtimestamp(request.now, UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            ended_by=item.ended_by,
            trees=tuple(outcomes),
            run_dir_removed=run_dir_removed,
            complete=not error and run_dir_removed and not lane_dir.exists(),
            error=error,
        )

    def _write_record(self, records: Path, record: ModelLaneCloseoutRecord) -> None:
        records.mkdir(parents=True, exist_ok=True)
        path = records / f"{record.run}.json"
        held = path.with_suffix(".json.tmp")
        held.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        held.replace(path)
