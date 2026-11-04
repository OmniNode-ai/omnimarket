# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host configuration of the alarm tick: paths and executables."""

import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .model_drainer_watch_options import ModelDrainerWatchOptions
from .model_watcher_recovery import ModelWatcherRecoveryOptions


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


# The calendar-fired jobs of launchd/morning-workflows and their fire spacing in seconds.
CALENDAR_JOB_INTERVALS: dict[str, int] = {
    "ai.omninode.lab-fill": 1200,
    "ai.omninode.hourly-tick": 3600,
    "ai.omninode.merged-worktree-reaper": 3600,
    "ai.omninode.morning-ground-state": 86400,
    "ai.omninode.morning-friction-sweep": 86400,
}


class ModelSilentFailureAlarmOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_root: Path
    ledger: Path | None = None
    ledger_lock: str | None = None
    ledger_bus_lane: str | None = Field(
        default_factory=lambda: os.environ.get("ONEX_WORK_LEDGER_BUS_LANE")
    )
    # queue-status keeps its trend here, outside every repository.
    trend_file: Path = Field(
        default_factory=lambda: _state_home() / "omni/queue-status/last.json"
    )
    # The PR watcher's state, whose merge records give the merges of the last hour.
    # None means the registry's own copy, {registry_root}/.claude_scratch/pr-watcher/state.json.
    watcher_state: Path | None = None
    watcher_recovery: ModelWatcherRecoveryOptions | None = None
    # The results bounds: merges per hour while non-draft PRs are open (the throughput tick's own
    # --min-merges-per-hour) and lanes placed on a pool host per hour. Zero turns a measure off.
    min_merges_per_hour: int = Field(default=5, ge=0)
    min_lanes_placed_per_hour: int = Field(default=1, ge=0)
    controller_state_dir: Path = Field(
        default_factory=lambda: Path.home() / ".local/state/omni/landing"
    )
    # The alert channel the sibling alarms use: a sourced library and the env file holding its
    # credentials. None means the registry's own alert-channel library.
    alert_lib: Path | None = None
    alert_env_file: Path = Field(
        default_factory=lambda: Path.home() / ".omnibase" / ".env"
    )
    # The command that reads the hook-event table: it prints ``<hostname>|<UTC stamp>``, one line
    # for each host, the stamp being that host's newest hook event. None means this
    # host has none configured, which the tick reports and never treats as a pass.
    hook_events_command: tuple[str, ...] | None = None
    host_aliases: dict[str, str] = Field(default_factory=dict)
    hook_events_timeout_seconds: int = Field(default=120, gt=0)
    # Where the hook-emit drainer is watched and reinstalled. None means this caller
    # configured none, which the tick reports as UNCONFIGURED and never treats as a pass.
    drainer: ModelDrainerWatchOptions | None = None
    launchctl: str = "launchctl"
    uid: int = Field(default_factory=os.getuid)
    job_state_file: Path = Field(
        default_factory=lambda: (
            _state_home() / "omni/silent-failure-alarm/job-exits.json"
        )
    )
    # What was recorded and not yet delivered, and the receipt of each delivery. The
    # ledger row records an alarm; this file records whether the channel took it.
    delivery_state_file: Path = Field(
        default_factory=lambda: (
            _state_home() / "omni/silent-failure-alarm/deliveries.json"
        )
    )
    dispatch_command: tuple[str, ...] | None = None
    dispatch_model: str = "sonnet"
    brief_dir: Path = Field(
        default_factory=lambda: _state_home() / "omni/silent-failure-alarm/briefs"
    )
    job_lanes: dict[str, str] = Field(default_factory=dict)
    # One failed exit opens an episode: a daily job at three runs in a row stayed
    # unseen for three days. The cooldown and the per-tick cap keep that from flooding the lab.
    job_exit_threshold: int = Field(default=1, gt=0)
    job_cooldown_minutes: int = Field(default=120, ge=0)
    job_max_opened: int = Field(default=6, gt=0)
    # Seconds between fires for calendar jobs, which launchd prints no run interval for. An
    # interval job's own "run interval" line is read from launchctl print; a key here overrides it.
    job_intervals: dict[str, int] = Field(
        default_factory=lambda: dict(CALENDAR_JOB_INTERVALS)
    )
    bash: str = "bash"
    python: str = Field(default_factory=lambda: sys.executable)
