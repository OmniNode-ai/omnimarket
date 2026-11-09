# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One silent-failure alarm tick: observe, decide through the compute node, then raise (OMN-19998).

Two failures went unseen for hours on 2026-10-09 because nothing read the facts that showed them:
lab-fill's STATUS rows said ``placed`` far below ``dispatched`` for twelve runs, and the session
ticks left no mark for 5 h 39 min. This tick reads those facts every run, from a host timer and
not from a session, and raises the same way the sibling alarms do: a FRICTION row through the
packaged appender, then the shared alert channel.

The ledger deduplicates placement, liveness and quiet-host alarms over two hours; the delivery
state file also keeps each such alarm open until its decision reads the condition healthy, so a
deduped alarm shows as open with its age, a condition held two hours escalates once and its end
writes one resolved row (OMN-19998). Job-exit
alarms instead persist run counters and episodes in a state file: a new episode dispatches a
repair lane, and an episode without a ledger CLAIM after 30 minutes gets one push alert. An
episode opens on a failed exit or a missed interval, once per job and cause inside the cooldown
(OMN-19998). Its brief carries the tail of the job's own logs, because the lane runs on a lab host
that cannot read this host's files, and a STATUS row records whether the dispatch started.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.models.silent_failure_alarm import (
    ModelDrainerRepair,
    ModelDrainerRequest,
    ModelDrainerResult,
    ModelHostActivity,
    ModelJobExitAlarmRequest,
    ModelJobExitEpisode,
    ModelJobExitReading,
    ModelJobExitStreak,
    ModelLaneRefusal,
    ModelPlacementRun,
    ModelPlacementShareRequest,
    ModelQuietHostRequest,
    ModelThroughputResultsRequest,
    ModelTickLivenessRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_compute import (
    HandlerDrainerAlarm,
    HandlerJobExitAlarm,
    HandlerPlacementShareAlarm,
    HandlerQuietHostAlarm,
    HandlerThroughputResultsAlarm,
    HandlerTickLivenessAlarm,
    host_id,
)

from ..host_table import read_unavailable_hosts
from ..models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
    ModelSilentFailureAlarmResult,
)
from ..protocols import ProtocolDrainerWatch
from .alarm_io import CONTROLLER_STALE, append_row, one_line, read_controller
from .command_drainer_watch import CommandDrainerWatchProbe
from .drainer_watch import DrainerWatchProbe
from .lab_fill_lane_reading import LabFillRunRow, parse_lanes, settle_run
from .quiet_host_repair import QuietHostRepairPlanner

LANE = "silent-failure-alarm"

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Runs older than this say nothing about whether lab-fill places lanes now.
LOOKBACK = timedelta(hours=6)
DEDUPE = timedelta(hours=2)
CONTROLLER_LIVE = CONTROLLER_STALE
# An alert the channel has refused for this long is given up on, with a warning: the ledger row that
# recorded it is still there, and a condition that persists is raised afresh past the dedupe window.
DELIVERY_ABANDON = timedelta(hours=24)
# A condition held this long raises one escalation, and never another until it ends.
ESCALATE_AFTER = timedelta(hours=2)
_SLACK_TS = re.compile(r"^\d{9,}\.\d{3,}$")
PLACEMENT = "placement-share"
LIVENESS = "tick-liveness"
QUIET = "quiet-host"
RESULTS = "throughput-results"
HOUR = timedelta(hours=1)
DRAINER = "drainer-dead"
JOB_EXIT = "job-exit"
_EPISODE = re.compile(r"ep-\d{8}T\d{4}Z-[a-z0-9.-]+")
_JOB_RUNS = re.compile(r"^\s*runs\s*=\s*(\d+)\s*$", re.MULTILINE)
_JOB_EXIT = re.compile(r"^\s*last exit code\s*=\s*(-?\d+)\s*$", re.MULTILINE)
_JOB_INTERVAL = re.compile(r"^\s*run interval\s*=\s*(\d+) seconds\s*$", re.MULTILINE)
_JOB_STDOUT = re.compile(r"^\s*stdout path\s*=\s*(\S.*?)\s*$", re.MULTILINE)
_JOB_STDERR = re.compile(r"^\s*stderr path\s*=\s*(\S.*?)\s*$", re.MULTILINE)
_LOG_TAIL_LINES = 60
_HOST_CELL = re.compile(r"^h\d+$")
# A host timer (host-drift) writes drift-pass rows that name its host. It runs no session, so it
# sends no hook event; its rows are not lanes and prove nothing about capture (OMN-22778).
_TIMER_ACTOR_PREFIX = "timer:"
_RUN_KEY = re.compile(r"^\d{4}-(\d\d)-(\d\d)T(\d\d)(\d\d)Z$")
_LANE_RUN_KEY = re.compile(r"^lab-fill-.+-(\d{8})$")
_STATUS = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ALERT_COMMAND = 'set -a; [ -r "$1" ] && . "$1" >/dev/null 2>&1; set +a; . "$2" && alert_channel_send "$3" "$4"'


def _parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _skip_none() -> tuple[frozenset[str], str]:
    """No host is skipped: the options did not ask for the lab host table."""
    return frozenset(), ""


def _stamp(value: datetime) -> str:
    return value.strftime(STAMP_FORMAT)


def watcher_state_path(options: ModelSilentFailureAlarmOptions) -> Path:
    """The PR watcher state the merge count is read from: the stated one, or the registry's."""
    return (
        options.watcher_state
        or options.registry_root / ".claude_scratch/pr-watcher/state.json"
    )


def _open_minutes(now: datetime, entry: dict[str, object]) -> int:
    started = _parse_stamp(str(entry.get("started") or ""))
    return 0 if started is None else max(0, int((now - started).total_seconds() // 60))


def _entry_hosts(entry: dict[str, object] | None) -> set[str]:
    raw = None if entry is None else entry.get("hosts")
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def _covered_hosts(
    entry: dict[str, object] | None, newest_row: set[str] | None
) -> set[str]:
    """The hosts an open alarm already raised on: its recorded hosts that the newest row names.

    The recorded set alone is not the record: it only ever grew over an episode that never ended, so
    a host that recovered, or one the alarm could not attribute before, stayed "raised" for days
    with no row naming it (OMN-22777). The newest row on the ledger is what was raised last; when
    the ledger holds none (it rolled), the recorded set stands.
    """
    recorded = _entry_hosts(entry)
    return recorded if newest_row is None else recorded & newest_row


def _episode_id(name: str, entry: dict[str, object]) -> str:
    started = _parse_stamp(str(entry.get("started") or ""))
    return f"al-{started.strftime('%Y%m%dT%H%MZ') if started else 'unknown'}-{name}"


def _escalation_cells(cells: str) -> str:
    """The FRICTION cells with an escalation cell ahead of the closing text cell."""
    parts = cells.split(" | ")
    parts.insert(len(parts) - 1, "escalation=held-past-2h")
    return " | ".join(parts)


def _minutes(now: datetime, at: datetime | None) -> str:
    return "absent" if at is None else f"{(now - at).total_seconds() / 60:.0f}m"


def _log_tails(episode: ModelJobExitEpisode, uid: int) -> str:
    """The last lines of the job's stdout and stderr logs, quoted for a lane on another host."""
    parts: list[str] = []
    for name, raw in (("stdout", episode.stdout_path), ("stderr", episode.stderr_path)):
        if not raw:
            continue
        path = Path(raw)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            parts.append(
                f"{name} ({path}) could not be read: {one_line(exc, 'unreadable')}\n"
            )
            continue
        tail = "\n".join(lines[-_LOG_TAIL_LINES:])
        parts.append(
            f"{name} ({path}), last {_LOG_TAIL_LINES} lines:\n\n```text\n{tail}\n```\n"
        )
    if not parts:
        return (
            f"launchd names no stdout or stderr path; read `launchctl print "
            f"gui/{uid}/{episode.label}` for the job's paths.\n"
        )
    return "\n".join(parts)


class HandlerSilentFailureAlarm:
    """Run one alarm tick against the host this handler was built for."""

    def __init__(
        self,
        options: ModelSilentFailureAlarmOptions | None = None,
        *,
        drainer_watch: ProtocolDrainerWatch | None = None,
        host_watches: Mapping[str, ProtocolDrainerWatch] | None = None,
        unavailable_hosts: Callable[[], tuple[frozenset[str], str]] | None = None,
    ) -> None:
        options = options or ModelSilentFailureAlarmOptions.from_environment()
        self._options = options
        # The hosts the lab host table declares unavailable take no lane, so the quiet-host reading skips
        # them when the options ask (OMN-22778). Injected so a test supplies the table; the default reads
        # the deployment's.
        self._unavailable_hosts: Callable[[], tuple[frozenset[str], str]] = (
            unavailable_hosts
            or (
                read_unavailable_hosts if options.skip_unavailable_hosts else _skip_none
            )
        )
        self._drainer = HandlerDrainerAlarm()
        self._watch = drainer_watch or (
            DrainerWatchProbe(options.drainer) if options.drainer is not None else None
        )
        self._placement = HandlerPlacementShareAlarm()
        self._liveness = HandlerTickLivenessAlarm()
        self._quiet = HandlerQuietHostAlarm()
        self._results = HandlerThroughputResultsAlarm()
        self._job_exit = HandlerJobExitAlarm()
        watches: dict[str, ProtocolDrainerWatch] = {
            host: CommandDrainerWatchProbe(host, target)
            for host, target in options.host_drainers.items()
        }
        if options.drainer is not None and self._watch is not None:
            watches[options.drainer.host] = self._watch
        if host_watches is not None:
            watches.update(host_watches)
        for host in options.repair_disabled_hosts:
            watches.pop(host, None)
        self._capture = QuietHostRepairPlanner(
            watches=watches,
            read_events=self._read_hook_events,
            state_file=options.capture_repair_state_file,
            cooldown_minutes=options.capture_repair_cooldown_minutes,
            max_repairs=options.capture_max_repairs,
        )

    def handle(
        self, request: ModelSilentFailureAlarmRequest
    ) -> ModelSilentFailureAlarmResult:
        """Observe the host, decide the six alarms, and raise new findings."""
        if request.operation != "tick":
            return self._drainer_command(request)
        options = self._options
        now = request.now or datetime.now(UTC)
        ledger = (
            options.ledger
            or options.registry_root / "docs/tracking/ROLLING_WORK_LEDGER.md"
        )
        try:
            text = ledger.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            detail = one_line(exc, "read failed")
            return ModelSilentFailureAlarmResult(
                request_id=request.request_id,
                exit_code=2,
                summary="",
                warnings=(f"silent_failure_alarm: ledger could not be read: {detail}",),
            )
        (
            runs,
            refusals,
            raised,
            raised_hosts,
            newest_row_hosts,
            resolved_episodes,
        ) = self._read_ledger(text, now)
        pending, delivered_log, opened, delivery_problem = self._read_deliveries()
        host_stamps = self._read_host_lane_stamps(text, now)
        host_claims = self._read_host_lane_stamps(text, now, kinds=("CLAIM",))

        share = self._placement.handle(
            ModelPlacementShareRequest(now=now, runs=runs, refusals=refusals)
        )
        controller = read_controller(options.controller_state_dir)
        live = (
            None
            if controller is None
            else now - controller.last_activity() <= CONTROLLER_LIVE
        )
        trend_at = self._read_trend(options.trend_file)
        # A watcher snapshot with no readable refresh stamp is the results decision's finding
        # (unreadable state); liveness judges the age of a stamp it could read (OMN-19998).
        watcher_at = self._read_watcher_tick(watcher_state_path(options))
        ticks = self._liveness.handle(
            ModelTickLivenessRequest(
                now=now,
                trend_at=trend_at,
                controller_live=live,
                watcher_read=watcher_at is not None,
                watcher_at=watcher_at,
            )
        )
        merges, open_prs = self._read_merges(watcher_state_path(options), now)
        placed = sum(
            1
            for stamps in host_claims.values()
            for at in stamps
            if now - HOUR < at <= now
        )
        results = self._results.handle(
            ModelThroughputResultsRequest(
                now=now,
                merges_last_hour=merges,
                open_prs=open_prs,
                lanes_placed_last_hour=placed,
                min_merges_per_hour=options.min_merges_per_hour,
                min_lanes_placed_per_hour=options.min_lanes_placed_per_hour,
            )
        )

        warnings: list[str] = []
        if delivery_problem:
            warnings.append(
                f"silent_failure_alarm: delivery state could not be read: {delivery_problem}"
            )
        unavailable, table_problem = self._unavailable_hosts()
        if table_problem:
            warnings.append(
                f"silent_failure_alarm: lab host table could not be read, no host is skipped as "
                f"unavailable: {table_problem}"
            )
        events, event_problem = self._read_hook_events()
        if event_problem:
            warnings.append(
                f"silent_failure_alarm: hook events could not be read: {event_problem}"
            )
        capture = self._capture.handle(
            ModelQuietHostRequest(
                now=now,
                events_readable=not event_problem,
                hosts=tuple(
                    ModelHostActivity(
                        host=host, lane_stamps=stamps, last_event_at=events.get(host)
                    )
                    for host, stamps in sorted(host_stamps.items())
                    if host not in unavailable
                ),
            ),
            apply=request.apply,
        )
        quiet = capture.decision
        warnings.extend(capture.warnings)
        quiet_state = (
            "UNCONFIGURED" if self._options.hook_events_command is None else quiet.state
        )
        drainer_state, drainer_repair, drainer_decision = self._watch_drainer(
            now,
            request.apply and self._drainer_host() not in quiet.quiet_hosts,
            warnings,
        )

        alarms: list[str] = []
        rows: list[str] = []
        friction = "not-needed"
        verdicts = {
            PLACEMENT: "ok",
            LIVENESS: ticks.state,
            QUIET: quiet_state,
            RESULTS: results.state,
            DRAINER: drainer_state,
        }
        raising = (
            (
                PLACEMENT,
                share.alarm,
                share.friction_cells,
                share.alert_text,
                "placement_share",
            ),
            (
                LIVENESS,
                ticks.alarm,
                ticks.friction_cells,
                ticks.alert_text,
                "tick_liveness",
            ),
            (QUIET, quiet.alarm, quiet.friction_cells, quiet.alert_text, "quiet_host"),
            (
                RESULTS,
                results.alarm,
                results.friction_cells,
                results.alert_text,
                "throughput_results",
            ),
            (
                DRAINER,
                drainer_decision.alarm,
                drainer_decision.friction_cells,
                drainer_decision.alert_text,
                "drainer_dead",
            ),
        )
        alarm_hosts = {QUIET: set(quiet.quiet_hosts), DRAINER: {self._drainer_host()}}
        # A condition that ended is resolved only when its own decision reads it healthy; an
        # unknown or skipped read leaves the alarm open.
        healthy = {
            PLACEMENT: True,
            LIVENESS: ticks.state == "LIVE",
            QUIET: quiet_state == "LIVE",
            RESULTS: results.state == "PRODUCING",
            DRAINER: drainer_state == "LIVE",
        }
        open_notes: list[str] = []
        resolved: list[str] = []
        raised_now: set[str] = set()
        for name, alarm, cells, alert, category in raising:
            current = opened.get(name)
            if not alarm:
                if current is None:
                    continue
                minutes = _open_minutes(now, current)
                if not healthy[name]:
                    open_notes.append(f"{name}(open {minutes}m,unread)")
                    continue
                episode_id = _episode_id(name, current)
                if episode_id in resolved_episodes:
                    # The ledger already holds this episode's resolved row: one episode, one row
                    # (OMN-22868). Only the stale open entry is dropped.
                    if request.apply:
                        del opened[name]
                    continue
                resolved.append(name)
                row = (
                    f"{_stamp(now)} | STATUS | lane=silent-failure-alarm | ticket=OMN-19998 | "
                    f"actor=launchd:silent-failure-alarm | episode={episode_id} | "
                    f"resolved={name} | open_minutes={minutes} | "
                    f"{name} alarm resolved after {minutes} minutes open"
                )
                rows.append(row)
                if request.apply:
                    appended, detail = append_row(
                        options.python,
                        options.ledger_lock,
                        ledger,
                        row,
                        registry_root=options.registry_root,
                    )
                    if appended:
                        del opened[name]
                    else:
                        warnings.append(
                            f"silent_failure_alarm: {name} resolved row append failed: {detail}"
                        )
                continue
            hosts = alarm_hosts.get(name)
            if hosts is not None:
                in_window = hosts <= raised_hosts.get(name, set())
            else:
                in_window = raised.get(name, now - DEDUPE * 2) > now - DEDUPE
            if current is None and in_window:
                # Raised before this state file held it: adopt the ledger's row as the open alarm.
                current = {
                    "started": _stamp(raised.get(name, now)),
                    "hosts": sorted(hosts or ()),
                    "escalated": None,
                }
                opened[name] = current
            escalating = False
            covered = _covered_hosts(current, newest_row_hosts.get(name))
            if current is not None and not (hosts or set()) - covered:
                # Still the same condition: it is open, and only a first two-hour hold raises again.
                held = _open_minutes(now, current)
                if hosts is not None:
                    # A host that read healthy since is no longer covered: it raises a row if it goes
                    # quiet again, whatever the episode's age (OMN-22777).
                    current["hosts"] = sorted(hosts)
                if (
                    current.get("escalated") is not None
                    or held < ESCALATE_AFTER.total_seconds() / 60
                ):
                    verdicts[name] = "deduped"
                    friction = "deduped"
                    open_notes.append(f"{name}(open {held}m)")
                    continue
                escalating = True
                cells = _escalation_cells(cells)
                alert = f"ESCALATION, held {held} minutes: {alert}"
            verdicts[name] = {
                PLACEMENT: "ALARM",
                LIVENESS: "STALE",
                QUIET: "QUIET",
                RESULTS: results.state,
                DRAINER: "DEAD",
            }[name]
            row = f"{_stamp(now)} | {cells}"
            alarms.append(name)
            rows.append(row)
            if not request.apply:
                friction = "dry-run"
                continue
            appended, detail = append_row(
                options.python,
                options.ledger_lock,
                ledger,
                row,
                registry_root=options.registry_root,
            )
            friction = "appended" if appended else "append-failed"
            if not appended:
                warnings.append(
                    f"silent_failure_alarm: {name} ledger append failed: {detail}"
                )
            delivered, detail, ref = self._deliver(f"{category}_alarm", alert)
            if not delivered:
                warnings.append(
                    f"silent_failure_alarm: {name} alert not delivered: {detail}"
                )
            if appended:
                # The row is the record; this is the receipt. Recorded and not delivered stays
                # pending, and a later tick sends it again without a second row.
                raised_now.add(name)
                opened[name] = {
                    "started": current["started"]
                    if current is not None
                    else _stamp(now),
                    "hosts": sorted(hosts if hosts is not None else set()),
                    "escalated": _stamp(now)
                    if escalating
                    else (current or {}).get("escalated"),
                }
                if delivered:
                    pending.pop(name, None)
                    delivered_log[name] = {"at": _stamp(now), "message_ref": ref}
                else:
                    pending[name] = {
                        "category": f"{category}_alarm",
                        "alert": alert,
                        "recorded_at": _stamp(now),
                        "attempts": 1,
                    }
        retried = (
            self._retry_pending(now, pending, delivered_log, raised_now, warnings)
            if request.apply
            else ()
        )

        readings, readable, unconfigured, job_problem = self._read_jobs()
        if job_problem:
            warnings.append(
                f"silent_failure_alarm: job table could not be read: {job_problem}"
            )
        previous, state_problem = self._read_job_state()
        if state_problem:
            warnings.append(
                f"silent_failure_alarm: job-exit state could not be read: {state_problem}"
            )
        cited: set[str] = set()
        for line in text.splitlines():
            parsed = parse_row(line)
            if parsed is not None and parsed.row_type == "CLAIM":
                for episode_id in _EPISODE.findall(line):
                    cited.add(episode_id)
                    cited.add(episode_id.removesuffix("-brief.md"))
        jobs = self._job_exit.handle(
            ModelJobExitAlarmRequest(
                now=now,
                readable=readable,
                readings=readings,
                previous=previous,
                cited_episodes=tuple(sorted(cited)),
                threshold=options.job_exit_threshold,
                lanes=options.job_lanes,
                intervals=options.job_intervals,
                finding_exits=options.job_finding_exits,
                cooldown_minutes=options.job_cooldown_minutes,
                max_opened=options.job_max_opened,
                dispatch_configured=options.dispatch_command is not None,
            )
        )
        job_streaks = {item.label: item for item in jobs.streaks}
        if jobs.opened:
            alarms.append(JOB_EXIT)
        for episode, cells in zip(jobs.opened, jobs.friction_cells, strict=True):
            row = f"{_stamp(now)} | {cells}"
            rows.append(row)
            if not request.apply:
                friction = "dry-run"
                continue
            appended, detail = append_row(
                options.python,
                options.ledger_lock,
                ledger,
                row,
                registry_root=options.registry_root,
            )
            friction = "appended" if appended else "append-failed"
            if not appended:
                warnings.append(
                    f"silent_failure_alarm: job-exit ledger append failed: {detail}"
                )
            dispatched, detail = self._dispatch_job(episode)
            if dispatched:
                job_streaks[episode.label] = job_streaks[episode.label].model_copy(
                    update={"dispatched": True}
                )
            else:
                warnings.append(f"silent_failure_alarm: job-exit {detail}")
            # The dispatch is an action, so it leaves its own row: the ledger's bus dual write
            # publishes it as an event, and a lane that never started reads as dispatched=no.
            status = (
                f"{_stamp(now)} | STATUS | lane=silent-failure-alarm | ticket=OMN-19998 | "
                f"actor=launchd:silent-failure-alarm | episode={episode.episode} | "
                f"job={episode.label} | cause={episode.cause} | lane_kind={episode.lane_kind} | "
                f"dispatched={'yes' if dispatched else 'no'} | "
                + (
                    f"fix lane dispatched through the remote-lane runner for {episode.label}"
                    if dispatched
                    else f"fix lane not dispatched for {episode.label}: {detail}"
                )
            )
            rows.append(status)
            appended, detail = append_row(
                options.python,
                options.ledger_lock,
                ledger,
                status,
                registry_root=options.registry_root,
            )
            if not appended:
                warnings.append(
                    f"silent_failure_alarm: job-exit status append failed: {detail}"
                )
        if request.apply:
            for alert in jobs.alert_text:
                delivered, detail, _ = self._deliver("job_exit_alarm", alert)
                if not delivered:
                    warnings.append(
                        f"silent_failure_alarm: job-exit alert not delivered: {detail}"
                    )
            if not unconfigured:
                state_problem = self._write_job_state(tuple(job_streaks.values()))
                if state_problem:
                    warnings.append(
                        "silent_failure_alarm: job-exit state could not be written: "
                        f"{state_problem}"
                    )
            delivery_problem = self._write_deliveries(pending, delivered_log, opened)
            if delivery_problem:
                warnings.append(
                    f"silent_failure_alarm: delivery state could not be written: {delivery_problem}"
                )
        job_state = "UNCONFIGURED" if unconfigured else jobs.state
        summary = (
            f"silent_failure_alarm: {_stamp(now)} placement={verdicts[PLACEMENT]} "
            f"reason={share.reason or 'none'} tick_liveness={verdicts[LIVENESS]} "
            f"quiet_host={verdicts[QUIET]} results={verdicts[RESULTS]} "
            f"merges_1h={'unread' if merges is None else merges} placed_1h={placed} "
            f"drainer={verdicts[DRAINER]} drainer_repair={drainer_repair} "
            f"trend_age={_minutes(now, trend_at)} "
            f"job_exit={job_state} failing={','.join(jobs.failing) or 'none'} "
            f"episodes_opened={len(jobs.opened)} pushed={len(jobs.push)} "
            f"held={','.join(jobs.suppressed) or 'none'} "
            f"alarms={','.join([*alarms, *open_notes]) or 'none'} "
            f"resolved={','.join(resolved) or 'none'} friction={friction} "
            f"retried={','.join(retried) or 'none'} "
            f"undelivered={','.join(sorted(pending)) or 'none'}"
        )
        return ModelSilentFailureAlarmResult(
            request_id=request.request_id,
            exit_code=3 if warnings else 0,
            summary=summary,
            warnings=tuple(warnings),
            alarms=tuple(alarms),
            rows=tuple(rows),
            capture_repairs=capture.repairs,
        )

    def _drainer_command(
        self, request: ModelSilentFailureAlarmRequest
    ) -> ModelSilentFailureAlarmResult:
        options = self._options.drainer
        if options is None or self._watch is None:
            return ModelSilentFailureAlarmResult(
                request_id=request.request_id,
                exit_code=2,
                summary="drainer unconfigured",
                warnings=("host drainer is not configured",),
            )
        before = self._watch.observe()
        repair = None
        if request.operation == "drainer-repair":
            verdict = self._drainer.handle(
                ModelDrainerRequest(
                    now=datetime.now(UTC),
                    host=options.host,
                    before=before,
                    stale_minutes=options.stale_minutes,
                )
            )
            if (
                not request.apply
                or before.job_state == "unknown"
                or not verdict.alarm
                or not verdict.repair_safe
                or options.host in self._options.repair_disabled_hosts
            ):
                return ModelSilentFailureAlarmResult(
                    request_id=request.request_id,
                    host=options.host,
                    exit_code=3,
                    summary="repair refused after host observation",
                    observation=before,
                )
            repair = self._watch.repair()
        return ModelSilentFailureAlarmResult(
            request_id=request.request_id,
            host=options.host,
            exit_code=0 if repair is None or repair.succeeded else 3,
            summary="host drainer observation",
            observation=before,
            repair=repair,
        )

    def _read_jobs(self) -> tuple[tuple[ModelJobExitReading, ...], bool, bool, str]:
        """Read launchd counters; an unavailable table can never be an OK verdict."""
        options = self._options
        # launchd is a macOS service. Other hosts may supply an explicit probe executable.
        if options.launchctl == "launchctl" and sys.platform != "darwin":
            return (), False, True, ""
        try:
            done = subprocess.run(
                [options.launchctl, "list"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except FileNotFoundError:
            return (), False, True, ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (), False, False, one_line(exc, "launchctl list could not run")
        if done.returncode != 0:
            return (
                (),
                False,
                False,
                (
                    f"launchctl list exited {done.returncode}: "
                    f"{one_line(done.stderr[-200:], 'no stderr')}"
                ),
            )
        labels: set[str] = set()
        for line in done.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2].startswith("ai.omninode."):
                labels.add(fields[2])
        readings: list[ModelJobExitReading] = []
        for label in sorted(labels):
            runs: int | None = None
            last_exit: int | None = None
            interval: int | None = None
            stdout_path: str | None = None
            stderr_path: str | None = None
            try:
                job = subprocess.run(
                    [options.launchctl, "print", f"gui/{options.uid}/{label}"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                if job.returncode == 0:
                    run_match = _JOB_RUNS.search(job.stdout)
                    exit_match = _JOB_EXIT.search(job.stdout)
                    runs = int(run_match[1]) if run_match else None
                    last_exit = int(exit_match[1]) if exit_match else None
                    interval_match = _JOB_INTERVAL.search(job.stdout)
                    interval = int(interval_match[1]) if interval_match else None
                    out_match = _JOB_STDOUT.search(job.stdout)
                    err_match = _JOB_STDERR.search(job.stdout)
                    stdout_path = out_match[1] if out_match else None
                    stderr_path = err_match[1] if err_match else None
            except (OSError, subprocess.TimeoutExpired):
                pass
            readings.append(
                ModelJobExitReading(
                    label=label,
                    runs=runs,
                    last_exit=last_exit,
                    interval_s=interval or None,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            )
        return tuple(readings), True, False, ""

    def _retry_pending(
        self,
        now: datetime,
        pending: dict[str, dict[str, object]],
        delivered_log: dict[str, dict[str, object]],
        raised_now: set[str],
        warnings: list[str],
    ) -> tuple[str, ...]:
        """Send again each alarm that was recorded and not delivered, and say what happened.

        An alarm raised in this very tick was just tried, so it is left for the next one. An alert
        the channel has refused for ``DELIVERY_ABANDON`` is dropped with a warning.
        """
        retried: list[str] = []
        for name in sorted(pending):
            if name in raised_now:
                continue
            item = pending[name]
            recorded = _parse_stamp(str(item.get("recorded_at") or ""))
            if recorded is not None and now - recorded > DELIVERY_ABANDON:
                del pending[name]
                warnings.append(
                    f"silent_failure_alarm: {name} alert abandoned after "
                    f"{DELIVERY_ABANDON.total_seconds() / 3600:.0f}h undelivered"
                )
                continue
            delivered, detail, ref = self._deliver(
                str(item.get("category") or f"{name}_alarm"),
                str(item.get("alert") or ""),
            )
            if delivered:
                del pending[name]
                delivered_log[name] = {
                    "at": _stamp(now),
                    "message_ref": ref,
                    "retried": True,
                }
                retried.append(name)
                continue
            attempts = item.get("attempts")
            item["attempts"] = (attempts if isinstance(attempts, int) else 1) + 1
            warnings.append(
                f"silent_failure_alarm: {name} alert still undelivered: {detail}"
            )
        return tuple(retried)

    def _read_deliveries(
        self,
    ) -> tuple[
        dict[str, dict[str, object]],
        dict[str, dict[str, object]],
        dict[str, dict[str, object]],
        str,
    ]:
        """(pending alerts, delivery receipts, open alarms, why the file could not be read)."""
        try:
            data = json.loads(
                self._options.delivery_state_file.read_text(encoding="utf-8")
            )
            pending, delivered = data["pending"], data["delivered"]
            opened = data.get("open", {})
            if not all(isinstance(item, dict) for item in (pending, delivered, opened)):
                raise ValueError("pending, delivered and open must be objects")
            return dict(pending), dict(delivered), dict(opened), ""
        except FileNotFoundError:
            return {}, {}, {}, ""
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {}, {}, {}, one_line(exc, "invalid delivery state")

    def _write_deliveries(
        self,
        pending: dict[str, dict[str, object]],
        delivered: dict[str, dict[str, object]],
        opened: dict[str, dict[str, object]],
    ) -> str:
        path = self._options.delivery_state_file
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    {"pending": pending, "delivered": delivered, "open": opened},
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
            temporary.replace(path)
        except OSError as exc:
            return one_line(exc, "delivery state write failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ""

    def _read_job_state(self) -> tuple[tuple[ModelJobExitStreak, ...], str]:
        try:
            data = json.loads(self._options.job_state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("streaks"), list):
                raise ValueError("expected an object with a streaks list")
            return tuple(
                ModelJobExitStreak.model_validate(item) for item in data["streaks"]
            ), ""
        except FileNotFoundError:
            return (), ""
        except (OSError, ValueError) as exc:
            return (), one_line(exc, "invalid job-exit state")

    def _write_job_state(self, streaks: tuple[ModelJobExitStreak, ...]) -> str:
        path = self._options.job_state_file
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    {"streaks": [item.model_dump(mode="json") for item in streaks]},
                    handle,
                )
                handle.write("\n")
            temporary.replace(path)
        except OSError as exc:
            return one_line(exc, "job-exit state write failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ""

    def _dispatch_job(self, episode: ModelJobExitEpisode) -> tuple[bool, str]:
        """Write the episode's brief and start one detached remote-lane run for it."""
        options = self._options
        try:
            options.brief_dir.mkdir(parents=True, exist_ok=True)
            brief = options.brief_dir / f"{episode.episode}-brief.md"
            what = (
                "has stopped running on its interval"
                if episode.cause == "missed-interval"
                else "exits nonzero"
            )
            task = (
                f"{episode.label} is working: exit {episode.last_exit} is its declared "
                "finding, not a crash. Read what its log reports and act on that condition "
                "(for a floor job, the floors it names as under); never change the job so "
                "the finding exits 0"
                if episode.cause == "finding-exit"
                else f"Find why {episode.label} {what} and fix the cause through a pull request"
            )
            brief.write_text(
                f"Job-exit alarm\n\nEpisode: {episode.episode}\nLabel: {episode.label}\n"
                f"Cause: {episode.cause}\n"
                f"streak: {episode.streak}\nlast exit: {episode.last_exit}\n\n"
                f"{_log_tails(episode, options.uid)}\n"
                f"{task}; "
                f"cite episode={episode.episode} in your CLAIM. A cause that lives only on the "
                f"operator host (a credential, a login, a missing file) is reported in your final "
                f"message with this log as evidence, not worked around.\n",
                encoding="utf-8",
            )
            if options.dispatch_command is None:
                return (
                    False,
                    "dispatch unconfigured (ONEX_ALARM_DISPATCH_COMMAND unset)",
                )
            short = re.sub(r"[^a-z0-9-]", "-", episode.episode[-24:].lower())
            log = options.brief_dir / f"{episode.episode}-dispatch.log"
            with log.open("ab") as output:
                subprocess.Popen(
                    [
                        *options.dispatch_command,
                        "--brief",
                        str(brief),
                        "--lane",
                        f"{episode.lane_kind}-{short}",
                        "--model",
                        options.dispatch_model,
                        "--ticket",
                        "OMN-19998",
                    ],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                )
        except OSError as exc:
            return (
                False,
                f"dispatch failed: {one_line(exc, 'could not start remote lane')}",
            )
        return True, ""

    def _read_ledger(
        self, text: str, now: datetime
    ) -> tuple[
        tuple[ModelPlacementRun, ...],
        tuple[ModelLaneRefusal, ...],
        dict[str, datetime],
        dict[str, set[str]],
        dict[str, set[str]],
        set[str],
    ]:
        """The lab-fill runs, the lane refusals, the alarms already raised, the hosts the
        host-keyed alarms inside the dedupe window named, the hosts each host-keyed alarm's
        newest row named (whatever its age), and the episodes a resolved row already closed, as of
        ``now``.

        A raise a later resolved row closed is not a raise: the next shortfall is a new episode
        with its own row, and is never adopted as the open alarm of the closed one (OMN-22868)."""
        floor = _stamp(now - LOOKBACK)
        ceiling = _stamp(now)
        fill_rows: list[LabFillRunRow] = []
        refusals: list[ModelLaneRefusal] = []
        claimed: dict[str, datetime] = {}
        raised: dict[str, datetime] = {}
        raised_hosts: dict[str, set[str]] = {}
        newest_row: dict[str, tuple[datetime, set[str]]] = {}
        resolved_at: dict[str, datetime] = {}
        resolved_episodes: set[str] = set()
        for line in text.splitlines():
            head = line[:20]
            if head > ceiling or ("lab-fill" not in line and LANE not in line):
                continue
            if head < floor and LANE not in line:
                continue
            row = parse_row(line)
            stamp = _parse_stamp(head)
            if row is None or stamp is None:
                continue
            if row.row_type == "STATUS" and row.token("lane") == "lab-fill":
                run = _RUN_KEY.match(row.value("run") or "")
                dispatched, placed = row.value("dispatched"), row.value("placed")
                if (
                    run
                    and dispatched
                    and placed
                    and dispatched.isdecimal()
                    and placed.isdecimal()
                ):
                    fill_rows.append(
                        LabFillRunRow(
                            run_key="".join(run.groups()),
                            stamp=stamp,
                            dispatched=int(dispatched),
                            placed=int(placed),
                            lanes=parse_lanes(row.value("lanes")),
                        )
                    )
            elif (
                row.row_type == "STATUS"
                and row.token("lane") == LANE
                and row.token("resolved")
            ):
                family = row.token("resolved") or ""
                resolved_at[family] = max(resolved_at.get(family, stamp), stamp)
                resolved_episodes.add(row.token("episode") or "")
            elif row.row_type == "CLAIM":
                lane_name = row.token("lane") or ""
                if _LANE_RUN_KEY.match(lane_name):
                    claimed.setdefault(lane_name, stamp)
            elif row.row_type in ("MSG", "STATUS"):
                # The runner's lane-end report was a MSG from the lane; since OMN-21822 it is a STATUS
                # row of the lane. Both name the status the lane ended on and its exit code.
                name = row.token("from" if row.row_type == "MSG" else "lane") or ""
                lane = _LANE_RUN_KEY.match(name)
                status = row.token("status") or ""
                if lane and _STATUS.match(status) and row.token("exit") != "0":
                    refusals.append(
                        ModelLaneRefusal(run_key=lane[1], lane=name, status=status)
                    )
            elif row.row_type == "FRICTION" and row.token("lane") == LANE:
                kind = row.token("class")
                if kind == JOB_EXIT:
                    continue
                family = (
                    kind if kind in (LIVENESS, QUIET, RESULTS, DRAINER) else PLACEMENT
                )
                raised[family] = max(raised.get(family, stamp), stamp)
                if family in (QUIET, DRAINER) and stamp > now - DEDUPE:
                    raised_hosts.setdefault(family, set()).update(
                        (row.token("hosts") or "").split(",")
                    )
                if family in (QUIET, DRAINER) and (
                    family not in newest_row or stamp >= newest_row[family][0]
                ):
                    newest_row[family] = (
                        stamp,
                        {h for h in (row.token("hosts") or "").split(",") if h},
                    )
        for family, closed_at in resolved_at.items():
            if family in raised and closed_at >= raised[family]:
                del raised[family]
                raised_hosts.pop(family, None)
        # The fill writes its row while its send jobs build: a pending lane is judged by its own CLAIM row.
        send_cap = timedelta(minutes=self._options.lane_send_cap_minutes)
        runs: list[ModelPlacementRun] = []
        for fill_row in fill_rows:
            settled, unplaced = settle_run(fill_row, claimed, now, send_cap)
            runs.append(settled)
            refusals.extend(unplaced)
        return (
            tuple(runs),
            tuple(refusals),
            raised,
            raised_hosts,
            {family: named for family, (_, named) in newest_row.items()},
            resolved_episodes,
        )

    def _drainer_host(self) -> str:
        return self._options.drainer.host if self._options.drainer is not None else ""

    def _watch_drainer(
        self, now: datetime, apply: bool, warnings: list[str]
    ) -> tuple[str, str, ModelDrainerResult]:
        """Read the hook-emit drainer, reinstall it when that is safe, and decide the alarm.

        A tick that applies nothing reads and decides only; the reinstall is the one mutation.
        """
        options = self._options.drainer
        if self._watch is None or options is None:
            return (
                "UNCONFIGURED",
                "none",
                ModelDrainerResult(alarm=False, state="UNKNOWN"),
            )
        before = self._watch.observe()
        if before.job_state == "unknown":
            warnings.append(
                f"silent_failure_alarm: drainer job state could not be read: {before.job_detail}"
            )

        def decide(repair: ModelDrainerRepair | None) -> ModelDrainerResult:
            return self._drainer.handle(
                ModelDrainerRequest(
                    now=now,
                    host=options.host,
                    before=before,
                    repair=repair,
                    stale_minutes=options.stale_minutes,
                )
            )

        decision = decide(None)
        if (
            decision.alarm
            and decision.repair_safe
            and before.job_state != "unknown"
            and options.host not in self._options.repair_disabled_hosts
            and apply
        ):
            decision = decide(self._watch.repair())
        return decision.state, decision.repair_outcome, decision

    @staticmethod
    def _read_host_lane_stamps(
        text: str,
        now: datetime,
        kinds: tuple[str, ...] = ("CLAIM", "STATUS", "TERMINAL"),
    ) -> dict[str, tuple[datetime, ...]]:
        """The stamps of the rows of ``kinds`` naming a pool host, by host id.

        A row written by a host timer (``actor=timer:``) names its host without a lane having run
        there, so it is not read as one (OMN-22778). Neither is a lane's own row naming a host its
        CLAIM never put the lane on: the ``host=`` cell of a STATUS that reports an install on h051,
        written by a lane running on h053, describes h051 and sends no hook event for it (OMN-23023).
        A lane with no CLAIM host in the window is counted as before.
        """
        floor, ceiling = _stamp(now - LOOKBACK), _stamp(now + timedelta(minutes=5))
        rows = []
        ran_on: dict[str, set[str]] = {}
        for line in text.splitlines():
            head = line[:20]
            if head < floor or head > ceiling or " | host=h" not in line:
                continue
            row = parse_row(line)
            stamp = _parse_stamp(head)
            host = row.token("host") if row is not None else None
            if (
                row is None
                or stamp is None
                or host is None
                or not _HOST_CELL.match(host)
                or (row.token("actor") or "").startswith(_TIMER_ACTOR_PREFIX)
            ):
                continue
            lane = row.token("lane")
            if row.row_type == "CLAIM" and lane is not None:
                ran_on.setdefault(lane, set()).add(host)
            if row.row_type in kinds:
                rows.append((stamp, host, lane))
        found: dict[str, list[datetime]] = {}
        for stamp, host, lane in rows:
            claimed = ran_on.get(lane or "")
            if claimed and host not in claimed:
                continue
            found.setdefault(host, []).append(stamp)
        return {host: tuple(sorted(stamps)) for host, stamps in found.items()}

    def _read_hook_events(self) -> tuple[dict[str, datetime], str]:
        """The newest hook event of each pool host, and why they could not be read ('' when read).

        The command prints ``<hostname>|<UTC stamp>`` for each host the table holds; a hostname
        nobody declared is left out, and a line that does not parse is a failed read.
        """
        command = self._options.hook_events_command
        if self._options.host_table_error:
            return (
                {},
                f"host table unreadable, hook-event hosts cannot be attributed: "
                f"{self._options.host_table_error}",
            )
        if not self._options.host_aliases:
            return (
                {},
                "host aliases are not configured; hook-event hosts cannot be attributed",
            )
        if command is None:
            return (
                {},
                "ONEX_HOOK_EVENTS_COMMAND is not set; the host timer has no way to read the table",
            )
        try:
            done = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._options.hook_events_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {}, one_line(exc, "the command could not run")
        if done.returncode != 0:
            tail = one_line(done.stderr.strip()[-200:], "no stderr")
            return {}, f"the command exited {done.returncode}: {tail}"
        newest: dict[str, datetime] = {}
        for line in done.stdout.splitlines():
            if not line.strip():
                continue
            name, _, text = line.partition("|")
            try:
                at = datetime.fromisoformat(text.strip())
            except ValueError:
                return {}, f"unparseable line {one_line(line, 'empty')[:80]!r}"
            if at.tzinfo is None:
                return (
                    {},
                    f"stamp without a zone in line {one_line(line, 'empty')[:80]!r}",
                )
            host = host_id(name, self._options.host_aliases)
            if host is not None:
                newest[host] = max(newest.get(host, at), at)
        return newest, ""

    @staticmethod
    def _read_watcher_tick(path: Path) -> datetime | None:
        """The PR watcher's last complete refresh (OMN-19998), or None when unreadable."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _parse_stamp(str(data.get("last_tick") or ""))
        except (OSError, ValueError, AttributeError, TypeError):
            return None

    @staticmethod
    def _read_trend(path: Path) -> datetime | None:
        """The newest point queue-status wrote, or None when the file says nothing usable."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            points = [data.get("last") or {}, *(data.get("history") or [])]
            stamps = [
                _parse_stamp(str(p["ts"]))
                for p in points
                if isinstance(p, dict) and "ts" in p
            ]
        except (OSError, ValueError, AttributeError, TypeError):
            return None
        known = [s for s in stamps if s is not None]
        return max(known) if known else None

    @staticmethod
    def _read_merges(path: Path, now: datetime) -> tuple[int | None, int | None]:
        """(merges in the hour before ``now``, open non-draft PRs) from the PR watcher state, or
        (None, None) when the state says nothing usable."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            merges = data["merges"].values()
            prs = data["prs"].values()
            stamps = [_parse_stamp(str(m.get("merged_at") or "")) for m in merges]
            open_prs = sum(
                1
                for p in prs
                if (p.get("facts") or {}).get("state") == "OPEN"
                and not (p.get("facts") or {}).get("draft")
            )
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            return None, None
        return sum(
            1 for at in stamps if at is not None and now - HOUR < at <= now
        ), open_prs

    def _deliver(self, category: str, text: str) -> tuple[bool, str, str | None]:
        """Send through the shared alert channel; credentials are sourced only in the child.

        Returns (delivered, detail, message reference). The reference is the Slack message
        timestamp the channel printed last on stdout, or None when it printed none.
        """
        options = self._options
        lib = options.alert_lib or Path(
            os.environ.get("ONEX_ALERT_CHANNEL_LIB")
            or options.registry_root
            / "omniclaude/plugins/onex/hooks/scripts/alert-channel.sh"
        )
        try:
            done = subprocess.run(
                [
                    options.bash,
                    "-c",
                    _ALERT_COMMAND,
                    "_",
                    str(options.alert_env_file),
                    str(lib),
                    category,
                    text,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, one_line(exc, "alert command could not run"), None
        lines = done.stdout.strip().splitlines()
        stamp = lines[-1].strip() if lines else ""
        ref = stamp if _SLACK_TS.match(stamp) else None
        return done.returncode == 0, f"alert channel exit {done.returncode}", ref
