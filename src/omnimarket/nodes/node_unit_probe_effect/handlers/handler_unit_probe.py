# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling audit's host-unit probe (OMN-19854, OMN-20780).

A launchd unit is read through ``launchctl list`` and the newest mtime of its log files. A systemd timer is read through
``systemctl show``, the system manager first and the user manager before it is called unloaded (a lab host installs its
timers as user units). On a host with no ``systemctl`` (a Mac) the timer is asked of the lab pool: one ssh
``systemctl show '*.timer'`` per remote host of the pool the landing controller places on, once per process, so a timer
the launching host cannot probe is judged from the host that runs it.

``loaded`` is None when nothing that could answer did: a scheduler this host lacks and a pool with a silent host or a
scope that failed leave the unit unknown, never unloaded.

A launchd unit that runs and fails writes a census record (``census.json``, beside the heartbeat the declaration names as
its evidence file) with ``status`` FAIL and its cause, but no heartbeat (OMN-22165). ``record_failure`` reads that record:
a failing record newer than the heartbeat, or with no heartbeat at all, is the unit's cause. A failing record is never a
run record, so it does not count toward ``last_run``. A unit with no record at all has no cause to report.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models.model_unit_probe import ModelUnitProbeRequest, ModelUnitProbeResult

PoolAnswer = tuple[bool, dict[str, tuple[bool, datetime | None]]]

# The census record a unit writes beside its declared evidence file on every run (the consumer-group guard's census.json).
CENSUS_RECORD = "census.json"
# The record fields that give a failing run's cause, most specific first.
CAUSE_FIELDS = ("error", "reap_detail", "detail")
# The same bound landing_placement's probes use for an ssh round trip.
POOL_SSH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ServerAliveInterval=30",
)
POOL_SSH_TIMEOUT_S = 20.0
# What each pool host runs: both scopes, the glob matching every loaded timer, the process's zone pinned so
# LastTriggerUSec parses. @@ lines frame the answer.
POOL_SCRIPT = """\
command -v systemctl >/dev/null 2>&1 || { echo @@nosystemd; exit 0; }
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" TZ=UTC LC_ALL=C
echo @@systemd
for scope in system user; do
  flag=; [ "$scope" = user ] && flag=--user
  echo "@@scope=$scope"
  systemctl $flag show '*.timer' -p Id -p ActiveState -p LastTriggerUSec || echo "@@failed=$scope"
done
"""


@dataclass
class PoolTimers:
    """Every systemd timer the lab pool's hosts report, from one ssh `systemctl show` per host."""

    active: dict[str, datetime | None] = field(
        default_factory=dict
    )  # unit -> last trigger, where it is active
    last: dict[str, datetime] = field(
        default_factory=dict
    )  # unit -> last trigger, where it is not active
    # Every remote pool host answered both scopes and one runs systemd: a unit in neither map is not loaded
    # anywhere. With a host silent, it might be that host's, so the unit stays unknown.
    complete: bool = False


# One read of the pool per process and pool, keyed by the hosts' ssh targets.
POOL_TIMERS: dict[tuple[str, ...], PoolTimers] = {}


def parse_pool_answer(text: str) -> PoolAnswer | None:
    """One host's answer as (runs systemd, {timer: (active, last trigger)}); None when it did not answer in
    full (no frame, or a scope systemctl failed for)."""
    if "@@nosystemd" in text:
        return False, {}
    if "@@systemd" not in text or "@@failed=" in text:
        return None
    units: dict[str, tuple[bool, datetime | None]] = {}
    block: dict[str, str] = {}

    def flush() -> None:
        if "Id" in block:
            try:
                last = datetime.strptime(
                    block.get("LastTriggerUSec", ""), "%a %Y-%m-%d %H:%M:%S UTC"
                ).replace(tzinfo=UTC)
            except ValueError:
                last = None
            seen = units.get(block["Id"])
            active = block.get("ActiveState") == "active" or bool(seen and seen[0])
            units[block["Id"]] = (
                active,
                max((t for t in (last, seen[1] if seen else None) if t), default=None),
            )
        block.clear()

    for line in text.splitlines():
        if not line.strip() or line.startswith("@@"):
            flush()
        elif "=" in line:
            key, _, value = line.partition("=")
            if key == "Id" and "Id" in block:
                flush()
            block[key] = value.strip()
    flush()
    return True, units


def ask_pool_host(target: str) -> PoolAnswer | None:
    try:
        proc = subprocess.run(
            ["ssh", *POOL_SSH_OPTS, target, f"sh -c {shlex.quote(POOL_SCRIPT)}"],
            capture_output=True,
            text=True,
            timeout=POOL_SSH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_pool_answer(proc.stdout) if proc.returncode == 0 else None


def pool_hosts(landing_placement_path: str) -> dict[str, str]:
    """The remote hosts of the pool the landing controller places on, as ``{ssh target: host name}`` (LANDING_POOL_HOSTS, else
    the shared lab host table); {} when the pool cannot be read."""
    try:
        spec = importlib.util.spec_from_file_location(
            "_landing_placement_unit_probe", landing_placement_path
        )
        if spec is None or spec.loader is None:
            return {}
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return {host.target: host.name for host in mod.load_pool() if host.target}
    except (OSError, ImportError, SyntaxError, AttributeError, ValueError):
        return {}


def pool_targets(landing_placement_path: str) -> list[str]:
    """The ssh targets of the pool's remote hosts; [] when the pool cannot be read."""
    return sorted(pool_hosts(landing_placement_path))


def pool_timers(landing_placement_path: str) -> PoolTimers:
    targets = tuple(pool_targets(landing_placement_path))
    if targets not in POOL_TIMERS:
        found = PoolTimers()
        if targets:
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                answers = list(pool.map(ask_pool_host, targets))
            for answer in (a for a in answers if a):
                for name, (active, last) in answer[1].items():
                    if active:
                        found.active[name] = max(
                            (t for t in (last, found.active.get(name)) if t),
                            default=None,
                        )
                    elif last:
                        found.last[name] = max(last, found.last.get(name, last))
            found.complete = all(a is not None for a in answers) and any(
                a and a[0] for a in answers
            )
        POOL_TIMERS[targets] = found
    return POOL_TIMERS[targets]


def failure_cause(path: Path) -> str:
    """The cause a census record at ``path`` gives for a failing run, or "" when it is absent, unreadable, not a census
    (no ``status``) or not FAIL."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(record, dict) or str(record.get("status", "")).upper() != "FAIL":
        return ""
    causes = [
        str(record[key]).strip()
        for key in CAUSE_FIELDS
        if str(record.get(key) or "").strip()
    ]
    return "; ".join(causes) or "status FAIL, no cause recorded"


def declared_paths(out_paths: tuple[str, ...]) -> list[Path]:
    """The unit's declared evidence files, with each one's census record beside it; an unresolved placeholder is skipped."""
    paths = [
        Path(raw).expanduser()
        for raw in out_paths
        if "{{" not in raw and "$" not in raw
    ]
    return paths + [
        path.with_name(CENSUS_RECORD) for path in paths if path.name != CENSUS_RECORD
    ]


def record_failure(out_paths: tuple[str, ...], last_run: datetime | None) -> str:
    """The cause in the newest failing census record of the unit, when it is newer than ``last_run`` (None: no heartbeat)."""
    newest: tuple[float, str] | None = None
    for path in declared_paths(out_paths):
        cause = failure_cause(path)
        try:
            at = path.stat().st_mtime if cause else 0.0
        except OSError:
            continue
        if cause and (newest is None or at > newest[0]):
            newest = (at, cause)
    if newest is None or (last_run is not None and newest[0] <= last_run.timestamp()):
        return ""
    return newest[1]


class HandlerUnitProbe:
    def handle(self, request: ModelUnitProbeRequest) -> ModelUnitProbeResult:
        """Read local unit state and its latest run record; only a systemd timer on a host with no systemctl goes
        to the pool over ssh."""
        try:
            loaded, last = self.local(request)
        except FileNotFoundError:
            loaded, last = (
                self.pool(request) if request.manager == "systemd" else (None, None)
            )
        except (OSError, subprocess.TimeoutExpired):
            loaded, last = None, None
        failure = (
            record_failure(request.out_paths, last)
            if loaded and request.manager == "launchd"
            else ""
        )
        return ModelUnitProbeResult(loaded=loaded, last_run=last, failure=failure)

    def local(
        self, request: ModelUnitProbeRequest
    ) -> tuple[bool | None, datetime | None]:
        if request.manager == "launchd":
            loaded = (
                subprocess.run(
                    ["launchctl", "list", request.label],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).returncode
                == 0
            )
            mtimes = []
            for raw in request.out_paths:
                if "{{" in raw or "$" in raw:
                    continue
                try:  # a failing census record at a declared path is a failed run, not a run record
                    if not failure_cause(Path(raw).expanduser()):
                        mtimes.append(Path(raw).expanduser().stat().st_mtime)
                except FileNotFoundError:
                    continue
            return loaded, datetime.fromtimestamp(max(mtimes), UTC) if mtimes else None
        # A lab host installs its timers as user units, so a unit the system manager does not
        # report active is read from the user manager before it is called unloaded (OMN-20780).
        for scope in ([], ["--user"]):
            active = subprocess.run(
                [
                    "systemctl",
                    *scope,
                    "show",
                    request.name,
                    "-p",
                    "ActiveState",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # `systemctl show` prints the timestamp in the process's zone; TZ=UTC makes it parseable.
            trigger = subprocess.run(
                [
                    "systemctl",
                    *scope,
                    "show",
                    request.name,
                    "-p",
                    "LastTriggerUSec",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
            )
            try:
                last = datetime.strptime(
                    trigger.stdout.strip(), "%a %Y-%m-%d %H:%M:%S UTC"
                ).replace(tzinfo=UTC)
            except ValueError:
                last = None
            if active.stdout.strip() == "active":
                return True, last
        return False, last

    def pool(
        self, request: ModelUnitProbeRequest
    ) -> tuple[bool | None, datetime | None]:
        """Active on any host that answered; not loaded when every host answered and none runs it; unknown when a
        silent host may be the one that does."""
        found = pool_timers(request.landing_placement_path)
        if request.name in found.active:
            return True, found.active[request.name]
        return (False, found.last.get(request.name)) if found.complete else (None, None)


# ---- a declared tick that runs on a pool host (OMN-22228) ---------------------------------------------------------------
#
# The lane census moved off the Mac to a lab host's systemd user timer (OMN-22152), but the host installer contract still
# declares it as a launchd tick, so the tick read the Mac's old record and printed MISSING for a census that runs elsewhere.
# A declared tick the launching host finds MISSING is asked of the pool's hosts (the host table; no host is named here):
# the user (or system) timer ``onex-<tick>.timer`` and its service. The contract's own interval judges the timer's last
# trigger, and a service whose last run failed is not a run (a timer fires while its unit aborts every hour: h051, exit 5,
# 2026-11-03), so the cause is the line's, never a green.

TICK_SCRIPT = """\
command -v systemctl >/dev/null 2>&1 || { echo @@nosystemd; exit 0; }
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" TZ=UTC LC_ALL=C
echo @@systemd
for scope in system user; do
  flag=; [ "$scope" = user ] && flag=--user
  echo "@@scope=$scope"
  systemctl $flag show "onex-$1.timer" "onex-$1.service" -p Id -p ActiveState -p LastTriggerUSec -p Result -p ExecMainStatus || echo "@@failed=$scope"
done
"""


@dataclass
class TickRun:
    """One pool host's systemd timer for a declared tick and the result of its service's last run."""

    host: str
    last_trigger: datetime | None
    result: str  # the service's last result: success, exit-code, ...
    status: str  # ExecMainStatus of the service's last run


def parse_tick_answer(text: str, host: str) -> TickRun | None:
    """The tick's timer on one host: the first scope whose timer is active; None when the host does not answer in full, has no
    systemd, or runs no such timer."""
    if "@@systemd" not in text or "@@failed=" in text:
        return None
    blocks: dict[str, dict[str, str]] = {}
    scope = ""
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("@@scope="):
            scope, current = line.partition("=")[2], {}
        elif "=" in line and not line.startswith("@@"):
            key, _, value = line.partition("=")
            if key == "Id" and "Id" in current:
                current = {}
            current[key] = value.strip()
            if key == "Id":
                blocks[f"{scope}:{value.strip()}"] = current
        elif not line.strip():
            current = {}
    for where in sorted({k.partition(":")[0] for k in blocks}):
        timer = next(
            (
                b
                for k, b in blocks.items()
                if k.startswith(f"{where}:") and k.endswith(".timer")
            ),
            {},
        )
        service = next(
            (
                b
                for k, b in blocks.items()
                if k.startswith(f"{where}:") and k.endswith(".service")
            ),
            {},
        )
        if timer.get("ActiveState") != "active":
            continue
        try:
            last = datetime.strptime(
                timer.get("LastTriggerUSec", ""), "%a %Y-%m-%d %H:%M:%S UTC"
            ).replace(tzinfo=UTC)
        except ValueError:
            last = None
        # the timer sets the result of its own unit too; only the service's counts as the run's
        return TickRun(
            host, last, service.get("Result", ""), service.get("ExecMainStatus", "")
        )
    return None


def ask_tick_host(target: str, name: str) -> str:
    try:
        proc = subprocess.run(
            [
                "ssh",
                *POOL_SSH_OPTS,
                target,
                f"sh -c {shlex.quote(TICK_SCRIPT)} _ {shlex.quote(name)}",
            ],
            capture_output=True,
            text=True,
            timeout=POOL_SSH_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def pool_tick_runs(
    name: str,
    landing_placement_path: str,
    ask: Callable[[str, str], str] = ask_tick_host,
) -> list[TickRun]:
    """Every pool host that runs the declared tick ``name`` as a systemd timer (hosts asked in parallel)."""
    hosts = pool_hosts(landing_placement_path)
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        answers = list(pool.map(lambda target: ask(target, name), sorted(hosts)))
    return [
        run
        for target, text in zip(sorted(hosts), answers, strict=True)
        if (run := parse_tick_answer(text, hosts[target]))
    ]


def pool_tick_wiring(
    tick: Any,
    verdict: Any,
    now: datetime | None,
    landing_placement_path: str,
    ask: Callable[[str, str], str] = ask_tick_host,
) -> Any:
    """``verdict`` (a Wiring) of a declared tick this host found MISSING, judged again from the pool hosts that run it as a
    systemd timer: wired when one ran inside twice the contract's interval and its service's last run succeeded; otherwise the
    line keeps its cause and names what the pool host holds. A verdict this host could not judge, or one whose unit runs here
    and failed, or a tick no pool host runs, is returned as it was."""
    if (
        verdict.wired
        or verdict.unjudgeable
        or getattr(verdict, "failed", False)
        or not tick.interval_s
    ):
        return verdict
    runs = pool_tick_runs(tick.name, landing_placement_path, ask)
    if not runs:
        return verdict
    when, notes = now or datetime.now(UTC), []
    for run in sorted(
        runs,
        key=lambda r: r.last_trigger or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    ):
        unit = f"onex-{tick.name}.timer on {run.host}"
        if run.last_trigger is None:
            notes.append(f"{unit} has no run record")
        elif (age := (when - run.last_trigger).total_seconds()) >= 2 * tick.interval_s:
            notes.append(
                f"{unit} last triggered {age:g}s ago, older than twice the {tick.interval_s:g}s interval"
            )
        elif run.result != "success":
            notes.append(
                f"{unit} triggered {age:g}s ago but onex-{tick.name}.service failed its last run "
                f"(result {run.result or 'unknown'}, status {run.status or 'unknown'})"
            )
        else:
            return replace(
                verdict,
                wired=True,
                detail=f"declared tick {tick.name}: runs on {run.host} ({unit} triggered {age:g}s ago, "
                "its last run succeeded)",
                ran_at=run.last_trigger.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
    return replace(verdict, detail=f"{verdict.detail}; {'; '.join(notes)}")
