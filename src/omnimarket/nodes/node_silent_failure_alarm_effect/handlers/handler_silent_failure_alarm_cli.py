# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the migrated host alarm tick through the canonical contract dispatcher.

The host-local bus lets this alarm notice outages of the remote broker.
Other alarm CLI operations retain their existing owner.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_silent_failure_alarm_compute import host_id

from ..models import (
    ModelDrainerWatchOptions,
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
    ModelSilentFailureAlarmResult,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-silent-failure-alarm")
    parser.add_argument("action", choices=("tick",), nargs="?", default="tick")
    parser.add_argument(
        "--omni-home", dest="registry_root", default=os.environ.get("OMNI_HOME")
    )
    parser.add_argument("--ledger", default=os.environ.get("ONEX_LEDGER_PATH"))
    parser.add_argument("--ledger-lock")
    parser.add_argument("--trend-file", help="queue-status trend file (last.json)")
    parser.add_argument(
        "--watcher-state", help="PR watcher state the merge count is read from"
    )
    parser.add_argument(
        "--min-merges-per-hour", type=int, help="merges an hour owed while PRs are open"
    )
    parser.add_argument(
        "--min-lanes-placed-per-hour",
        type=int,
        help="lanes an hour placed on a pool host",
    )
    parser.add_argument(
        "--controller-state-dir", help="landing controller state directory"
    )
    parser.add_argument("--alert-lib", help="alert-channel.sh to source")
    parser.add_argument("--alert-env-file", help="env file the alert library reads")
    parser.add_argument(
        "--hook-events-command",
        default=os.environ.get("ONEX_HOOK_EVENTS_COMMAND"),
        help="command printing '<hostname>|<UTC stamp>' for the newest hook event of each host",
    )
    parser.add_argument(
        "--job-state-file", help="persisted job-exit streaks and episodes"
    )
    parser.add_argument(
        "--delivery-state-file",
        help="alerts recorded and not yet delivered, and delivery receipts",
    )
    parser.add_argument(
        "--dispatch-command", default=os.environ.get("ONEX_ALARM_DISPATCH_COMMAND")
    )
    parser.add_argument("--launchctl", default="launchctl")
    parser.add_argument(
        "--host-aliases-json",
        default=os.environ.get("ONEX_ALARM_HOST_ALIASES", "{}"),
        help="deployment host id to hook-event hostname map",
    )
    parser.add_argument(
        "--job-intervals",
        default=os.environ.get("ONEX_ALARM_JOB_INTERVALS"),
        help="label=seconds[,label=seconds...] fire spacing of calendar jobs, over the defaults",
    )
    parser.add_argument(
        "--drainer-installer", help="install-hook-emit-drainer.sh to reinstall with"
    )
    parser.add_argument(
        "--drainer-state-dir", help="state root holding the drainer status file"
    )
    parser.add_argument(
        "--host-name", help="the name the drainer alarm row gives this host"
    )
    parser.add_argument(
        "--no-drainer", action="store_true", help="do not watch the hook-emit drainer"
    )
    parser.add_argument(
        "--state-dir", type=Path, help="runtime receipts and the last-run record"
    )
    parser.add_argument("--now", help="UTC timestamp used instead of the current time")
    parser.add_argument(
        "--dry-run", action="store_true", help="append and deliver nothing"
    )
    return parser


def _drainer_options(
    args: argparse.Namespace, registry_root: Path
) -> ModelDrainerWatchOptions:
    """The drainer watch every host tick carries: watched and reinstalled unless switched off."""
    label = socket.gethostname().split(".", 1)[0].lower()
    state = (
        args.drainer_state_dir
        or os.environ.get("ONEX_STATE_DIR")
        or registry_root / ".onex_state"
    )
    installer = (
        args.drainer_installer
        or registry_root / "omniclaude/scripts/install-hook-emit-drainer.sh"
    )
    return ModelDrainerWatchOptions(
        host=args.host_name
        or host_id(label, json.loads(args.host_aliases_json))
        or label,
        state_dir=Path(state),
        installer=Path(installer),
        home=Path.home(),
        registry_root=registry_root,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    replay: datetime | None = None
    if args.now:
        try:
            replay = datetime.strptime(args.now, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError as exc:
            parser.error(f"--now must be a UTC timestamp: {exc}")
    if not args.registry_root:
        parser.error("--omni-home is required when OMNI_HOME is not set")
    paths = {
        "ledger": args.ledger,
        "job_state_file": args.job_state_file,
        "delivery_state_file": args.delivery_state_file,
        "trend_file": args.trend_file,
        "watcher_state": args.watcher_state,
        "controller_state_dir": args.controller_state_dir,
        "alert_lib": args.alert_lib,
        "alert_env_file": args.alert_env_file,
    }
    # An unset path keeps the model's own default; only a stated one overrides it.
    stated: dict[str, Path] = {
        key: Path(value) for key, value in paths.items() if value
    }
    command = (
        tuple(shlex.split(args.hook_events_command))
        if args.hook_events_command
        else None
    )
    intervals: dict[str, int] = {}
    for item in (args.job_intervals or "").split(","):
        if not item.strip():
            continue
        label, _, seconds = item.strip().partition("=")
        if not label or not seconds.isdecimal() or int(seconds) <= 0:
            parser.error(f"--job-intervals entry must be label=seconds: {item!r}")
        intervals[label] = int(seconds)
    registry_root = Path(args.registry_root)
    drainer = None if args.no_drainer else _drainer_options(args, registry_root)
    bounds = {
        key: value
        for key, value in (
            ("min_merges_per_hour", args.min_merges_per_hour),
            ("min_lanes_placed_per_hour", args.min_lanes_placed_per_hour),
        )
        if value is not None
    }
    options = ModelSilentFailureAlarmOptions.model_validate(
        {
            "registry_root": registry_root,
            "drainer": drainer,
            "ledger_lock": args.ledger_lock,
            "hook_events_command": command,
            "host_aliases": json.loads(args.host_aliases_json),
            **bounds,
            "launchctl": args.launchctl,
            "dispatch_command": (
                tuple(shlex.split(args.dispatch_command))
                if args.dispatch_command
                else None
            ),
            **stated,
        }
    )
    if intervals:
        options = options.model_copy(
            update={"job_intervals": {**options.job_intervals, **intervals}}
        )
    result = dispatch_tick(
        options,
        ModelSilentFailureAlarmRequest(now=replay or now(), apply=not args.dry_run),
        args.state_dir,
    )
    for warning in result.warnings:
        sys.stderr.write(warning + "\n")
    if result.summary:
        sys.stdout.write(result.summary + "\n")
    return result.exit_code


def dispatch_tick(
    options: ModelSilentFailureAlarmOptions,
    request: ModelSilentFailureAlarmRequest,
    state_dir: Path | None = None,
) -> ModelSilentFailureAlarmResult:
    """Use the canonical contract dispatcher for the host-local scheduled tick."""
    state = state_dir or options.delivery_state_file.parent / "runtime"
    state.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("ONEX_SILENT_FAILURE_ALARM_CONFIG")
    with TemporaryDirectory(prefix="silent-failure-alarm-") as temporary:
        root = Path(temporary)
        config = root / "options.json"
        config.write_text(options.model_dump_json(), encoding="utf-8")
        payload = root / "request.json"
        payload.write_text(request.model_dump_json(), encoding="utf-8")
        try:
            os.environ["ONEX_SILENT_FAILURE_ALARM_CONFIG"] = str(config)
            runtime = RuntimeLocal(
                Path(__file__).resolve().parents[1] / "contract.yaml",
                input_path=payload,
                state_root=state,
                run_id=request.request_id,
                backend_overrides={"event_bus": "inmemory"},
                timeout=900,
                host_handlers=True,
            )
            runtime.run()
        finally:
            if previous is None:
                os.environ.pop("ONEX_SILENT_FAILURE_ALARM_CONFIG", None)
            else:
                os.environ["ONEX_SILENT_FAILURE_ALARM_CONFIG"] = previous
    result = runtime.handler_result
    if not isinstance(result, ModelSilentFailureAlarmResult):
        return ModelSilentFailureAlarmResult(
            request_id=request.request_id,
            exit_code=2,
            summary="",
            warnings=(
                f"silent_failure_alarm: bus dispatch failed: {runtime.last_error}",
            ),
        )
    (state / "last-run.json").write_text(
        result.model_dump_json() + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    sys.exit(main())
