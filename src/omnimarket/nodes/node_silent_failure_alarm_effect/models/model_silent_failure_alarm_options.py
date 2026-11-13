# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host configuration of the alarm tick: paths and executables (OMN-19998)."""

import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..host_table import HostTableError, read_host_aliases
from .model_closer_liveness_options import ModelCloserLivenessOptions
from .model_drainer_watch_options import ModelDrainerWatchOptions
from .model_quiet_host_repair import ModelHostDrainerCommand


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
    # queue-status keeps its trend here, outside every repository.
    trend_file: Path = Field(
        default_factory=lambda: _state_home() / "omni/queue-status/last.json"
    )
    # The PR watcher's state, whose merge records give the merges of the last hour (OMN-19998).
    # None means the registry's own copy, {registry_root}/.claude_scratch/pr-watcher/state.json.
    watcher_state: Path | None = None
    # The results bounds: merges per hour while non-draft PRs are open (the throughput tick's own
    # --min-merges-per-hour) and lanes placed on a pool host per hour. Zero turns a measure off.
    min_merges_per_hour: int = Field(default=5, ge=0)
    min_lanes_placed_per_hour: int = Field(default=1, ge=0)
    # How long a lane the lab fill handed to a detached send job may read ``pending`` with no CLAIM
    # row before the placement alarm counts it unplaced: the fill's own send cap (sendCapS 900 s).
    lane_send_cap_minutes: int = Field(default=15, ge=1)
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
    # for each host, the stamp being that host's newest hook event (OMN-19998). None means this
    # host has none configured, which the tick reports and never treats as a pass.
    hook_events_command: tuple[str, ...] | None = None
    hook_events_timeout_seconds: int = Field(default=120, gt=0)
    # Where the hook-emit drainer is watched and reinstalled (OMN-19998). None means this caller
    # configured none, which the tick reports as UNCONFIGURED and never treats as a pass.
    drainer: ModelDrainerWatchOptions | None = None
    # Alias/host and remote execution bindings come from deployment data, never from a request.
    # A host id maps to the event label (or labels) its hook events carry. Name a ``host_table``
    # instead of listing them: the aliases are then derived from that table at load time, so the
    # two cannot disagree (OMN-22755). Naming both is refused.
    host_aliases: dict[str, str | tuple[str, ...]] = Field(default_factory=dict)
    host_table: Path | None = None
    # Why ``host_table`` could not be read ('' when it was, or none is named); set by the
    # validator below. Hook events are then unattributable, which the tick reports as UNKNOWN.
    host_table_error: str = ""
    # True reads the lab host table (the one lab host list, however the deployment names it) and leaves
    # out of the quiet-host reading each host it declares unavailable: such a host takes no lane, so a
    # ledger row that names it is not lane work that lost its hook events (OMN-22778). False, the default,
    # skips no host. A table that cannot be read is reported and skips nothing.
    skip_unavailable_hosts: bool = False
    host_drainers: dict[str, ModelHostDrainerCommand] = Field(default_factory=dict)
    capture_repair_state_file: Path = Field(
        default_factory=lambda: (
            _state_home() / "omni/silent-failure-alarm/capture-repairs.json"
        )
    )
    capture_repair_cooldown_minutes: int = Field(default=120, gt=0)
    capture_max_repairs: int = Field(default=8, gt=0)
    repair_disabled_hosts: tuple[str, ...] = ()
    launchctl: str = "launchctl"
    uid: int = Field(default_factory=os.getuid)
    job_state_file: Path = Field(
        default_factory=lambda: (
            _state_home() / "omni/silent-failure-alarm/job-exits.json"
        )
    )
    # What was recorded and not yet delivered, and the receipt of each delivery (OMN-19998). The
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
    # One failed exit opens an episode (OMN-19998): a daily job at three runs in a row stayed
    # unseen for three days. The cooldown and the per-tick cap keep that from flooding the lab.
    job_exit_threshold: int = Field(default=1, gt=0)
    job_cooldown_minutes: int = Field(default=120, ge=0)
    job_max_opened: int = Field(default=6, gt=0)
    # Seconds between fires for calendar jobs, which launchd prints no run interval for. An
    # interval job's own "run interval" line is read from launchctl print; a key here overrides it.
    job_intervals: dict[str, int] = Field(
        default_factory=lambda: dict(CALENDAR_JOB_INTERVALS)
    )
    # Exit codes a job declares as its own finding, by label (OMN-19998): a floor job exiting 1 for
    # floors under is working, so its episode briefs a lane to act on the finding, not the job.
    job_finding_exits: dict[str, tuple[int, ...]] = Field(default_factory=dict)
    # The job-exit reraise bound and the install-failure alarm settings (OMN-21663).
    job_reraise_after_minutes: int = Field(default=240, ge=0)
    install_failures_file: Path | None = None
    install_failure_threshold: int = Field(default=3, gt=0)
    install_failure_window_minutes: int = Field(default=360, gt=0)
    install_failure_reraise_minutes: int = Field(default=240, ge=0)
    install_failure_state_file: Path = Field(
        default_factory=lambda: (
            _state_home() / "omni/silent-failure-alarm/install-failure.json"
        )
    )
    # The ticket closer's staleness check (OMN-20718). None means this caller configured none,
    # which the tick reports as UNCONFIGURED and never treats as a pass.
    closer: ModelCloserLivenessOptions | None = None
    bash: str = "bash"
    python: str = Field(default_factory=lambda: sys.executable)

    @model_validator(mode="before")
    @classmethod
    def derive_host_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not data.get("host_table"):
            return data
        if data.get("host_aliases"):
            raise ValueError("name host_table or host_aliases, not both")
        try:
            aliases: dict[str, str | tuple[str, ...]] = dict(
                read_host_aliases(Path(data["host_table"]))
            )
            error = ""
        except HostTableError as exc:
            aliases, error = {}, str(exc)
        return {**data, "host_aliases": aliases, "host_table_error": error}

    @model_validator(mode="after")
    def declared_hosts(self) -> "ModelSilentFailureAlarmOptions":
        missing = set(self.host_drainers) - set(self.host_aliases)
        if missing:
            raise ValueError(
                f"drainer targets need declared host aliases: {sorted(missing)}"
            )
        if self.drainer is not None and self.drainer.host in self.host_drainers:
            raise ValueError("local drainer host must not also have a remote command")
        return self

    @classmethod
    def from_environment(cls) -> "ModelSilentFailureAlarmOptions":
        config = os.environ.get("ONEX_SILENT_FAILURE_ALARM_CONFIG")
        if config:
            path = Path(config)
            document = json.loads(path.read_text())
            table = document.get("host_table") if isinstance(document, dict) else None
            if table and not Path(table).is_absolute():
                # A relative table path is read from the configuration file's own directory.
                document["host_table"] = str(path.resolve().parent / table)
            return cls.model_validate(document)
        root = os.environ.get("OMNI_HOME")
        if not root:
            raise ValueError(
                "OMNI_HOME or ONEX_SILENT_FAILURE_ALARM_CONFIG is required"
            )
        return cls(registry_root=Path(root))
