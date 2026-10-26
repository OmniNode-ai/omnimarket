"""Typed input and output of the throughput tick's host pool alarm (OMN-17427).

Frozen dataclasses, not pydantic: throughput_tick.py, which calls the handler, runs in the offline skill suites and on
installed plugin copies, where only the standard library is installed.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The bounds are the tick's own. Swap-in over 20 MB/s is a host paging its working set back in (one pool host read
# 85 MB/s while it answered the probe in 20 s); the admission bar now refuses swap-in over the same bound
# (ADMIT_MAX_SWAP_IN_KB_S, OMN-21288). Memory PSI is judged far above the admission bar (5), which only refuses placement and prints as a NOTE; the
# macOS critical level is the admission bar's own.
SWAP_IN_BOUND_KB_S = 20000.0
MEM_PSI_BOUND = 20.0
MEM_PRESSURE_CRITICAL = 4


@dataclass(frozen=True)
class ModelHostPoolReading:
    """One declared host's pool read, from the probe's own fields (landing_placement's ``Reading``)."""

    host: str
    lane_cap: int
    error: (
        str | None
    )  # why the read failed (UNREADABLE, a timeout); None: the host answered
    available: bool = True  # False: the shared table marks the host down, which is its own state and not this alarm's
    load1: float | None = None
    swap_used_frac: float | None = None
    swap_in_kb_s: float | None = None
    swap_out_kb_s: float | None = None
    mem_psi_avg60: float | None = None
    mem_pressure: int | None = None


@dataclass(frozen=True)
class ModelHostPoolAlarmRequest:
    """The pool read of this tick, where the per-host streaks are kept, and where the attention rows go."""

    readings: list[ModelHostPoolReading]
    now: datetime
    state_dir: Path  # holds host-pool-alarm.json, the consecutive reads of each host
    exception_dir: Path | None = None  # the exception queue; None: its default
    emit: bool = (
        True  # False (a read-only tick): nothing is written, the streaks are only read
    )
    runner: Path | None = None  # the exception queue's mechanize runner
    parent: str | None = None
    plugin_repo: str | None = (
        None  # the repository the exception rows are filed against (the repos overlay's plugin_source)
    )


@dataclass(frozen=True)
class ModelHostPoolAlarm:
    """The findings as (level, loop, why, fix), and the hosts whose free-headroom line the tick must not act on."""

    found: list[tuple[str, str, str, str]]
    silenced: frozenset[str]
