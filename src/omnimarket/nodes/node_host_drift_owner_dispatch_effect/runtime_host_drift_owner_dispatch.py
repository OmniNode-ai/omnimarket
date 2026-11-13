# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pass of the host drift owner dispatch on the overlay's backend; the registry-module-tick unit runs ``main`` (OMN-24239)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryBriefPreparer,
    InMemoryDispatchRecord,
    InMemoryDriftOpenItems,
    InMemoryLanePlacer,
    InMemoryLiveLanes,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.runtime_adapters import (
    FileDispatchRecord,
    FileDriftOpenItems,
    LaneBriefPreparer,
    LedgerQueryLiveLanes,
    SubprocessLanePlacer,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.handlers.handler_host_drift_owner_dispatch import (
    HandlerHostDriftOwnerDispatch,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOwnerRequest,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelDriftOwnerOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.protocols import (
    DriftStateUnreadableError,
    LiveLanesUnreadableError,
)

REGISTRY_ROOT_TOKEN = "{registry_root}"
EXIT_PLACEMENT_FAILED = 1
EXIT_UNREADABLE = 3


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _build_inmemory(
    overlay: ModelDriftOwnerOverlay,
    *,
    root: Path,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> HandlerHostDriftOwnerDispatch:
    dispatch = overlay.dispatch
    briefs = InMemoryBriefPreparer()
    return HandlerHostDriftOwnerDispatch(
        overlay=overlay,
        items=InMemoryDriftOpenItems(host="inmemory"),
        live=InMemoryLiveLanes(),
        record=InMemoryDispatchRecord(),
        briefs=briefs,
        runner=InMemoryLanePlacer(briefs),
        runner_path=environ.get(dispatch.runner_env, ""),
        clock=clock,
    )


def _build_runtime(
    overlay: ModelDriftOwnerOverlay,
    *,
    root: Path,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> HandlerHostDriftOwnerDispatch:
    dispatch = overlay.dispatch
    runner_path = environ.get(dispatch.runner_env, "")
    if not runner_path:
        raise DriftStateUnreadableError(
            f"{dispatch.runner_env} is not set: the unit's declaration names the runner"
        )
    briefs = root / dispatch.briefs_dir
    plugin_root = (
        Path(runner_path).resolve().parents[overlay.brief.runner_root_levels - 1]
    )
    return HandlerHostDriftOwnerDispatch(
        overlay=overlay,
        items=FileDriftOpenItems(root / overlay.state_path),
        live=LedgerQueryLiveLanes(
            tuple(
                part.replace(REGISTRY_ROOT_TOKEN, str(root))
                for part in overlay.live_lanes.query_command
            ),
            overlay.live_lanes.lookback_hours,
            overlay.live_lanes.row_limit,
            overlay.live_lanes.timeout_seconds,
            clock,
        ),
        record=FileDispatchRecord(root / overlay.dispatch_record_path),
        briefs=LaneBriefPreparer(
            briefs,
            plugin_root,
            overlay.brief,
            model=dispatch.model,
            ticket=dispatch.ticket,
            environ=environ,
        ),
        runner=SubprocessLanePlacer(dispatch.settle_seconds, briefs),
        runner_path=runner_path,
        clock=clock,
    )


_BACKEND_BUILDERS: dict[str, Callable[..., HandlerHostDriftOwnerDispatch]] = {
    "inmemory": _build_inmemory,
    "runtime": _build_runtime,
}


def build_handler(
    overlay: ModelDriftOwnerOverlay,
    *,
    root: Path,
    environ: Mapping[str, str],
    clock: Callable[[], datetime] = _utc_now,
) -> HandlerHostDriftOwnerDispatch:
    """The handler wired to the overlay's backend; ``root`` is what the overlay's relative paths are under."""
    return _BACKEND_BUILDERS[overlay.backend](
        overlay, root=root, environ=environ, clock=clock
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    overlay = load_overlay()
    try:
        handler = build_handler(overlay, root=args.registry_root, environ=os.environ)
        result = handler.handle(ModelDriftOwnerRequest(dry_run=args.dry_run))
    except (DriftStateUnreadableError, LiveLanesUnreadableError) as exc:
        sys.stdout.write(f"host-drift-owner-dispatch UNREADABLE {exc}\n")
        return EXIT_UNREADABLE
    started = sum(lane.started for lane in result.lanes)
    sys.stdout.write(
        f"host-drift-owner-dispatch host={result.host} items={result.items_read} "
        f"rows={result.rows_rendered} lanes={len(result.lanes)} started={started} "
        f"dry_run={str(args.dry_run).lower()}\n"
    )
    for lane in result.lanes:
        if not lane.brief_checked:
            state = "brief-refused"
        else:
            state = (
                "dry-run" if lane.dry_run else ("started" if lane.started else "failed")
            )
        checked = (
            f"brief=checked sha256={lane.brief_sha256[:12]}"
            if lane.brief_checked
            else "brief=unchecked"
        )
        sys.stdout.write(
            f"LANE {state} {lane.lane} {lane.cause_key} {checked} {lane.detail}\n"
        )
    failed = [
        lane
        for lane in result.lanes
        if not lane.brief_checked or (not lane.dry_run and not lane.started)
    ]
    return EXIT_PLACEMENT_FAILED if failed else 0


if __name__ == "__main__":
    sys.exit(main())
