#!/usr/bin/env python3
"""lane_guard_host: the bounded scope a lane's command runs in on a lab host (OMN-20199).

THE INCIDENT. On 2026-09-30 a lane's pytest (glm-coding-eval) ran 6h43m at full CPU on a lab
host. With other work it drove load to 1138 and exhausted 96 GB of RAM and swap, which took down the
dev lane, the local model server and the CI runner. Nothing bounded a command a lane starts on a lab
host, and the lane's own watchdog (``kill -TERM 0``) only reaches the engine's process group, so a
child that starts its own session survived its lane.

WHAT THIS IS. One stdlib-only program that ships to the lab host inside the lane's ssh script (``python3
-c "$SRC" run ...``), so nothing has to be installed first, and is also imported by the host reaper
(``lane_reaper.py``) and by the launching side (``lane_guard.py``). It must run on Python 3.9 (the
macOS system interpreter) through 3.13.

``run`` starts a command in the strongest bounded scope the host offers and prints one ``LANE-GUARD``
line (the mode used, the limits, the linger state) on stderr when it starts and one ``LANE-GUARD-END``
line when it ends:

- ``systemd-scope``: ``systemd-run --user --scope`` with ``RuntimeMaxSec``, ``MemoryMax``,
  ``MemorySwapMax=0``, ``CPUQuota``, ``TasksMax`` and a unit named ``onexlane-<lane>-<id>``. The kernel
  enforces memory inside the scope's cgroup, so a runaway is killed there and the host is untouched, and
  every process the command starts (a ``setsid`` child included) is in the scope and ends with it.
  Used only on Linux when ``systemctl --user`` answers and the user manager has the memory controller.
- ``timeout-prlimit``: ``timeout --kill-after=60 <runtime> prlimit --as=<bytes>`` (``--data`` for an
  engine lane, whose runtime reserves address space it never touches). Used on Linux when systemd
  user scopes are unavailable (no linger, no user bus, no memory controller).
- ``python-watchdog``: this program kills the command's session at the deadline. Used where neither
  exists (macOS). It has no memory limit and the line says so (``mem_enforced=false``).

Every mode tags the command's environment with ``ONEX_LANE_GUARD=<unit>`` and
``ONEX_LANE_GUARD_DEADLINE=<epoch>``, so a process that escaped its scope or session is still found by
``reap`` and by the reaper. When the command ends, the guard stops the unit and kills whatever carries
the tag: no orphan survives its lane. A SIGTERM, SIGHUP or SIGINT to the guard does the same (the
lane's ``kill -TERM 0`` watchdog, a dispatcher that dropped its ssh session).

``reap --unit U`` stops a unit and kills its tagged processes. ``probe`` prints the host's facts.
``spool-drain`` prints and removes the reaper's pending FRICTION records (the host holds no ledger;
the launching side appends them).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

UNIT_PREFIX = "onexlane-"
TAG_ENV = "ONEX_LANE_GUARD"
DEADLINE_ENV = "ONEX_LANE_GUARD_DEADLINE"
HARD_CAP_S = 3 * 3600
GRACE_S = 120
KILL_AFTER_S = 60
STOP_TIMEOUT_S = 30
# OMN-22991: a lane that must be stopped is told first. A headless engine run that gets SIGTERM mid-tool ends 143 with no
# ``result`` event (measured on h053 with the engine CLI 2.1.316); the same run that gets SIGINT ends 0 with a ``result`` event
# (subtype error_during_execution) within a second. So an engine lane's stop is SIGINT, then the notice, then SIGTERM.
STOP_NOTICE_S = 20
# How far apart two readings of one process's start may fall: ``ps`` reports elapsed time in whole seconds and the
# tracker and the reap read it at different moments.
IDENTITY_SLACK_S = 3.0
GIB = 1024**3
DEFAULT_MEM_BYTES = 8 * GIB
MEM_CEILING_BYTES = 32 * GIB
DEFAULT_CPU_PCT = 400
CPU_CEILING_PCT = 1600
TASKS_MAX = 8192
KINDS = ("lane", "tests")
MODE_SCOPE = "systemd-scope"
MODE_TIMEOUT = "timeout-prlimit"
MODE_WATCHDOG = "python-watchdog"
EXIT_USAGE = 64

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": GIB, "T": 1024**4}


# ----------------------------------------------------------------------------------------------
# Limits and names (pure).
# ----------------------------------------------------------------------------------------------


def parse_size(text: object) -> int:
    """``8G``, ``512M``, ``8589934592`` to bytes. Refuses anything else."""
    found = _SIZE_RE.match(str(text))
    if found is None:
        raise ValueError(f"{text!r} is not a size (use 8G, 512M or a byte count)")
    return int(float(found.group(1)) * _SIZE_UNITS[found.group(2).upper()])


def sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", text)


def unit_name(lane: str, run_id: str) -> str:
    """``onexlane-<lane>-<id>``: the lane name first, so ``systemctl --user`` and a reaper read it."""
    tail = sanitize(run_id).rsplit("-", 1)[-1][-12:].strip("-.") or "run"
    return f"{UNIT_PREFIX}{sanitize(lane)[:60].strip('-.')}-{tail}"


class Limits:
    """The bounds one guarded command runs under."""

    def __init__(
        self,
        runtime_s: int,
        mem_bytes: int,
        cpu_pct: int,
        tasks_max: int = TASKS_MAX,
        kind: str = "lane",
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"kind {kind!r} is not one of {KINDS}")
        if not 1 <= runtime_s <= HARD_CAP_S:
            raise ValueError(
                f"runtime {runtime_s}s is outside 1..{HARD_CAP_S}s (the hard cap is 3 h)"
            )
        if not 64 * 1024**2 <= mem_bytes <= MEM_CEILING_BYTES:
            raise ValueError(
                f"memory limit {mem_bytes} bytes is outside 64M..{MEM_CEILING_BYTES // GIB}G "
                "(the hard ceiling)"
            )
        if not 10 <= cpu_pct <= CPU_CEILING_PCT:
            raise ValueError(f"CPU quota {cpu_pct}% is outside 10..{CPU_CEILING_PCT}%")
        self.runtime_s, self.mem_bytes, self.cpu_pct = (
            int(runtime_s),
            int(mem_bytes),
            int(cpu_pct),
        )
        self.tasks_max, self.kind = int(tasks_max), kind

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_s": self.runtime_s,
            "mem_bytes": self.mem_bytes,
            "cpu_pct": self.cpu_pct,
            "tasks_max": self.tasks_max,
            "kind": self.kind,
        }


# OMN-22991: an engine lane that reaches its deadline is told by the guard first (SIGINT, ``stop_notice_s``, SIGTERM,
# ``STOP_TIMEOUT_S`` to SIGKILL), which only works while the guard's deadline comes before the OS limit's. So the OS limit
# (``RuntimeMaxSec``, ``timeout``) is a backstop set that long after it. A tests run gets no notice and no offset.
def backstop_runtime_s(limits: Limits, stop_notice_s: float = STOP_NOTICE_S) -> int:
    return limits.runtime_s + (
        int(stop_notice_s) + STOP_TIMEOUT_S
        if limits.kind == "lane" and stop_notice_s > 0
        else 0
    )


def scope_argv(
    unit: str, limits: Limits, command: list[str], stop_notice_s: float = STOP_NOTICE_S
) -> list[str]:
    """``systemd-run --user --scope``: the limits as unit properties, the command after ``--``."""
    return [
        # No ``--collect``: a scope that failed (memory kill, runtime limit) stays loaded in the failed state until
        # the guard has read its ``Result`` and ``reap_unit`` has reset it, so the end line can say WHY it died.
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--unit",
        unit,
        "--description",
        f"onex lane guard {unit}",
        "-p",
        f"RuntimeMaxSec={backstop_runtime_s(limits, stop_notice_s)}",
        "-p",
        f"MemoryMax={limits.mem_bytes}",
        "-p",
        "MemorySwapMax=0",
        "-p",
        f"CPUQuota={limits.cpu_pct}%",
        "-p",
        f"TasksMax={limits.tasks_max}",
        "-p",
        f"TimeoutStopSec={STOP_TIMEOUT_S}",
        "--",
        *command,
    ]


def timeout_argv(
    limits: Limits, command: list[str], stop_notice_s: float = STOP_NOTICE_S
) -> list[str]:
    """``timeout --kill-after=60 <runtime> prlimit --as|--data=<bytes> -- <command>``."""
    flag = "--as" if limits.kind == "tests" else "--data"
    return [
        "timeout",
        f"--kill-after={KILL_AFTER_S}",
        str(backstop_runtime_s(limits, stop_notice_s)),
        "prlimit",
        f"{flag}={limits.mem_bytes}",
        "--",
        *command,
    ]


def select_mode(facts: dict[str, bool]) -> str:
    """The strongest scope the host's facts allow."""
    if (
        facts.get("linux")
        and facts.get("systemd_ok")
        and facts.get("memory_controller")
    ):
        return MODE_SCOPE
    if facts.get("linux") and facts.get("timeout") and facts.get("prlimit"):
        return MODE_TIMEOUT
    return MODE_WATCHDOG


def end_exit_code(rc: int) -> int:
    """A signal death (negative returncode) as the shell would report it."""
    return 128 - rc if rc < 0 else rc


# ----------------------------------------------------------------------------------------------
# Host facts.
# ----------------------------------------------------------------------------------------------


def systemd_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """``environ`` with the variables ``systemctl --user`` needs, which a non-interactive ssh command
    lacks: the runtime directory and the user bus (present with linger, or while a login session lives)."""
    env = dict(os.environ if environ is None else environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    bus = Path(runtime) / "bus"
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and bus.exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def user_controllers() -> list[str]:
    """Controllers the user manager may use, from its cgroup (empty when unreadable, e.g. cgroup v1)."""
    uid = os.getuid()
    path = Path(
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    )
    try:
        return path.read_text(encoding="utf-8").split()
    except OSError:
        return []


def linger_state(run: Any = subprocess.run, env: dict[str, str] | None = None) -> str:
    """``yes``, ``no`` or ``unknown`` (no loginctl)."""
    if not shutil.which("loginctl"):
        return "unknown"
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid())
        done = run(
            ["loginctl", "show-user", user, "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    value = (done.stdout or "").strip().lower()
    return value if done.returncode == 0 and value in ("yes", "no") else "unknown"


def gather_facts(
    environ: dict[str, str] | None = None, run: Any = subprocess.run
) -> dict[str, Any]:
    linux = sys.platform.startswith("linux")
    env = (
        systemd_env(environ)
        if linux
        else dict(os.environ if environ is None else environ)
    )
    ok = False
    if linux and shutil.which("systemd-run") and shutil.which("systemctl"):
        try:
            ok = (
                run(
                    ["systemctl", "--user", "show-environment"],
                    capture_output=True,
                    check=False,
                    timeout=10,
                    env=env,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    controllers = user_controllers() if linux else []
    return {
        "linux": linux,
        "systemd_ok": ok,
        "controllers": controllers,
        "memory_controller": "memory" in controllers,
        "timeout": bool(shutil.which("timeout")),
        "prlimit": bool(shutil.which("prlimit")),
        "linger": linger_state(run, env) if linux else "na",
    }


# ----------------------------------------------------------------------------------------------
# The process table (Linux /proc, else ps), tagged by the guard's environment variables.
# ----------------------------------------------------------------------------------------------


class Proc:
    def __init__(
        self,
        pid: int,
        ppid: int,
        age_s: float,
        rss_kb: int,
        cmd: str,
        tag: str | None = None,
        deadline: float | None = None,
        cgroup: str = "",
    ) -> None:
        self.pid, self.ppid, self.age_s, self.rss_kb = (
            pid,
            ppid,
            float(age_s),
            int(rss_kb),
        )
        self.cmd, self.tag, self.deadline, self.cgroup = cmd, tag, deadline, cgroup

    def brief(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "age_s": int(self.age_s),
            "rss_kb": self.rss_kb,
            "cmd": self.cmd[:160],
        }


_TAG_RE = re.compile(rb"(?:^|\0)" + TAG_ENV.encode() + rb"=([^\0]*)")
_DEADLINE_RE = re.compile(rb"(?:^|\0)" + DEADLINE_ENV.encode() + rb"=(\d+)")
_PS_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(.*)$")
_PS_TAG_RE = re.compile(r"(?:^|\s)" + TAG_ENV + r"=(\S+)")
_PS_DEADLINE_RE = re.compile(r"(?:^|\s)" + DEADLINE_ENV + r"=(\d+)")


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def read_proc_table_linux(proc_root: str = "/proc") -> list[Proc]:
    """Every process this user can read: pid, parent, age, RSS, command line, cgroup and the guard tag."""
    try:
        uptime = float(_read(f"{proc_root}/uptime").split()[0])
        tck = os.sysconf("SC_CLK_TCK")
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, ValueError, IndexError):
        return []
    table: list[Proc] = []
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        base = f"{proc_root}/{name}"
        try:
            stat = _read(f"{base}/stat").decode("utf-8", "replace")
            fields = stat[stat.rindex(")") + 2 :].split()
            ppid, start = int(fields[1]), int(fields[19])
            rss_kb = int(_read(f"{base}/statm").split()[1]) * page_kb
            cmd = (
                _read(f"{base}/cmdline")
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
                .strip()
            )
            if not cmd:
                cmd = _read(f"{base}/comm").decode("utf-8", "replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        tag: str | None = None
        deadline: float | None = None
        try:
            env = _read(f"{base}/environ")
            found = _TAG_RE.search(env)
            if found:
                tag = found.group(1).decode("utf-8", "replace")
                dl = _DEADLINE_RE.search(env)
                deadline = float(dl.group(1)) if dl else None
        except OSError:
            pass
        try:
            cgroup = (
                _read(f"{base}/cgroup")
                .decode("utf-8", "replace")
                .strip()
                .splitlines()[0]
            )
        except (OSError, IndexError):
            cgroup = ""
        table.append(
            Proc(
                int(name),
                ppid,
                max(0.0, uptime - start / tck),
                rss_kb,
                cmd,
                tag,
                deadline,
                cgroup,
            )
        )
    return table


def etime_seconds(text: str) -> int:
    """``[[dd-]hh:]mm:ss`` (ps etime) to seconds."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_ps(output: str) -> list[Proc]:
    """``ps -axeww -o pid=,ppid=,etime=,rss=,command=`` (the ``e`` puts the environment after the command)."""
    table: list[Proc] = []
    for line in output.splitlines():
        found = _PS_RE.match(line)
        if not found:
            continue
        tag = _PS_TAG_RE.search(found.group(5))
        dl = _PS_DEADLINE_RE.search(found.group(5))
        try:
            age = etime_seconds(found.group(3))
        except ValueError:
            continue
        table.append(
            Proc(
                int(found.group(1)),
                int(found.group(2)),
                age,
                int(found.group(4)),
                found.group(5),
                tag.group(1) if tag else None,
                float(dl.group(1)) if dl else None,
            )
        )
    return table


def read_proc_table() -> list[Proc]:
    if sys.platform.startswith("linux"):
        return read_proc_table_linux()
    try:
        done = subprocess.run(
            ["ps", "-axeww", "-o", "pid=,ppid=,etime=,rss=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ps(done.stdout) if done.returncode == 0 else []


def kill_pids(
    pids: list[int],
    grace_s: float = 5.0,
    sleep: Any = time.sleep,
    kill: Any = os.kill,
    alive: Any = None,
) -> dict[int, str]:
    """SIGTERM every pid, wait up to ``grace_s`` for them to go, SIGKILL the rest. pid -> how it ended
    (``term``, ``kill``, ``gone`` when it was already gone, ``denied``)."""

    def is_alive(pid: int) -> bool:
        try:
            kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    alive = alive or is_alive
    result: dict[int, str] = {}
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
            result[pid] = "term"
        except ProcessLookupError:
            result[pid] = "gone"
        except PermissionError:
            result[pid] = "denied"
    end = time.monotonic() + grace_s
    pending = [p for p in pids if result[p] == "term"]
    while pending and time.monotonic() < end:
        sleep(0.25)
        pending = [p for p in pending if alive(p)]
    for pid in pending:
        try:
            kill(pid, signal.SIGKILL)
            result[pid] = "kill"
        except (ProcessLookupError, PermissionError):
            pass
    return result


def scope_result(
    unit: str, *, run: Any = subprocess.run, environ: dict[str, str] | None = None
) -> str | None:
    """The scope's own verdict from systemd, read after it ended: ``success``, ``oom-kill``, ``timeout``
    (RuntimeMaxSec), ``signal``, ``exit-code`` and so on; None when it cannot be read."""
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        return None
    try:
        done = run(
            ["systemctl", "--user", "show", unit + ".scope", "-p", "Result", "--value"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=systemd_env(environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (done.stdout or "").strip()
    return value if done.returncode == 0 and value else None


# OMN-22991: a pid is a name, not an identity. On a Mac pids wrap at 99999 and a busy lab host reuses one about every
# hour, so a pid the tracker saw in a neighbouring lane's tree can later belong to another lane's guard, run script or
# engine. The old test (age >= age at first sight - 2) held for every process older than two seconds, so a lane ending
# signalled every live process that shared a number with anything it had ever spawned. A process keeps its start time
# for life; that time must equal the one recorded.
def same_process(proc: Proc, born: float, now: float) -> bool:
    return abs((now - proc.age_s) - born) <= IDENTITY_SLACK_S


def reap_unit(
    unit: str,
    *,
    run: Any = subprocess.run,
    read_table: Any = read_proc_table,
    kill: Any = kill_pids,
    environ: dict[str, str] | None = None,
    tracked: dict[int, float] | None = None,
) -> dict[str, Any]:
    """Stop the systemd unit (when there is one) and kill every process that carries the unit's tag, and every
    ``tracked`` descendant (pid -> epoch it started) that is still the same process (macOS hides the
    environment from ps, so there the guard follows the process tree while the command runs)."""
    stopped = False
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        env = systemd_env(environ)
        for suffix in (".scope", ".service"):
            try:
                done = run(
                    ["systemctl", "--user", "stop", unit + suffix],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=STOP_TIMEOUT_S + 30,
                    env=env,
                )
                stopped = stopped or done.returncode == 0
                run(
                    ["systemctl", "--user", "reset-failed", unit + suffix],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    me = os.getpid()
    table = read_table()
    now = time.time()
    victims = [p for p in table if p.tag == unit and p.pid != me]
    seen = {p.pid for p in victims}
    for p in table:
        born = (tracked or {}).get(p.pid)
        if (
            born is not None
            and p.pid not in seen
            and p.pid != me
            and same_process(p, born, now)
        ):
            victims.append(p)
    outcome = kill([p.pid for p in victims]) if victims else {}
    return {
        "unit": unit,
        "systemd_stopped": stopped,
        "killed": [{**p.brief(), "how": outcome.get(p.pid, "?")} for p in victims],
    }


# ----------------------------------------------------------------------------------------------
# The friction spool: the host holds no ledger, so kills are queued for the launching side.
# ----------------------------------------------------------------------------------------------


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "onex-lane-guard"


def spool_append(record: dict[str, object], directory: Path | None = None) -> Path:
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "friction-spool.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def spool_drain(directory: Path | None = None) -> list[dict[str, object]]:
    """Take every queued record (the file is renamed first, so an append during the read is kept)."""
    directory = directory or state_dir()
    path = directory / "friction-spool.jsonl"
    taken = directory / f"friction-spool.draining-{os.getpid()}"
    try:
        os.replace(path, taken)
    except OSError:
        return []
    records: list[dict[str, object]] = []
    for line in taken.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    taken.unlink(missing_ok=True)
    return records


# ----------------------------------------------------------------------------------------------
# The end record: how a killed lane ended and where its work is (read only).
# ----------------------------------------------------------------------------------------------

END_RECORD_SCHEMA = 1
CAUSE_OOM_KILL = "oom_kill"
CAUSE_RUNTIME_MAX = "runtime_max"
CAUSE_SIGNAL = "signal"
CAUSE_EXIT_CODE = "exit_code"
TREE_DEPTHS = (1, 2)
RECORD_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def classify_end(
    rc: int,
    verdict: str | None,
    deadline_hit: bool,
    stopped_by_signal: int | None,
    oom_kills: int,
) -> str | None:
    """How the command ended: ``oom_kill``, ``runtime_max``, ``signal``, ``exit_code``, or None for a clean exit."""
    if verdict == "oom-kill" or oom_kills > 0:
        return CAUSE_OOM_KILL
    if deadline_hit:
        return CAUSE_RUNTIME_MAX
    if rc < 0 or stopped_by_signal is not None:
        return CAUSE_SIGNAL
    if rc != 0:
        return CAUSE_EXIT_CODE
    return None


def find_trees(worktrees_dir: str, lane: str, max_trees: int) -> list[str]:
    """The outermost git trees under ``<worktrees_dir>/<lane>`` (depth 1 or 2, the layout the closeout inventory
    reads), sorted, at most ``max_trees``; empty when the lane directory is missing."""
    lane_dir = Path(worktrees_dir) / lane
    found: list[str] = []
    if not lane_dir.is_dir():
        return found
    for root, dirs, files in os.walk(lane_dir):
        depth = len(Path(root).relative_to(lane_dir).parts)
        if depth in TREE_DEPTHS and ".git" in (*dirs, *files):
            found.append(root)
            dirs[:] = []
        elif depth >= TREE_DEPTHS[-1]:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d != ".git"]
    return sorted(found)[:max_trees]


def read_tree(
    path: str, git_timeout_s: int, run: Any = subprocess.run
) -> dict[str, object]:
    """One tree's branch, last pushed head, unpushed commit count and changed file count. Reads only: every
    command is a ``git --no-optional-locks`` read (symbolic-ref, rev-parse, rev-list, status), so the tree is unchanged."""
    out: dict[str, object] = {
        "path": path,
        "branch": "",
        "last_pushed_head": "",
        "unpushed_commits": 0,
        "changed_files": 0,
        "readable": True,
        "error": "",
    }

    def git(*argv: str) -> Any:
        return run(
            ["git", "--no-optional-locks", "-C", path, *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=git_timeout_s,
        )

    try:
        branch = git("symbolic-ref", "-q", "--short", "HEAD")
        out["branch"] = (branch.stdout or "").strip() if branch.returncode == 0 else ""
        upstream = git("rev-parse", "-q", "--verify", "@{u}")
        pushed = upstream
        if upstream.returncode != 0 and out["branch"]:
            pushed = git(
                "rev-parse", "-q", "--verify", f"refs/remotes/origin/{out['branch']}"
            )
        out["last_pushed_head"] = (
            (pushed.stdout or "").strip() if pushed.returncode == 0 else ""
        )
        count_args = (
            ["@{u}..HEAD"]
            if upstream.returncode == 0
            else ["HEAD", "--not", "--remotes"]
        )
        count = git("rev-list", "--count", *count_args)
        status = git("status", "--porcelain")
        if count.returncode != 0 or status.returncode != 0:
            raise ValueError("git could not read the tree")
        out["unpushed_commits"] = int((count.stdout or "0").strip())
        out["changed_files"] = len(
            [ln for ln in (status.stdout or "").splitlines() if ln.strip()]
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        out.update(
            {
                "readable": False,
                "error": type(exc).__name__,
                "unpushed_commits": 0,
                "changed_files": 0,
            }
        )
    return out


def build_end_record(
    *,
    unit: str,
    lane: str,
    host: str,
    mode: str,
    cause: str,
    rc: int,
    signal: int | None,
    mem_peak_bytes: int | None,
    mem_limit_bytes: int,
    mem_enforced: bool,
    runtime_s: float,
    runtime_limit_s: int,
    ended_at: str,
    trees: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": END_RECORD_SCHEMA,
        "unit": unit,
        "lane": lane,
        "host": host,
        "mode": mode,
        "cause": cause,
        "exit": end_exit_code(rc),
        "signal": signal,
        "mem_peak_bytes": mem_peak_bytes,
        "mem_limit_bytes": mem_limit_bytes,
        "mem_enforced": mem_enforced,
        "runtime_s": runtime_s,
        "runtime_limit_s": runtime_limit_s,
        "ended_at": ended_at,
        "trees": trees,
    }


def write_end_record(record_dir: str, unit: str, record: dict[str, object]) -> str:
    """``<record_dir>/<unit>.json``, written atomically (a temp file in the same directory, then a rename)."""
    directory = Path(record_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{unit}.json"
    held = directory / f".{unit}.json.tmp"
    held.write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(held, path)
    return str(path)


# ----------------------------------------------------------------------------------------------
# run: start the command in its scope and end everything with it.
# ----------------------------------------------------------------------------------------------


def _emit(label: str, payload: dict[str, object], stream: Any = None) -> None:
    # the ssh session that read these lines may be gone; the cleanup must still finish
    with contextlib.suppress(OSError):
        out = stream or sys.stderr
        out.write(f"{label} {json.dumps(payload, sort_keys=True)}\n")
        out.flush()


def _cgroup_dir(pid: int) -> Path | None:
    try:
        for line in (
            _read(f"/proc/{pid}/cgroup").decode("utf-8", "replace").splitlines()
        ):
            if line.startswith("0::"):
                return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    except OSError:
        return None
    return None


class _CgroupWatch(threading.Thread):
    """Samples the scope's own cgroup while the command runs, so the end line can say whether the kernel
    enforced the memory limit and whether it killed something (the cgroup is gone once the scope ends)."""

    def __init__(self, pid: int, unit: str) -> None:
        super().__init__(daemon=True)
        self.pid, self.unit = pid, unit
        self.stop = threading.Event()
        self.memory_max: int | None = None
        self.peak: int | None = None
        self.oom_kills = 0

    def run(self) -> None:
        while not self.stop.is_set():
            cg = _cgroup_dir(self.pid)
            if cg is not None and cg.name == self.unit + ".scope":
                self._sample(cg)
            self.stop.wait(0.3)

    def _sample(self, cg: Path) -> None:
        try:
            raw = (cg / "memory.max").read_text(encoding="utf-8").strip()
            self.memory_max = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            pass
        for name in ("memory.peak", "memory.current"):
            try:
                value = int((cg / name).read_text(encoding="utf-8").strip())
                self.peak = max(self.peak or 0, value)
                break
            except (OSError, ValueError):
                continue
        try:
            for line in (cg / "memory.events").read_text(encoding="utf-8").splitlines():
                key, _, count = line.partition(" ")
                if key == "oom_kill":
                    self.oom_kills = max(self.oom_kills, int(count))
        except (OSError, ValueError):
            pass


class _TreeTracker(threading.Thread):
    """Where the environment tag cannot be read (macOS), follows the command's process tree while it runs, so
    a child that later leaves the tree (reparented to init) is still known and is killed with its lane."""

    def __init__(self, root: int, interval_s: float = 0.25) -> None:
        super().__init__(daemon=True)
        self.root, self.interval_s = root, interval_s
        self.stop = threading.Event()
        self.seen: dict[int, float] = {}

    def run(self) -> None:
        while not self.stop.is_set():
            table = read_proc_table()
            kids: dict[int, list[Proc]] = {}
            for p in table:
                kids.setdefault(p.ppid, []).append(p)
            frontier = [self.root]
            while frontier:
                for child in kids.get(frontier.pop(), []):
                    if child.pid not in self.seen:
                        self.seen[child.pid] = (
                            time.time() - child.age_s
                        )  # the epoch it started: its identity
                        frontier.append(child.pid)
            self.stop.wait(self.interval_s)


# OMN-22991: a stop (a SIGTERM, SIGHUP or SIGINT to this guard, or the deadline of an engine lane in any mode) of an engine lane
# (kind=lane) is a notice first: SIGINT to the command's group, then stop_notice_s seconds for it to write its report and
# exit, and only then the SIGTERM and SIGKILL of before. A tests run gets no notice.
def run_guarded(
    unit: str,
    limits: Limits,
    command: list[str],
    lane: str = "",
    end_record: dict[str, Any] | None = None,
    stop_notice_s: float = STOP_NOTICE_S,
) -> int:
    """Run ``command`` bounded by ``limits``; return the exit code the shell would see.

    ``end_record`` (record_dir, worktrees_dir, git_timeout_s, max_trees) switches on the end record: a command
    that ended by a memory kill, the runtime limit, a signal or a non-zero exit leaves one typed record in
    ``record_dir``; a clean exit leaves none."""
    if not command:
        sys.stderr.write("lane_guard_host: no command after --" + "\n")
        return EXIT_USAGE
    started = time.time()
    deadline = started + limits.runtime_s
    facts = gather_facts()
    mode = select_mode(facts)
    env = systemd_env() if mode == MODE_SCOPE else dict(os.environ)
    env[TAG_ENV] = unit
    env[DEADLINE_ENV] = str(int(deadline))
    notice_s = stop_notice_s if limits.kind == "lane" else 0.0
    if mode == MODE_SCOPE:
        argv = scope_argv(unit, limits, command, notice_s)
    elif mode == MODE_TIMEOUT:
        argv = timeout_argv(limits, command, notice_s)
    else:
        argv = list(command)
    enforced = {
        "runtime": True,
        "memory": mode in (MODE_SCOPE, MODE_TIMEOUT),
        "swap": mode == MODE_SCOPE,
        "cpu": mode == MODE_SCOPE and "cpu" in facts.get("controllers", []),
    }
    start_info: dict[str, object] = {
        "event": "start",
        "lane": lane,
        "unit": unit,
        "mode": mode,
        **limits.as_dict(),
        "deadline": int(deadline),
        "linger": facts["linger"],
        "controllers": facts["controllers"],
        "enforced": enforced,
        "mem_enforced": enforced["memory"],
        "host": os.uname().nodename,
    }
    stop_signal: list[int] = []

    def on_signal(signum: int, _frame: object) -> None:
        stop_signal.append(signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    # systemd-run itself starts when the lane's program does not exist and exits 1 ("Failed to find executable"), so
    # a missing program is resolved here, the same in every mode: exit 127, as a shell would.
    if shutil.which(command[0], path=env.get("PATH", os.defpath)) is None:
        missing = f"cannot start {command[0]}: no such executable"
        _emit("LANE-GUARD", {**start_info, "error": missing})
        _emit(
            "LANE-GUARD-END", {"unit": unit, "mode": mode, "rc": 127, "error": missing}
        )
        return 127
    try:
        proc = subprocess.Popen(argv, env=env, start_new_session=True)
    except OSError as exc:
        _emit("LANE-GUARD", {**start_info, "error": f"cannot start {argv[0]}: {exc}"})
        _emit(
            "LANE-GUARD-END", {"unit": unit, "mode": mode, "rc": 127, "error": str(exc)}
        )
        return 127
    start_info["pid"] = proc.pid
    _emit("LANE-GUARD", start_info)
    watch = _CgroupWatch(proc.pid, unit) if mode == MODE_SCOPE else None
    if watch:
        watch.start()
    tracker = None if sys.platform.startswith("linux") else _TreeTracker(proc.pid)
    if tracker:
        tracker.start()
    term_at: float | None = None
    notice_at: float | None = None
    stop_phase: str | None = None
    killed_group = deadline_fired = False
    rc: int | None = None
    while rc is None:
        try:
            rc = proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        # An engine lane's own deadline is enforced here in every mode, so it gets the notice (OMN-22991); the OS limit
        # is the backstop ``backstop_runtime_s`` sets after it. A tests run keeps the OS limit as its only deadline.
        if (
            stop_signal or ((mode == MODE_WATCHDOG or notice_s > 0) and now >= deadline)
        ) and term_at is None:
            if notice_at is None and notice_s > 0:
                notice_at, stop_phase, deadline_fired = now, "notice", not stop_signal
                _signal_group(proc.pid, signal.SIGINT)
            if notice_at is None or now - notice_at >= notice_s:
                deadline_fired = deadline_fired or not stop_signal
                if stop_signal:
                    reap_unit(unit, tracked=tracker.seen if tracker else None)
                _signal_group(proc.pid, signal.SIGTERM)
                term_at, stop_phase = now, "term"
        if (
            term_at is not None
            and not killed_group
            and now - term_at >= (10 if stop_signal else KILL_AFTER_S)
        ):
            _signal_group(proc.pid, signal.SIGKILL)
            killed_group = True
    if watch:
        watch.stop.set()
        watch.join(timeout=2)
    if tracker:
        tracker.stop.set()
        tracker.join(timeout=2)
    ended = time.time()
    verdict = (
        scope_result(unit) if mode == MODE_SCOPE else None
    )  # before reap_unit resets the failed scope
    residue = reap_unit(unit, tracked=tracker.seen if tracker else None)
    deadline_hit = (
        deadline_fired or verdict == "timeout" or (ended >= deadline - 2 and rc != 0)
    )
    end_info: dict[str, object] = {
        "event": "end",
        "unit": unit,
        "mode": mode,
        "rc": rc,
        "exit": end_exit_code(rc),
        "signal": -rc if rc < 0 else None,
        "elapsed_s": round(ended - started, 1),
        "deadline_hit": bool(deadline_hit),
        "stopped_by_signal": stop_signal[0] if stop_signal else None,
        "stop_phase": stop_phase,
        "residue_killed": len(residue["killed"]),
        "residue": residue["killed"],
    }
    if mode == MODE_SCOPE:
        end_info["scope_result"] = verdict
    if watch:
        end_info.update(
            {
                "memory_max_seen": watch.memory_max,
                "mem_peak_bytes": watch.peak,
                "oom_kills": max(watch.oom_kills, 1 if verdict == "oom-kill" else 0),
            }
        )
    cause = classify_end(
        rc,
        verdict,
        bool(deadline_hit),
        stop_signal[0] if stop_signal else None,
        watch.oom_kills if watch else 0,
    )
    end_info["cause"] = cause
    if cause is not None and end_record is not None:
        try:
            trees = [
                read_tree(tree, int(end_record["git_timeout_s"]))
                for tree in find_trees(
                    str(end_record["worktrees_dir"]), lane, int(end_record["max_trees"])
                )
            ]
            record = build_end_record(
                unit=unit,
                lane=lane,
                host=os.uname().nodename,
                mode=mode,
                cause=cause,
                rc=rc,
                signal=-rc if rc < 0 else None,
                mem_peak_bytes=watch.peak if watch else None,
                mem_limit_bytes=limits.mem_bytes,
                mem_enforced=bool(enforced["memory"]),
                runtime_s=round(ended - started, 1),
                runtime_limit_s=limits.runtime_s,
                ended_at=time.strftime(RECORD_STAMP, time.gmtime(ended)),
                trees=trees,
            )
            end_info["end_record"] = write_end_record(
                str(end_record["record_dir"]), unit, record
            )
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            end_info["end_record_error"] = type(exc).__name__
    _emit("LANE-GUARD-END", end_info)
    return end_exit_code(rc)


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(
        OSError
    ):  # ProcessLookupError and PermissionError are OSErrors
        os.killpg(pid, sig)


# ----------------------------------------------------------------------------------------------
# Command line.
# ----------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lane_guard_host")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--unit", required=True)
    run.add_argument("--lane", default="")
    run.add_argument("--runtime-s", type=int, required=True)
    run.add_argument("--mem-bytes", type=int, default=DEFAULT_MEM_BYTES)
    run.add_argument("--cpu-pct", type=int, default=DEFAULT_CPU_PCT)
    run.add_argument("--tasks-max", type=int, default=TASKS_MAX)
    run.add_argument("--kind", choices=KINDS, default="lane")
    run.add_argument("--stop-notice-s", type=float, default=STOP_NOTICE_S)
    run.add_argument("--record-dir", default=None)
    run.add_argument("--worktrees-dir", default=None)
    run.add_argument("--git-timeout-s", type=int, default=None)
    run.add_argument("--max-trees", type=int, default=None)
    reap = sub.add_parser("reap")
    reap.add_argument("--unit", required=True)
    sub.add_parser("probe")
    sub.add_parser("spool-drain")
    return ap


def _expand_path(path: str) -> str:
    """A settings path as the host reads it: ``~`` is the lane user's home and ``$VAR`` (``$OMNI_HOME``, the host's own
    registry root) the lane's environment, so one overlay row serves every lab host."""
    return os.path.expanduser(os.path.expandvars(path))


def main(argv: list[str] | None = None) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "--" in words:
        split = words.index("--")
        words, command = words[:split], words[split + 1 :]
    ns = _parser().parse_args(words)
    if ns.cmd == "run":
        if not ns.unit.startswith(UNIT_PREFIX):
            sys.stderr.write(
                f"lane_guard_host: unit {ns.unit!r} must start with {UNIT_PREFIX}"
                + "\n"
            )
            return EXIT_USAGE
        try:
            limits = Limits(
                ns.runtime_s, ns.mem_bytes, ns.cpu_pct, ns.tasks_max, ns.kind
            )
        except ValueError as exc:
            sys.stderr.write(f"lane_guard_host: {exc}" + "\n")
            return EXIT_USAGE
        end_record: dict[str, Any] | None = None
        if ns.record_dir is not None:
            if None in (ns.worktrees_dir, ns.git_timeout_s, ns.max_trees):
                sys.stderr.write(
                    "lane_guard_host: --record-dir needs --worktrees-dir, --git-timeout-s and --max-trees"
                    + "\n"
                )
                return EXIT_USAGE
            end_record = {
                "record_dir": _expand_path(ns.record_dir),
                "worktrees_dir": _expand_path(ns.worktrees_dir),
                "git_timeout_s": ns.git_timeout_s,
                "max_trees": ns.max_trees,
            }
        return run_guarded(
            ns.unit,
            limits,
            command,
            ns.lane,
            end_record,
            stop_notice_s=ns.stop_notice_s,
        )
    if ns.cmd == "reap":
        if not ns.unit.startswith(UNIT_PREFIX):
            sys.stderr.write(
                f"lane_guard_host: unit {ns.unit!r} must start with {UNIT_PREFIX}"
                + "\n"
            )
            return EXIT_USAGE
        _emit("LANE-GUARD-STOP", reap_unit(ns.unit), sys.stdout)
        return 0
    if ns.cmd == "probe":
        facts = gather_facts()
        _emit(
            "LANE-GUARD-PROBE",
            {
                **facts,
                "mode": select_mode(facts),
                "host": os.uname().nodename,
            },
            sys.stdout,
        )
        return 0
    for record in spool_drain():
        _emit("LANE-GUARD-SPOOL", record, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
