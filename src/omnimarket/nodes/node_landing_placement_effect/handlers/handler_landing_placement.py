# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a landing worker runs: the pool host with the most free capacity (OMN-19998).

THE PROBLEM. Until this module the controller started every fix worker on the Mac it runs on and
paused all dispatch on that one host's load (a fixed 20.0, then a per-core ceiling). On 2026-09-29 the
Mac sat at load 60-114 on 24 cores while three lab hosts sat near idle: workers either did not
start or piled more load onto the busiest machine.

WHAT THIS DOES. The pool is every machine that can host a worker, the Mac included. Each host row
names the worker engines it holds a login for (claude -p). One read probes all hosts in
parallel (the Mac locally, the others over ssh) for cores, load1 and available memory.

SLOTS PER HOST (lane controller-whole-lab-0030, operator 2026-09-30 ~00:30Z: "you can fucking send
work to the 2 other macs and the pcs as well, 3 lanes is fucking dumb"). A host's slots are derived
from what it has free, not from a fixed per-host number:

- free cores are ``ceiling x cores - load - WORKER_RESIDUAL_CORES x fresh``, where load is load1, or on
  Linux the cores the one-second CPU sample found busy when that is lower (load1 counts tasks in IO wait,
  which leave the CPU idle), where ``ceiling`` is
  ``LAB_LOAD_PER_CORE`` on a lab host and the lower ``LOCAL_LOAD_PER_CORE`` on the controller Mac (the
  interactive host takes work only under its per-core threshold: the ruling of 2026-09-29, "it's fine
  to put work on this machine if there's enough capability if the load is low enough"). The residual
  charges a fresh worker (placed in the last ``PLACEMENT_SETTLE_S``) whose load the average lacks; a settled one is in load already;
- one further worker per ``WORKER_COST_CORES`` free cores (``LAB_WORKER_COST_CORES`` on a lab host), and one per ``WORKER_MEM_GB`` of memory
  above the ``MIN_FREE_GB`` floor, whichever is fewer;
- an idle host (load1 at most ``IDLE_LOAD_PER_CORE`` per core, nothing placed, memory above the
  floor) takes at least one, so a small idle machine is never left out by rounding;
- none at all while the admission rule refuses the host (``Reading.admission_refusal``: memory under
  ``ADMIT_MIN_FREE_GB``, memory PSI or swap traffic over the bar, macOS free memory under its bar or pressure critical (OMN-19998), CPU package over
  ``ADMIT_MAX_CPU_C`` or hottest DIMM over ``ADMIT_MAX_DIMM_C``: OMN-20367), whatever its cores say (OMN-20207);
- none at all while the host's one-minute load average is over ``ADMIT_MAX_LOAD_PER_CORE`` per core
  (``Reading.load_refusal``: OMN-20366), whatever the busy-core sample, the free-core arithmetic or the cap say;
- never more than ``PLACEMENT_BURST_PER_HOST`` workers placed inside one ``PLACEMENT_SETTLE_S`` window (``Reading.fresh``,
  OMN-19998): a worker placed seconds ago is not yet in the probe's load or memory, so the budget, not the next probe, bounds a
  burst; the next worker takes another host or waits a retry;
- never more than the host's cap: a row's declared ``cap`` when it carries one, else
  ``cores // WORKER_COST_CORES`` (``LAB_WORKER_COST_CORES`` on a lab host: a 32-core PC holds 16, a 10-core
  laptop 5, and free memory binds first on the laptops; the 24-core Mac holds 6).

``policy_for`` (landing_facts) sums the pool's slots and placed workers and bounds the total by
the host table (landing_facts.pool_capacity) and the quota governor.

``choose`` spreads workers (OMN-20531): a host with nothing placed comes first, then the host with the lowest placed-over-cap
fraction, and only then the most free capacity (ranked on load1, so the least-loaded eligible host wins). Ranking on free cores
alone gave the 32-core h202 75 percent of the workers while smaller hosts sat idle. The host must hold a login for the requested engine.
The landing ladder stays Claude tiers (sonnet, opus, opus). Codex drafts first inside a worker (the delegation_step route), and a
brief class whose fix is code in the PR (``landing_facts.CODEX_FIRST_BRIEF_CLASSES``: the mechanical classes of RULING 2026-10-04T18:17:13Z
and, since OMN-20106 / RULING 2026-10-08T23:38:53Z, real_red, conflict_substantive and runtime) at the first rung is run by the Codex
engine (``codex_high``): ``choose_codex`` takes a lab host the shared table marks
``codex: true`` that has a Codex login, a writable lane workdir, a Codex sandbox that can write it, no usage-limit mark and a
free slot; with none, the worker falls back to the node's Claude engine. When no host has room the worker is not placed
(exit 75; next tick retries).

``pool_slots`` is what the controller's policy reads: how many more workers the whole pool can take
now. Zero means the dispatch pauses because no machine has headroom, not because the Mac is busy.

THE TABLE. ``LANDING_POOL_HOSTS`` (a JSON list of rows) when set; ``LANDING_POOL_HOSTS=[]`` turns
placement off and every worker runs on this machine, as before. Otherwise the pool is the SHARED lab host
table, the one the lab runner reads (the file ``ONEX_LAB_RUN_HOSTS`` names, else the ``lab_hosts.hosts`` field of
the ``lab_hosts`` overlay), plus this host as the local row unless the
table carries one: every lab machine is read every tick, not the two a hardcoded list named. A row marked
``available: false`` (in the table or in a ``LANDING_POOL_HOSTS`` row) is not in the pool, so nothing is probed or
pinged there (OMN-20366). The table's
``penalty`` (load per core, for ranking only) is honored the same way the lab runner honors it: it is
subtracted as ``penalty x cores`` from a host's free cores when choosing, never from its slots. With no
table resolvable (and ``LANDING_POOL_HOSTS`` unset) a pool read refuses naming ``lab_hosts.hosts`` and exits 75;
``pool_source`` says where the rows came from.

LIVE LOGINS. A table row's engines are not declared: the probe reads them. It prints
``LOGIN <claude> <gh>``: claude is 1 when ``claude auth status`` reports the host's own login, read before any
token, or else when a real ``claude -p`` answered within six hours, the probe starting that call in the background
with the long-lived token in ``CLAUDE_TOKEN_FILE`` exported when it exists (a macOS keychain does not unlock over
ssh). A timed-out auth-status check prints ``CLAUDEWHY auth-unavailable`` and waits for the next probe instead of
starting another Claude process. ``CLAUDEWHY`` names the admission or refusal cause; a missing token file or
``loggedIn: false`` alone is never no login (OMN-20864). A failed call with a readable token file reads ``token-failed:<word>``
(``CLAUDE-TOKEN-FAILED``), and a failed not-logged-in call with neither a native login nor a token file ``no-login``
(``NO-CLAUDE-LOGIN``), so a dead token and a host never logged in are told apart (OMN-19998). gh is 1 when the host's GitHub credential verdict is ok (GITHUB CREDENTIAL, below). A host with no
Claude login is still read and reported (``login=none``), and takes no worker. So the day a login is added on one of those hosts those
hosts start taking workers with no code change. The remote engine exports the same token file only when the host's own login
does not read loggedIn (``TOKEN_EXPORT``).

GITHUB CLI. A host declared ``gh_absent: true`` in the pool table requires gh absent, without executing it.
The launching Mac keeps its cached ``GHAUTH`` verdict. The table changes only after the host's transport is migrated.

HOST PARITY (OMN-20266). The probe also prints the PARITY lines (``parity_probe_script``) that ``parse_parity`` turns into one
typed ``HostParity`` per host: the clones the host's table row names (``parity_clones``, a deployment fact kept in the private table)
at origin tip with their pre-commit hook installed, the dispatch venv's omnimarket revision against the
launching host's, the GitHub CLI policy, pytest importable by python3, and each installed omni plugin's file tree against the tree of the revision
it says it shipped. ``Reading.parity_refusal`` turns the failures a unit's ``ParityNeeds`` names (a plugin tree is judged for every
unit) into a PARITY-FAILED admission refusal, so a refused host takes no worker or lane, the launching host included.
``record_parity`` keeps the latest record per host in the placement directory for queue-status.
A unit that provisions (``ParityNeeds.provision``, OMN-19998: the remote-lane runner) is not refused for a clone missing from the
registry root, a clone behind its origin tip or a clone without its pre-commit hook: those are ``HostParity.repairs``, which the
runner carries out on the host it picks before the lane starts. A landing worker does not provision and keeps the refusal.

HOOK PARSE (OMN-20961). The probe also runs ``/bin/bash -n`` under the host's own /bin/bash over every hook script the host's
Claude loads (``hook_parse_script``: each enabled installed plugin's ``hooks/`` tree and the ``.sh`` files its ``hooks.json`` names,
plus the plugin trees a caller ships there, which the remote-lane runner names) and prints one ``HOOKPARSE ok|fail|unknown`` line.
On 2026-10-11 a plugin release shipped a guard hook that macOS /bin/bash 3.2 cannot parse; parity read the tree as shipped, so
placement kept sending workers and lanes to h101, where every Bash call was refused. ``Reading.hook_parse_refusal`` turns a failure
into HOOK-PARSE-FAILED and a check that could not run into HOOK-PARSE-UNKNOWN: both refuse the host, the launching host included.

TRANSPORT. The read and the remote start use ssh with the launching user's key, the same transport
the lab runner uses. The event-bus placement (OMN-20105) replaces this
transport when it lands; the placement decision here does not change.

Markers: a worker placed on a host leaves ``<placement_dir>/<host>.<lease>.<pid>``; a marker whose
pid is dead is removed on the next read, so a crashed worker frees its slot.

"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import (
    REGISTRY_ROOT_ENV,
    resolve_lab_hosts_table,
    resolve_overlay_key,
)
from omnimarket.nodes.node_clone_parity_compute.handlers import handler_clone_parity
from omnimarket.nodes.node_codex_sandbox_probe_compute.node import (
    ModelCodexSandboxProbeRequest,
    NodeCodexSandboxProbeCompute,
)
from omnimarket.nodes.node_host_capability_compute.handlers import (
    handler_host_capability as host_capability,
)
from omnimarket.nodes.node_landing_placement_effect.handlers.parity_probe import (
    PARITY_PY as _PARITY_PY,
)
from omnimarket.nodes.node_landing_placement_effect.models import (
    ModelLandingPlacementRequest,
    ModelLandingPlacementResult,
    ModelPoolHostReading,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.handlers import (
    pool_probe as recovery,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.models.model_claude_account_limit import (
    ModelClaudeAccountLimitRequest,
    ModelClaudeAccountLimitResult,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.node import (
    NodeLanePlacementRecoveryEffect,
)
from omnimarket.nodes.node_link_gate_compute.handlers import (
    handler_link_gate as link_gate,
)
from omnimarket.nodes.node_link_gate_compute.node import (
    ModelLinkGateRequest,
    ModelLinkGateVerdict,
    NodeLinkGateCompute,
)
from omnimarket.nodes.node_link_history_effect.models.model_link_history import (
    ModelLinkHistoryRequest,
)
from omnimarket.nodes.node_link_history_effect.node import NodeLinkHistoryEffect

# One worker per this many free cores (and the hardware cap when a row declares none). A lab host takes
# a worker per LAB_WORKER_COST_CORES: measured on h202 on 2026-09-30 with seven lane sessions live, a
# ``claude -p`` process holds about 250 MB and 1% of a core (it waits on the API) and its tool
# children burst to one or two cores, so four cores a worker left a 10-core laptop at load 3.9 refusing
# a third lane (REMOTE-LANE-NO-HOST, exit 75, OMN-17427). The controller Mac is the interactive host
# and keeps the conservative WORKER_COST_CORES.
WORKER_COST_CORES = 4.0
LAB_WORKER_COST_CORES = 2.0
# Admission for a lane (OMN-20199). On 2026-09-30 a runaway pytest with other work exhausted 96 GB of RAM and swap
# on a lab host and took down the dev lane, the model server and the CI runner. The remote-lane runner (which starts
# whole orchestrator lanes, with their tests) refuses a lab host, whatever its core count says, while it has under
# ADMIT_MIN_FREE_GB of memory available, or while it is short of memory in the way its OS reports it. The landing
# controller honors the same refusal (OMN-20207): ``Reading.slots`` is 0 for a refused host, because workers placed
# on h101 at memory pressure warn could not push (OMN-20216). The one admission rule (OMN-20205) is these constants, together:
#
# The floor is what one lane may take plus headroom: ADMIT_LANE_MEM_MAX_GB is the lane guard's own MemoryMax
# (lane_guard_host.DEFAULT_MEM_BYTES, 8 GiB; a test pins the two together), so a host short of the floor could not
# hold a lane at its cap. A separate 16 GB bar refused h105 at 11.6 GB free and pressure normal (2026-09-30T22:33Z).
ADMIT_LANE_MEM_MAX_GB = 8.0
ADMIT_HEADROOM_GB = 2.0
ADMIT_MIN_FREE_GB = ADMIT_LANE_MEM_MAX_GB + ADMIT_HEADROOM_GB
# Live memory measures refuse independently (OMN-20843): Linux PSI memory ``some`` avg60 (the percent of the last
# minute some task stalled on memory), or sustained swap traffic on Linux and macOS. Occupancy is history, not
# pressure: h202 on 2026-09-30 had 96% swap occupied, 44 GB available and PSI 0.09 (OMN-17427).
# h201 at 2026-10-09T23:36Z refused on "swap 32% in use > 25%, memory PSI 7.6% > 5%"; at 2026-10-10T00:28Z
# it had 62 of 199 GB swap in use and PSI some avg60 2.19. Occupancy stays reported, never refuses.
ADMIT_MAX_MEM_PSI = 5.0
# Swap-in plus swap-out, KB/s, judged on the quieter of two consecutive one-second samples so one burst second
# never refuses. lab-rebuild-triage measured h201 idle at 16000-21000 KB/s si+so and calls so over 100000 KB/s
# with PSI climbing thrashing. h201 at 2026-10-10T00:33Z averaged si 12670 + so 18216 KB/s over 90 one-second
# samples, with 1 of 89 consecutive pairs over 100000 and 11 over 50000.
ADMIT_MAX_SWAP_KB_S = 100000
# Swap-in is the page-fault stall; swap-out alone reclaims cold pages (OMN-21288). This bar equals the host-pool
# alarm's SWAP_IN_BOUND_KB_S, pinned by a test. h201 at 2026-10-25T07:30Z was admitted at 25 MB/s swap-in under
# the 100000 KB/s combined bar.
ADMIT_MAX_SWAP_IN_KB_S = 20000
# macOS grows swap dynamically and compresses memory, so swap occupancy is never read there. A Mac is judged on MEASURED free
# memory, with the kernel's pressure level (kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical) as a second
# reading (OMN-19998). The level alone refused the Mac Studio (h200, 192 GB) all night on 2026-10-17: warn, with 32% free (~61 GB)
# and no lane running, because the Docker VM, the local model server and fseventsd held memory the kernel can reclaim. A Mac is
# refused at ADMIT_MAC_CRITICAL_PRESSURE whatever is free, and under ADMIT_MAC_MIN_FREE_PCT of its memory free (the kernel's own
# free percentage, kern.memorystatus_level, the probe's MEMFREE line). At warn (ADMIT_MAC_WARN_PRESSURE) it is admitted while the
# free memory covers one lane's declared need (ADMIT_LANE_MEM_MAX_GB) plus ADMIT_MAC_WARN_MARGIN_GB, so the unit placed cannot
# push the kernel into critical; h101 at 22:33Z (level 2, 14.3 GB compressed, 8 of 9.2 GB swap, 7 GB available) stays refused.
# A Mac with no free-percentage reading is judged on its available memory (free, inactive and speculative pages) the same way.
# Every refusal prints the measured figure. The per-unit cost model (WORKER_MEM_GB, cores per unit, the cap) is unchanged.
ADMIT_MAC_WARN_PRESSURE = 2
ADMIT_MAC_CRITICAL_PRESSURE = 4
ADMIT_MAC_MIN_FREE_PCT = 10.0
ADMIT_MAC_WARN_MARGIN_GB = 8.0
# The ssh link gate judges what breaks an ssh lane, sustained loss, never one short sample (OMN-17427): the policy, its numbers and its
# evidence (h105 lost 40-80% on 2026-10-01 and ssh timed out; one lost ping of ten refused h203 on 2026-10-18) live in
# omni.nodes.node_link_gate_compute, and the per-host read history in omni.nodes.node_link_history_effect. ``read_pool`` pings every
# non-local row with LINK_PING_COUNT plain unprivileged pings beside its probe (a ping that prints no summary leaves the host unjudged
# by loss; the probe's own ssh still has to succeed, and a failed probe benches the host). ``Reading.link_verdict``: BENCHED (LINK-LOSS)
# only on sustained loss over ADMIT_MAX_LINK_LOSS_PCT, DEGRADED (ranked after cleaner hosts by ``spread_key``, still admitted) over
# the node's soft bar, and never judged for a unit delivered over the event bus (``Reading.bus_delivered``).
ADMIT_MAX_LINK_LOSS_PCT = link_gate.ADMIT_MAX_LINK_LOSS_PCT
LINK_PING_COUNT = link_gate.LINK_PING_COUNT
LINK_PING_INTERVAL_S = link_gate.LINK_PING_INTERVAL_S
# A host that is too hot takes no lane or worker (OMN-20367). On 2026-10-02 h202 (open frame, water-cooled CPU) reached
# the board's 95C readout and beeped: 95C is the Ryzen Tjmax, k10temp Tctl read 90C at about 20% CPU use, and the spd5118
# DIMM sensors latched their 55C alarm once. The Linux probe reads the CPU package temperature (k10temp Tctl, or coretemp
# "Package id N") and the hottest spd5118 DIMM temp1_input, twice about a second apart, and prints their average, so one spike
# does not flap the host. Over ADMIT_MAX_CPU_C or ADMIT_MAX_DIMM_C the host takes zero new placements; a lane already running
# is never touched. A host with no such sensor (a Mac, a VM) is not judged on temperature.
ADMIT_MAX_CPU_C = 85.0
ADMIT_MAX_DIMM_C = 70.0
# A host whose one-minute load average is over ADMIT_MAX_LOAD_PER_CORE per core takes no lane or worker, whatever its cap,
# its free-core arithmetic or its sampled busy cores say (OMN-20366). On 2026-10-02 ~12:30 ET h202 sat at load1 59 on 32
# cores (1.84 per core; 13 claude -p and 7 codex placed, cap 28) while h201 sat at load 10 with 62 GB free and h101 was idle:
# the busy-core sample (``Reading.effective_load1``) read the iowait-inflated load as spare CPU and the cap admitted more.
# The bar sits above the 1.25 per core (load 40 on 32 cores) of the iowait-bound h202 of 2026-09-30, which that sample
# exists to admit, and below the 1.84 that is measured overload.
ADMIT_MAX_LOAD_PER_CORE = 1.5
# macOS judges CPU demand on measured CPU idle, never on load1 alone (OMN-20434). The macOS load average counts threads blocked
# on IO, so it overstates demand: h101 (12 cores) on 2026-10-03 ~22:38 local read load 43.06/37.82/45.69 while ``top -l 2`` reported
# 31.7% user, 9.6% sys, 58.7% idle, and ADMIT_MAX_LOAD_PER_CORE refused a host with over half its CPU free. The Mac probe takes the
# second ``top`` sample (the first is the average since boot) as CPUIDLE. A Mac with that sample is refused under
# ADMIT_MAC_MIN_CPU_IDLE_PCT idle, and its load1 is a secondary guard only at ADMIT_MAC_MAX_LOAD_PER_CORE per core, far above the
# 3.6 per core of the IO-bound h101, so a wedged host (load with no idle CPU to explain it) is still refused. A Mac with no sample
# keeps the ADMIT_MAX_LOAD_PER_CORE bar. Free memory and memory pressure (ADMIT_MAC_CRITICAL_PRESSURE) are judged separately and unchanged.
# Linux keeps load1: its CPU sample is only a slot discount, and the h202 overload of 2026-10-02 was real (OMN-20366).
ADMIT_MAC_MIN_CPU_IDLE_PCT = 25.0
ADMIT_MAC_MAX_LOAD_PER_CORE = 5.0
# A lab host keeps a reserved share of lane and worker admission that the load bar does not take (OMN-20864). h201 runs 44
# self-hosted GitHub Actions runner containers beside the lanes; from 05:45Z to 11:47Z on 2026-10-11 the remote-lane runner
# refused 65 lane dispatches no-host; in 46 of them h201's refusal was LOAD-OVER-BAR, at load1 48.9-106.2 on 32 cores with
# 36.4-54.9 GB available and 5-45 runner jobs busy, median 23 (10:35:48Z: load1 84.5, 45.7 GB, 42 jobs). The bar took h201's
# admission to zero, so the cause lanes that fix CI waited on CI. While the bar (``Reading.load_bar_refusal``) or spent free cores would admit nothing, a
# host still takes up to ADMIT_RESERVED_SLOTS units (a table row's ``reserve`` replaces it; 0 turns it off; the launching host
# keeps none unless its row names one), and only above a hard memory floor:
# each reserved unit is charged the lane guard's whole MemoryMax (ADMIT_LANE_MEM_MAX_GB, with MemorySwapMax=0) and the host
# keeps ADMIT_MIN_FREE_GB available after it, so a reserved unit cannot push the host into swap. Every other bar still refuses:
# memory under the floor, memory PSI, swap traffic, heat, parity, the burst budget, the cap. Over the bar the reserve opens only
# for load the host's own placements cannot explain: after crediting every placed unit LAB_WORKER_COST_CORES, load1 must still be
# over the bar (h201 at 84.5 with nine placed: 66.5 > 48), so a host its own lanes overload (h202 on 2026-10-02: load1 59 with 20
# placed) stays refused (OMN-20366). A worker's reserve counts every unit placed on the host; a lane's counts only lanes
# (``Reading.lanes_placed``, from ``lane_counts``): at the refusals above h201 already held 3-16 placed units, so a reserve counted
# against every unit was spent before a cause lane arrived (a replay of the 65 recorded readings admitted none). the lab runner, which
# places the test suites and builds that make the load, judges without the reserve (``Reading.without_reserve``).
ADMIT_RESERVED_SLOTS = 2
# A lane's placement marker carries the remote-lane runner's epoch-second id (``onex_remote_lane.place``: ``int(time.time())``) and a
# worker's carries its lease id, a small sequence number, so a marker id at or over this is a lane's (``lane_counts``).
LANE_MARKER_MIN_ID = 1_000_000_000
# A Mac takes a lane only while it holds the macOS Local Network grant: a process the grant does not cover gets
# EHOSTUNREACH ("No route to host") for every LAN address, so the lane's git fetch from a lab mirror, its lab model
# endpoint and the bus broker all fail on it. The Mac probe tests the grant with a TCP connect from its python3
# back to the address the probe's own ssh session came from (``SSH_CLIENT``): ``ok`` (connected, refused or timed
# out: the route exists), ``denied`` (EHOSTUNREACH or ENETUNREACH), ``unknown`` (no ssh peer or no python3). Only
# ``ok`` qualifies.
LAN_GRANT_OK = "ok"
# The probe's WORKDIR line when the lane workdir took a write (OMN-20531).
WORKDIR_OK = "ok"
# Most lanes the launching host holds at once when its row in the pool table declares no ``cap`` (OMN-17427, operator
# RULING 2026-10-02T10:59:04Z: "I want the entire lab being used", the launching Mac is a placement after the lab
# hosts with a free slot). A row's own ``cap`` replaces it, so the number lives in the one host table.
LOCAL_LANE_SLOT_CAP = 6
# Load a fresh worker (``Reading.fresh``) is charged before the one-minute average reflects it; a settled one is in load already.
WORKER_RESIDUAL_CORES = 1.0
# The burst budget (OMN-19998). A worker placed under PLACEMENT_SETTLE_S ago is in neither the one-minute load average nor the
# probe's available memory, so the next worker's probe still reads a healthy host and only ``cap`` bounds the burst: on
# 2026-10-11 06:42-06:46Z five fix workers took h201 inside one tick and it rose to load1 94-101 on 32 cores. A host takes at most
# PLACEMENT_BURST_PER_HOST workers inside one settle window (``Reading.fresh``, counted from the placement markers' age): a host
# with that many fresh workers has no slot, so the next worker takes another host or holds its lease and probes again. The window is two time constants of the
# one-minute average (86 percent of a new worker's load is in it by then).
PLACEMENT_SETTLE_S = 120.0
PLACEMENT_BURST_PER_HOST = 3
# Memory each further worker needs above the floor, and the floor itself.
WORKER_MEM_GB = 2.0
MIN_FREE_GB = 4.0
# Load per core a host may carry and still take work: a lab host up to its core count, the controller
# host (the interactive Mac) only below three quarters of it.
LAB_LOAD_PER_CORE = 1.0
LOCAL_LOAD_PER_CORE = 0.75
# At or under this load per core, with nothing placed, a host takes at least one worker.
IDLE_LOAD_PER_CORE = 0.5
PROBE_TIMEOUT_S = 20.0
# The probe on the controller's own host runs under launchd's Background throttle (ProcessType Background), which
# made it take 25 s where the foreground took 7 s: at 20 s the launching host read UNREADABLE and left the pool
# (OMN-19998). It is not an ssh round trip, so it gets its own bound. Measured on 2026-11-01 at load 56 on 24 cores
# (OMN-21733): 20 s in the foreground and 139 s under the Background class, so at 60 s the factory floor drew h200 unread.
LOCAL_PROBE_TIMEOUT_S = 180.0
SSH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ServerAliveInterval=30",
)
CONTRACT_FILE = Path(__file__).resolve().parent.parent / "contract.yaml"


def _engine_policy() -> dict[str, Any]:
    """The worker engine policy: the ``landing_engine_policy`` block of this node's contract (OMN-19998)."""
    raw = yaml.safe_load(CONTRACT_FILE.read_text(encoding="utf-8"))
    return dict(raw["landing_engine_policy"])


CLAUDE_ENGINES = tuple(_engine_policy()["claude_engines"])
# A long-lived Claude token (``claude setup-token``) on a lab host, read by the probe and the remote
# engine. Relative to the remote $HOME; mode 600; written by the operator, never by this code.
CLAUDE_TOKEN_FILE = ".config/omni/claude-oauth-token"
# The token line (``TOKEN_EXPORT``, defined after ``PROBE_BOUND_SH``) is shared by the placement probe's limit re-probe, the
# remote-lane runner and the landing worker's remote engine step.
# The Codex worker engine (OMN-20531, operator RULING 2026-10-04T18:17:13Z). ``codex_high`` was retired as a worker engine on
# 2026-09-29 (OMN-17427); the ruling above reinstates it for mechanical brief classes only. It is the node's own enum value,
# never a ladder rung: the controller still sends the Claude ladder, and the worker swaps the first rung of a mechanical class
# for this engine when a codex host has room (``landing_facts.codex_first``). It runs on a lab host only, never on the
# launching host, so the Codex CLI is started by the remote script and not by this machine's own worker process.
CODEX_ENGINE = "codex_high"
# RULING 2026-10-26T11:31:48Z: old Opus leases run as Sonnet too.
RETIRED_ENGINES: dict[str, str] = dict(_engine_policy()["retired_engines"])

# The launching host is always a pool row: the table's own local row when it carries one, else this one. Every other
# row comes from the lab host table (``lab_hosts.hosts``); the package ships no host.
LOCAL_POOL_ROW: dict[str, Any] = {
    "name": "mac",
    "local": True,
    "engines": list(CLAUDE_ENGINES),
    "gh": True,
}
EXIT_NO_POOL = 75  # a pool read with no host table and no LANDING_POOL_HOSTS: the next tick retries


def resolve_engine(engine: str) -> str:
    """The engine a worker actually runs as: a retired engine resolves to its Claude replacement."""
    return RETIRED_ENGINES.get(engine, engine)


def registry_root_candidates(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The registry root candidates to try on a host, in order: the ``lab_hosts.registry_root_candidates`` overlay key.

    Raises :class:`NodeOverlayError` naming the key when neither the overlay nor ``ONEX_REGISTRY_ROOT`` supplies one."""
    environ = dict(_env(env))
    environ.pop(REGISTRY_ROOT_ENV, None)
    try:
        return tuple(
            resolve_overlay_key("lab_hosts.registry_root_candidates", environ=environ)
        )
    except NodeOverlayError:
        return (
            str(
                resolve_overlay_key(
                    "lab_hosts.registry_root_candidates", environ=_env(env)
                )
            ),
        )


def registry_roots_sh(env: Mapping[str, str] | None = None) -> str:
    """The shell word list a probe loops over to find the registry root: the host's own ``ONEX_REGISTRY_ROOT``, then each candidate."""
    return " ".join(
        [
            '"${ONEX_REGISTRY_ROOT:-}"',
            *(f'"{c}"' for c in registry_root_candidates(env)),
        ]
    )


def bind_registry_roots(script: str, env: Mapping[str, str] | None = None) -> str:
    """``script`` with its ``__REGISTRY_ROOTS__`` placeholders bound to this deployment's candidates."""
    return (
        script.replace("__REGISTRY_ROOTS__", registry_roots_sh(env))
        if "__REGISTRY_ROOTS__" in script
        else script
    )


Capture = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Host:
    name: str
    target: str | None
    engines: frozenset[str]
    # None: derived from the host's cores (``cores // WORKER_COST_CORES``, at least 1).
    cap: int | None
    gh: bool
    # Load per core added for ranking only (the shared table's ``penalty``).
    penalty: float = 0.0
    # True: engines and gh are read by the probe's LOGIN line, not declared.
    engines_live: bool = False
    # The shared table's ``ledger_bus_lane`` and ``ledger_bus_python`` (OMN-20281): the declared bus lane
    # a lane on this host writes the work ledger through, and the interpreter (relative to the host's own
    # registry root) that has omnimarket's work_ledger_bus package. None: the host declares no ledger bus.
    ledger_bus_lane: str | None = None
    ledger_bus_python: str | None = None
    # The shared table's ``tenant_id`` (OMN-18829): the tenant a lane on this host delegates as, exported
    # to the lane as ONEX_TENANT_ID. None: the host declares none and its install identity applies.
    tenant_id: str | None = None
    # The shared table's ``parity_clones`` (OMN-20266): registry clones this host must carry at origin tip with their pre-commit
    # hook installed, whatever unit is placed. A deployment fact of the private table; shipped code names none.
    parity_clones: tuple[str, ...] = ()
    # The shared table's ``reserve`` (OMN-20864): units this host admits while its load bar refuses. None: the default,
    # ``ADMIT_RESERVED_SLOTS`` on a lab host and none on the launching host.
    reserve: int | None = None
    # The shared table's ``codex`` column on this host's own row (OMN-20864). It matters for the launching host only: a lab
    # host's Codex eligibility is the table's ``codex: true`` set (``codex_placement.codex_hosts``), while the launching host
    # takes a Codex LANE only when its own row marks ``codex: true`` (operator RULING 2026-10-11T16:48:49Z, the Mac Studio).
    codex: bool = False
    # The shared table's ``unit_cores`` (OMN-20864): the cores the slot arithmetic charges each further unit on this host.
    # None: ``WORKER_COST_CORES`` on the launching host, ``LAB_WORKER_COST_CORES`` on a lab host. It changes the arithmetic
    # only: every admission bar (CPU idle, load, memory, pressure, swap, heat, parity, burst budget) still refuses.
    unit_cores: float | None = None
    # The shared table's ``max_cpu_c`` (OMN-20769): this host's own CPU temperature bar, used only when it is LOWER than
    # ``ADMIT_MAX_CPU_C``; a row can tighten the heat bar, never relax it. h202 carries 80 (operator RULING
    # 2026-10-11T19:42:54Z returned it to placement with its temperature watched). None: ``ADMIT_MAX_CPU_C``.
    max_cpu_c: float | None = None
    # The shared table's ``min_free_gb`` (OMN-17232): this host's own memory admission bar in GB, replacing ``ADMIT_MIN_FREE_GB``
    # for the units it takes (a 7.8 GB laptop can never show 10 GB). None: the shared floor.
    min_free_gb: float | None = None
    # The shared table's ``parity_python`` (OMN-20266): the interpreter, relative to the host's own registry root (as
    # ``ledger_bus_python``), the pytest parity check imports with; a deployment fact of the private table. None: the host's
    # ``python3`` on PATH. A Mac's unversioned brew ``python3`` follows brew's newest release (3.14 on h200, 2026-10-13, with no
    # pytest), while the dispatch venv, built on the registry's canonical interpreter, carries the locked pytest units run with.
    parity_python: str | None = None
    # Per-host cutover policy from the existing pool table; enabled after transport migration.
    gh_absent: bool = False
    # Optional absolute lab-work root from the host table; no host path in code.
    lab_work_root: str | None = None
    report_retention_days: int = 7
    # The shared table's ``fallback_targets`` (OMN-17427): further ssh destinations for the same machine, tried in order after
    # ``target``. Empty: ``target`` is the only destination and nothing is probed (``resolve_target``).
    fallback_targets: tuple[str, ...] = ()

    @property
    def cpu_bar_c(self) -> float:
        """The CPU temperature over which this host takes no new unit: the row's ``max_cpu_c`` when lower, else the default."""
        if self.max_cpu_c is not None and 0 < self.max_cpu_c < ADMIT_MAX_CPU_C:
            return self.max_cpu_c
        return ADMIT_MAX_CPU_C

    @property
    def local(self) -> bool:
        return self.target is None


# A host whose ``~/.claude/settings.json`` does not set ``includeCoAuthoredBy`` to false makes every worker's commit carry
# a Claude co-author trailer, and the no-ai-coauthor-trailer gate fails the PR (h202, 2026-09-30: 15 of the last 24 landing
# workers, five OCC companions red). The probe's COAUTHOR line reports the setting; the two functions below turn it into a
# named refusal. They are kept apart from the admission bar's memory and swap rules on purpose.
COAUTHOR_SETTING = "includeCoAuthoredBy"
COAUTHOR_OK = "false"
COAUTHOR_UNREPORTED = "unreported"


def coauthor_state(text: str) -> str:
    """The probe's ``COAUTHOR <state>`` line: ``false`` (the setting is false), ``unset``, ``other`` (present, not
    false), ``nofile`` or ``unreadable``; ``unreported`` when the probe printed no such line."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "COAUTHOR":
            return parts[1]
    return COAUTHOR_UNREPORTED


def coauthor_refusal(state: str | None) -> str | None:
    """The named refusal for a host whose settings do not disable the co-author trailer, else None. ``None`` (a
    Reading built without a probe) is not judged; every state but ``false`` is refused, an unreadable or unreported
    one included."""
    if state is None or state == COAUTHOR_OK:
        return None
    return (
        f"COAUTHOR-TRAILER-NOT-DISABLED: ~/.claude/settings.json {COAUTHOR_SETTING} is {state}, "
        f"not false, so a commit here carries a Claude co-author trailer"
    )


# ---------------------------------------------------------------------------------------------------
# HOST PARITY (OMN-20266). What a host carries, read by the probe, judged against what a unit needs.
# ---------------------------------------------------------------------------------------------------

# The plugins of this repository, whose installed file tree must equal the tree of the revision the install says it shipped
# (a hot-patched runner in the plugin cache ran lanes on 2026-10-09, OMN-20266). The repository that holds their marketplace
# manifest is looked up on the host by this directory name when the marketplace clone does not hold the revision.
PARITY_PLUGINS = ("omni", "onex-overlays")


def parity_plugin_repo(env: Mapping[str, str] | None = None) -> str:
    """The plugin source repository's directory name: the ``repos.plugin_source`` overlay key."""
    return str(resolve_overlay_key("repos.plugin_source", environ=_env(env)))


PARITY_FILE = "parity.json"
# A host record no pool read has replaced for this long is not current: the throughput tick reads the pool every ten minutes,
# so three missed reads. queue-status prints such a record as STALE with its age (OMN-17427).
PARITY_STALE_S = 30 * 60
PARITY_DEFAULT_PYTHON = "python3"
# A row's ``parity_python`` is a path under the host's registry root that reaches the probe's shell, so only plain path segments
# are taken (no ``..``, no leading slash, no shell syntax).
PARITY_PYTHON_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._+-]*(/[A-Za-z0-9._+-]+)*$")
# The oldest omnimarket revision a dispatch venv may hold (OMN-20864): the plugin's declared floor, never the launching host's live
# revision, which moved with every omnimarket merge and refused every lab host until a hand reconcile.
DISPATCH_VENV_FLOOR_FILE = (
    Path(__file__).resolve().parent.parent / "dispatch_venv_floor.json"
)
VENV_FLOOR_GIT_TIMEOUT_S = 10
CLONE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHORT = 12


def _short(value: str | None) -> str:
    return (value or "none")[:SHORT]


# The host's GitHub credential verdict (OMN-21016). A good verdict is read from the host's cache for GH_FRESH_MIN minutes, then
# checked live; through a GitHub 5xx or a timeout the last good one is kept for GH_FALLBACK_MIN minutes, never past a definite
# logged-out answer. The cache is the mtime of ``$HOME/.cache/omni/<GH_OK_FILE>`` on the host, so every caller that probes the
# host (the landing controller, a remote lane, the lab runner) shares one check.
GH_FRESH_MIN = 15
GH_FALLBACK_MIN = 360
GH_CHECK_TIMEOUT_S = 5
GH_OK_FILE = "gh-credential-ok"
# Per-host fallback episodes, in the placement directory: the FRICTION row is written once per episode, and a refused append is
# retried no sooner than GH_FRICTION_RETRY_S later.
GH_FALLBACK_FILE = "gh-credential-fallback.json"
GH_FRICTION_RETRY_S = 1800
GH_FRICTION_TICKET = "OMN-21016"
GH_VERDICTS = frozenset({"ok", "expired", "unreachable", "missing"})


@dataclass(frozen=True)
class GhCredential:
    """The probe's GHAUTH line: ``verdict`` ok, expired (GitHub answered 401, or no credential is configured), unreachable (no
    definite answer and no good verdict newer than GH_FALLBACK_MIN) or missing (no gh); ``source`` live, cached or fallback;
    the age of the last good verdict in seconds (None: none); ``detail`` the live check's answer (``http=<code>``, ``timeout``,
    ``no-response``, ``no-credential``) or ``-``."""

    verdict: str
    source: str
    last_ok_age_s: int | None
    detail: str

    @property
    def refusal(self) -> str | None:
        if self.verdict == "ok":
            return None
        if self.verdict == "expired":
            return f"CREDENTIAL-EXPIRED: GitHub answered that the gh credential is logged out or invalid ({self.detail})"
        if self.verdict == "unreachable":
            return (
                f"GITHUB-UNREACHABLE: the gh credential check got no answer from GitHub ({self.detail}) and no good "
                f"verdict is newer than {GH_FALLBACK_MIN // 60}h"
            )
        return "NO-GH: gh is not installed"


def parse_gh_credential(text: str) -> GhCredential | None:
    """The last GHAUTH line of a probe, or None (an older probe, or a line of another shape)."""
    for line in reversed(text.splitlines()):
        parts = line.split()
        if len(parts) == 5 and parts[0] == "GHAUTH" and parts[1] in GH_VERDICTS:
            age = int(parts[3]) if parts[3].isdigit() else None
            return GhCredential(parts[1], parts[2], age, parts[4])
    return None


@dataclass(frozen=True)
class ParityNeeds:
    """What a unit needs of its host beyond the clones the host's row names and the installed plugins, which every unit needs."""

    gh: bool = False
    pytest: bool = False
    venv: bool = False
    # The unit writes the work ledger through the host's bus lane, so the lane's broker host must resolve (2026-10-13: h101 lost
    # Tailscale DNS while ``tailscale status`` read healthy).
    bus: bool = False
    # The launching Mac's git transport needs gh on the lane PATH; lab hosts require its absence instead.
    gh_path: bool = False
    # The unit commits (``hook-preflight`` runs ``pre-commit`` on the staged files) or runs ``uvx`` tools: ``pre-commit`` must
    # resolve on the PATH lanes run with and the pre-commit cache must hold no entry that cannot run (2026-10-17: h202 had neither
    # ``pre-commit`` nor ``uvx`` on the login PATH, lab-run exit 127, and a cache entry for a deleted hook repo, hook-preflight exit 3).
    hooks: bool = False
    # Registry clones the unit is built from beyond the ones the host's row names (the repository of a lane or of a PR). A host
    # without one fails parity for this unit only (2026-10-17: no plugin source clone on a host, lane blocked); read_pool probes
    # them through ``need_clones``.
    clones: tuple[str, ...] = ()
    # The runner provisions what is repairable on the host it picks (OMN-19998): a missing clone, a clone behind its origin tip
    # and a clone without its pre-commit hook are not refusals for this unit, they are ``HostParity.repairs``. Only a unit whose
    # runner has a provisioning step sets this; a landing worker keeps refusing a host on them.
    provision: bool = False
    # The unit commits or opens a pull request, so git must know who the lane account is (``git_identity``, OMN-17427).
    commit: bool = False
    # Host capabilities the unit uses (OMN-17427, ``host_capability.CAPABILITIES``): one the host's PARITY-CAP line reads missing refuses it.
    capabilities: frozenset[str] = frozenset()


PARITY_DEFAULT_NEEDS = ParityNeeds()
PARITY_EVERYTHING = ParityNeeds(
    gh=True,
    pytest=True,
    venv=True,
    bus=True,
    gh_path=True,
    hooks=True,
    commit=True,
    capabilities=frozenset(host_capability.CAPABILITIES),
)

# The directories the runner's PR head fetch puts before the ssh session's PATH, which on a non-interactive shell holds neither a
# Homebrew nor a user install. The probe resolves gh with the same export (``fetch_path_export``), so the two cannot disagree.
FETCH_PATH_DIRS = "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"


def fetch_path_export(tail: str = "$PATH") -> str:
    """The shell line that gives a non-interactive script the PATH a lab host's gh is found on; ``tail`` is the PATH it extends."""
    return f'export PATH="{FETCH_PATH_DIRS}:{tail}"'


# Registry clones that sit beside the registry root, not under it (``clone_path`` in the probe): the private repositories the
# operator keeps next to the registry root. A missing clone is provisioned beside the root when its name is here, else under it.


def beside_root_clones(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The directory names of the clones kept beside the registry root: ``repos.beside_root`` roles through the ``repos`` overlay."""
    environ = _env(env)
    return frozenset(
        str(resolve_overlay_key(f"repos.{role}", environ=environ))
        for role in resolve_overlay_key("repos.beside_root", environ=environ)
    )


# The repairs a parity failure can carry (``ParityFailure.repair``): clone the repository from its organisation, fast-forward a
# clean clone to its origin tip, install the clone's pre-commit hook. Listed in the order they are carried out.
REPAIR_CLONE = "clone"
REPAIR_FAST_FORWARD = "fast-forward"
REPAIR_HOOK = "hook"
REPAIR_KINDS = (REPAIR_CLONE, REPAIR_FAST_FORWARD, REPAIR_HOOK)


@dataclass(frozen=True)
class ParityFailure:
    item: str
    reason: str
    # One of REPAIR_KINDS when the runner can fix this on the host before the unit starts; None: only a refusal fits.
    repair: str | None = None


@dataclass(frozen=True)
class CloneParity:
    name: str
    present: bool
    head: str | None
    tip: str | None
    # An executable pre-commit hook is installed (core.hooksPath honoured). None: the clone is missing.
    hook: bool | None


@dataclass(frozen=True)
class PluginParity:
    name: str
    # ``ok`` (the installed tree equals the shipped revision's, or each file that differs matches a revision merged after it: an
    # adopted file); ``hotpatched`` (every other difference is a hot-patch made the sanctioned way, OMN-20864: the file's oldest
    # ``.bak.<UTC stamp>`` backup beside it holds the shipped or a merged content); ``modified`` (a file matches no merged
    # revision and is no sanctioned hot-patch); ``unverifiable`` (no shipped tree to compare). Backups and ``tests/`` files are
    # never loaded by a unit, so they are left out of the comparison.
    state: str
    installed: str
    shipped: str
    revision: str
    detail: str


@dataclass(frozen=True)
class BusHost:
    """The probe's PARITY-BUS line: the broker host of the host's ledger bus lane (read from the registry's
    ``omnimarket/config/ci_bus_lanes.yaml`` on the host) and whether ``getaddrinfo`` resolved it there. ``verdict`` is ``ok``,
    ``unresolved`` (the lookup failed or timed out) or ``unknown`` (no broker host to resolve: the file or the lane is absent, or the
    broker is not a host:port); only ``unresolved`` fails."""

    lane: str
    host: str
    verdict: str
    detail: str


@dataclass(frozen=True)
class HookTools:
    """The probe's PARITY-HOOKTOOLS line: where ``pre-commit`` and ``uvx`` resolve with the PATH lanes run with
    (``fetch_path_export`` over the ssh session's own); None: neither resolves."""

    pre_commit: str | None
    uvx: str | None


@dataclass(frozen=True)
class PrecommitCache:
    """The probe's PARITY-PCCACHE line: ``ok`` (every entry of pre-commit's cache database has its checkout, with the hook
    manifest, and every local hook repository it names exists), ``bad`` (an entry that cannot run: ``detail`` names the
    first) or ``none`` (no cache database yet, nothing to be wrong)."""

    verdict: str
    detail: str


@dataclass(frozen=True)
class PluginAge:
    """The probe's PARITY-PLUGIN-AGE line for an installed plugin: how far its shipped revision is behind the plugin
    repository's merged tip. ``behind`` counts the commits after the shipped revision that touch the plugin's directory,
    ``tree_equal`` is whether the installed tree already equals the tip's (adopted files), ``age_h`` the age in hours of the
    oldest of those commits. None: the tip or the log could not be read."""

    name: str
    revision: str
    tip: str
    behind: int | None
    tree_equal: bool | None
    age_h: int | None


# An installed plugin whose shipped revision is behind the merged tip by a commit that touches the plugin, with the tip's tree not
# installed, fails parity once that commit is this old (h101 ran 4 days behind until 2026-10-17T08:13Z). A day lets the version bump
# and the host's own plugin update land before a host is refused.
PLUGIN_STALE_HOURS = 24


@dataclass(frozen=True)
class HostParity:
    clones: tuple[CloneParity, ...] = ()
    venv_omnimarket: str | None = None
    # Observation only, never consumed by failures() or admission.
    fseventsd_rss_kib: int | None = None
    # The launching host's dispatch venv omnimarket revision; None: not known, so the venv is not judged.
    reference_omnimarket: str | None = None
    gh: bool | None = None
    gh_absent: bool = False
    pytest: bool | None = None
    # The interpreter the pytest check imported with (``Host.parity_python``, else ``python3``).
    pytest_python: str = PARITY_DEFAULT_PYTHON
    plugins: tuple[PluginParity, ...] = ()
    error: str | None = None
    # GHAUTH is used only on the launching Mac.
    gh_credential: GhCredential | None = None
    # The launching host's own record (OMN-20864): its installed plugin is the session's own, so a sanctioned hot-patch there is
    # compatible. A lab host's hot-patch is still refused.
    own_host: bool = False
    # The plugin's dispatch venv floor (``DISPATCH_VENV_FLOOR_FILE``) and whether ``venv_omnimarket`` is at or after it on
    # omnimarket's merged history, read by ``venv_floor_verdict`` on the launching host. None: not known (no floor, no clone, a
    # commit the clone does not hold), so the venv is judged against the launching host's as before.
    venv_floor: str | None = None
    venv_floor_ok: bool | None = None
    # The probe's PARITY-BUS line; None: the host's row declares no ledger bus lane, or an older probe.
    bus: BusHost | None = None
    # PARITY-GHPATH resolves gh on the lane PATH; None: an older probe.
    gh_path: str | None = None
    gh_path_ok: bool | None = None
    # The probe's PARITY-HOOKTOOLS, PARITY-PCCACHE and PARITY-PLUGIN-AGE lines; None / empty: an older probe.
    hook_tools: HookTools | None = None
    precommit_cache: PrecommitCache | None = None
    plugin_ages: tuple[PluginAge, ...] = ()
    # Clones the probe read beyond the host's row (the plugin repository, the repositories units asked for): a missing or
    # stale one fails parity only for a unit whose ``ParityNeeds.clones`` names it.
    optional_clones: frozenset[str] = frozenset()
    # The probe's PARITY-CORE-WORKTREE lines; empty: no clone carries a stray core.worktree (or an older probe).
    core_worktrees: tuple[tuple[str, str], ...] = ()
    # The probe's PARITY-GITIDENT line, (user.name set, user.email set); None: an older probe.
    git_identity: tuple[bool, bool] | None = None
    # The probe's PARITY-CAP lines (``host_capability.ModelHostCapability``); empty: an older probe, which refuses nothing.
    capabilities: tuple[Any, ...] = ()

    def failures(
        self, needs: ParityNeeds = PARITY_DEFAULT_NEEDS
    ) -> list[ParityFailure]:
        """Refusals for ``needs``, excluding repairable failures when provisioning."""
        found = self._failures(needs)
        return [f for f in found if f.repair is None] if needs.provision else found

    def repairs(self, needs: ParityNeeds = PARITY_DEFAULT_NEEDS) -> list[ParityFailure]:
        """Ordered repairs for provisioning units; none after a failed probe."""
        if not needs.provision or self.error:
            return []
        found = [f for f in self._failures(needs) if f.repair is not None]
        return sorted(found, key=lambda f: REPAIR_KINDS.index(f.repair or REPAIR_HOOK))

    def _failures(self, needs: ParityNeeds) -> list[ParityFailure]:
        out: list[ParityFailure] = []
        if self.error:
            out.append(ParityFailure("parity-probe", self.error))
        for c in self.clones:
            if c.name in self.optional_clones and c.name not in needs.clones:
                continue
            if not c.present:
                out.append(
                    ParityFailure(
                        f"clone:{c.name}",
                        f"clone {c.name} is missing from the registry root",
                        REPAIR_CLONE,
                    )
                )
                continue
            if c.tip is None:
                out.append(
                    ParityFailure(
                        f"clone:{c.name}",
                        f"clone {c.name} has no origin tip to compare its HEAD with",
                    )
                )
            elif c.head != c.tip:
                out.append(
                    ParityFailure(
                        f"clone:{c.name}",
                        f"clone {c.name} is at {_short(c.head)}, not at origin tip {_short(c.tip)}",
                        REPAIR_FAST_FORWARD,
                    )
                )
            if c.hook is False:
                out.append(
                    ParityFailure(
                        f"hook:{c.name}",
                        f"clone {c.name} has no executable pre-commit hook installed",
                        REPAIR_HOOK,
                    )
                )
        for name, path in self.core_worktrees:
            if name not in self.optional_clones or name in needs.clones:
                out.append(
                    ParityFailure(*_clone_parity.core_worktree_failure(name, path))
                )
        if needs.venv:
            out.extend(self._venv_failures())
        if self.gh_absent:
            if self.gh_path_ok is not False:
                out.append(
                    ParityFailure(
                        "gh-absent",
                        "lab hosts require gh absent; use onex scm through the gateway",
                    )
                )
        elif needs.gh and self.gh_credential is not None:
            if self.gh_credential.refusal:
                out.append(ParityFailure("gh-auth", self.gh_credential.refusal))
        elif needs.gh and self.gh is False:
            out.append(ParityFailure("gh-auth", "gh auth status fails"))
        if not self.gh_absent and needs.gh_path and self.gh_path_ok is False:
            out.append(
                ParityFailure(
                    "gh-path",
                    "gh is not on the PATH the PR head fetch runs with, so its credential helper and a push fail with "
                    "`gh: command not found`",
                )
            )
        if needs.bus and self.bus is not None and self.bus.verdict == "unresolved":
            out.append(
                ParityFailure(
                    "bus-host",
                    f"BUS-HOST-UNRESOLVED: the broker host {self.bus.host} of bus lane {self.bus.lane} does not "
                    f"resolve on the host ({self.bus.detail})",
                )
            )
        if needs.pytest and self.pytest is False:
            out.append(
                ParityFailure("pytest", f"{self.pytest_python} cannot import pytest")
            )
        if needs.hooks:
            if self.hook_tools is not None and self.hook_tools.pre_commit is None:
                out.append(
                    ParityFailure(
                        "hook-toolchain",
                        "pre-commit is not on the PATH lanes run with, so hook-preflight and a commit fail with "
                        f"`pre-commit: command not found` (uvx: {self.hook_tools.uvx or 'not found'})",
                    )
                )
            if (
                self.precommit_cache is not None
                and self.precommit_cache.verdict == "bad"
            ):
                out.append(
                    ParityFailure(
                        "precommit-cache",
                        f"the pre-commit cache holds an entry that cannot run, so hook-preflight stops on it: "
                        f"{self.precommit_cache.detail}",
                    )
                )
        if (
            needs.commit
            and self.git_identity
            and (identity := _clone_parity.git_identity_failure(*self.git_identity))
        ):
            out.append(ParityFailure(*identity))
        for p in self.plugins:
            if p.state == "ok" or (p.state == "hotpatched" and self.own_host):
                continue
            what = {
                "modified": "differs from",
                "hotpatched": "carries hot-patches, on a host other than the launching host, of",
            }.get(p.state, "cannot be checked against")
            out.append(
                ParityFailure(
                    f"plugin:{p.name}",
                    f"installed plugin {p.name} {what} its shipped revision {_short(p.revision)}: {p.detail}",
                )
            )
        for a in self.plugin_ages:
            if (
                a.behind
                and not a.tree_equal
                and a.age_h is not None
                and a.age_h >= PLUGIN_STALE_HOURS
            ):
                out.append(
                    ParityFailure(
                        f"plugin-stale:{a.name}",
                        f"installed plugin {a.name} ships {_short(a.revision)}, {a.behind} commit(s) behind the "
                        f"merged tip {_short(a.tip)}, the oldest {a.age_h} h ago (bound {PLUGIN_STALE_HOURS} h)",
                    )
                )
        out.extend(
            ParityFailure(item, reason)
            for item, reason in host_capability.capability_failures(
                self.capabilities, needs.capabilities
            )
        )
        return out

    def _venv_failures(self) -> list[ParityFailure]:
        """The dispatch venv against the plugin's floor (OMN-20864): at or after it on the merged history passes; below it, or
        off the merged history, fails. Where the floor cannot be judged, the venv must equal the launching host's (OMN-20266)."""
        have = (
            "has no omnimarket commit"
            if self.venv_omnimarket is None
            else f"holds omnimarket {_short(self.venv_omnimarket)}"
        )
        if self.venv_floor_ok is True:
            return []
        if self.venv_floor_ok is False:
            return [
                ParityFailure(
                    "dispatch-venv",
                    f"the dispatch venv {have}, which is not at or after the plugin's omnimarket "
                    f"floor {_short(self.venv_floor)} on the merged history",
                )
            ]
        if (
            self.reference_omnimarket is not None
            and self.venv_omnimarket != self.reference_omnimarket
        ):
            return [
                ParityFailure(
                    "dispatch-venv",
                    f"the dispatch venv {have}, the launching host's is {_short(self.reference_omnimarket)}",
                )
            ]
        return []

    def record(self, needs: ParityNeeds = PARITY_EVERYTHING) -> dict[str, Any]:
        """The typed record, JSON-able: the facts as read and the failures against ``needs``."""
        return {
            "clones": [asdict(c) for c in self.clones],
            "venv_omnimarket": self.venv_omnimarket,
            "fseventsd_rss_kib": self.fseventsd_rss_kib,
            "core_worktrees": [list(w) for w in self.core_worktrees],
            "reference_omnimarket": self.reference_omnimarket,
            "gh": self.gh,
            "gh_absent": self.gh_absent,
            "gh_credential": None
            if self.gh_credential is None
            else asdict(self.gh_credential),
            "gh_path": self.gh_path,
            "gh_path_ok": self.gh_path_ok,
            "bus": None if self.bus is None else asdict(self.bus),
            "pytest": self.pytest,
            "pytest_python": self.pytest_python,
            "own_host": self.own_host,
            "venv_floor": self.venv_floor,
            "venv_floor_ok": self.venv_floor_ok,
            "plugins": [asdict(p) for p in self.plugins],
            "plugin_ages": [asdict(a) for a in self.plugin_ages],
            "hook_tools": None if self.hook_tools is None else asdict(self.hook_tools),
            "precommit_cache": None
            if self.precommit_cache is None
            else asdict(self.precommit_cache),
            "error": self.error,
            "git_identity": self.git_identity and list(self.git_identity),
            "capabilities": [asdict(c) for c in self.capabilities],
            "failures": [asdict(f) for f in self.failures(needs)],
            "repairs": [asdict(f) for f in self.repairs(needs)],
        }


@dataclass(frozen=True)
class Reading:
    host: Host
    cores: int = 0
    load1: float = 0.0
    mem_avail_gb: float = 0.0
    placed: int = 0
    error: str | None = None
    # Of ``placed``, the workers placed under ``PLACEMENT_SETTLE_S`` ago (``fresh_counts``): not yet in load1 or ``mem_avail_gb``,
    # so the one reading cannot tell the next placement about them. At most ``PLACEMENT_BURST_PER_HOST`` per host.
    fresh: int = 0
    # Linux only: cores actually busy on CPU over a one-second sample (iowait counts as idle). The
    # one-minute load average counts tasks in uninterruptible IO wait, so a host with 60% idle CPU
    # and heavy disk traffic reads load 40 on 32 cores (h202, 2026-09-30). None: not measured.
    busy_cores: float | None = None
    # Linux only: the fraction of swap in use (the probe's SWAP line). None: not measured, no swap, or macOS.
    swap_used_frac: float | None = None
    # macOS only: kern.memorystatus_vm_pressure_level (1 normal, 2 warn, 4 critical; the probe's PRESSURE line).
    mem_pressure: int | None = None
    # macOS only: the kernel's percent of memory free (kern.memorystatus_level, what ``memory_pressure`` prints as the system-wide
    # free percentage) and the machine's memory in GB (hw.memsize); the probe's MEMFREE line (OMN-19998). None: not read.
    mem_free_pct: float | None = None
    mem_total_gb: float | None = None
    # Linux only: PSI memory ``some`` avg60 in percent (the probe's MEMPSI line). None: no PSI, or macOS.
    mem_psi_avg60: float | None = None
    # The quieter of the probe's two one-second SWAPIO samples, KB/s. None: not read.
    swap_in_kb_s: float | None = None
    swap_out_kb_s: float | None = None
    # The probe's COAUTHOR state (``coauthor_state``) for ``~/.claude/settings.json``. None: not measured.
    coauthor: str | None = None
    # Percent of the placing host's pings to this host that were lost (``parse_link_loss``). None: not pinged (the
    # local host) or the ping printed no summary.
    link_loss_pct: float | None = None
    # The earlier reads of that loss, newest first (``read_pool`` with a ``link_history``); empty: one lossy read cannot bench the host.
    link_loss_prior: tuple[float, ...] = ()
    # True for a unit delivered over the event bus (lab-work), which no ssh link carries: no link bar judges it.
    bus_delivered: bool = False
    # macOS only: the probe's LANGRANT state (``ok``, ``denied``, ``unknown``). None: not a Mac, or not measured.
    lan_grant: str | None = None
    # Linux only: the probe's THERMAL line, the average of two reads a second apart, in degrees C: the CPU package
    # (k10temp Tctl or coretemp Package id) and the hottest spd5118 DIMM. None: the host has no such sensor.
    cpu_temp_c: float | None = None
    dimm_temp_c: float | None = None
    # macOS only: percent of CPU idle over the probe's second ``top`` sample (the CPUIDLE line). None: not a Mac, or not read.
    cpu_idle_pct: float | None = None
    # The preflight (OMN-20531). ``workdir``: the probe's WORKDIR line, ``ok`` or ``fail`` for a write test of the lane workdir
    # (``$HOME/lab-run``) on a lab host. ``codex_login``: the CODEXLOGIN line, ``codex login status`` passed. ``codex_sandbox``:
    # the CODEXSANDBOX line (``ok``, ``fail`` or ``unknown``), a cached smoke of ``codex exec -s workspace-write`` writing the
    # workspace and an ``--add-dir`` directory (RC-7: 15 lanes were placed on an h101 whose Codex sandbox could not write the lane
    # workdir). None: not measured (a Reading built without the probe, or the launching host).
    workdir: str | None = None
    codex_login: bool | None = None
    codex_sandbox: str | None = None
    # The probe's CODEXSANDBOXWHY line: why the lane-shaped sandbox probe failed (``rc=<n>`` and the tail of its log). None: it did not fail.
    codex_sandbox_why: str | None = None
    # The probe's CLAUDEWHY line (OMN-20864): how the Claude login was read. ``host-login`` (``claude auth status`` read the host's
    # own login), ``call-ok`` (a real ``claude -p`` answered within six hours), ``unchecked`` (no call has finished yet; the probe
    # started one), ``call-failed:<what it answered>`` (usage-limit, auth, no-answer, error) or ``no-claude`` (no claude binary).
    # None: an older probe.
    claude_login_why: str | None = None
    # The probe's CLAUDEPOOL line (OMN-17427): the host's token pool status line (tokens, how many limited, the next reset; ALARM
    # first when every token is limited). None: no pool node on the host.
    claude_pool: str | None = None
    # The probe's CODEXLOGIN line said ``unavailable``: ``codex login status`` timed out or could not run (OMN-20864). Only the Codex
    # engine is refused for it (``codex_refusal``); the host keeps its Claude engines and every other check.
    codex_unavailable: bool = False
    # Usage-limit and login marks read from the placement directory (``limited_hosts``, ``codex_limited_hosts``,
    # ``auth_expired_hosts``): the until stamp, kept so a refusal can name its cause. A Claude mark also empties the host's engines.
    claude_limited_until: str | None = None
    claude_auth_expired_at: str | None = None
    codex_limited_until: str | None = None
    # The probe's PARITY lines (``parse_parity``) and what the unit being placed needs of the host (OMN-20266). None: not measured.
    parity: HostParity | None = None
    parity_needs: ParityNeeds = PARITY_DEFAULT_NEEDS
    # Set when the runner provisioned this host's repairable parity items and failed (OMN-19998): the error, so a second placement
    # refuses the host for it. None: not tried, or it worked.
    provision_error: str | None = None
    # The probe's HOOKPARSE line (``parse_hook_parse``, OMN-20961): ``ok``, ``fail`` or ``unknown``, and the rest of the line (the
    # counts, or the failures and the first one, or why the check could not run). None: not measured (a Reading built without the probe).
    hook_parse: str | None = None
    hook_parse_detail: str | None = None
    # Of ``placed``, the lanes (``lane_counts``, OMN-20864). Set only when this Reading judges a lane: its reserved share then counts
    # lanes alone. None: a worker's reading, whose reserve counts every placed unit.
    lanes_placed: int | None = None
    # Why a bus unit cannot run on this host: it does not advertise the lane guard the launcher ships (``node_lane_guard_admission_compute``,
    # OMN-17427). A bus unit's stub execs the host's installed copy of that guard and exits 78 in 0 s without it. None: it does, or this
    # is not a bus advertisement (an ssh unit ships the guard inline).
    lane_guard_refusal: str | None = None
    # OMN-21809: seconds since the probe text this Reading was parsed from was read, when the live read timed out and the host's
    # last good reading, younger than the placement contract's pool_reading_max_age_s, stood in for it. None: read live just now.
    cached_age_s: float | None = None
    # OMN-17427: the launcher-side verdict (``ModelLauncherGap``: cause, owner action, age, cooldown) when this launching host cannot resolve,
    # reach or log in to the host, found now or kept from an earlier read inside its cooldown. The host is still unreadable (``error``), so it
    # is never free and never full; the shared host table is untouched, so the host stays available to every other launcher. None: no verdict.
    launcher_gap: Any = None

    @property
    def thermal_refusals(self) -> list[str]:
        """The over-temperature reasons for this host (``cpu NN.NC > 85C``, ``dimm NN.NC > 70C``), none for a sensor
        that was not read. The CPU bar is the host row's ``max_cpu_c`` when lower than the default."""
        reasons = []
        bar = self.host.cpu_bar_c
        if self.cpu_temp_c is not None and self.cpu_temp_c > bar:
            reasons.append(f"cpu {self.cpu_temp_c:.1f}C > {bar:.0f}C")
        if self.dimm_temp_c is not None and self.dimm_temp_c > ADMIT_MAX_DIMM_C:
            reasons.append(f"dimm {self.dimm_temp_c:.1f}C > {ADMIT_MAX_DIMM_C:.0f}C")
        return reasons

    @property
    def workdir_refusal(self) -> str | None:
        """Why a lab host takes no worker of any engine: the probe could not write its lane workdir (``$HOME/lab-run``), so a
        worktree, the prompt and the result file cannot be created there. Not judged for the launching host or an unread host."""
        if (
            self.error is not None
            or self.host.local
            or self.workdir in (None, WORKDIR_OK)
        ):
            return None
        return f"WORKDIR-NOT-WRITABLE: the lane workdir on {self.host.name} failed its write test ({self.workdir})"

    @property
    def hook_parse_refusal(self) -> str | None:
        """Why this host takes no worker or lane of any engine: a hook script its Claude loads does not parse under its own
        /bin/bash, so every tool call the hook guards is refused (OMN-20961), or the check could not run, which proves nothing,
        so the host is not placed until it reads ``ok`` (fail closed). Judged for the launching host too; not for an unreadable
        host (already refused) or a Reading the probe did not build."""
        if self.error is not None or self.hook_parse in (None, HOOK_PARSE_OK):
            return None
        detail = self.hook_parse_detail or "no detail"
        if self.hook_parse == HOOK_PARSE_FAIL:
            return (
                f"HOOK-PARSE-FAILED: hook scripts the host's Claude loads do not parse under /bin/bash on {self.host.name} "
                f"({detail})"
            )
        return f"HOOK-PARSE-UNKNOWN: the hook parse check on {self.host.name} could not run ({detail}); not placed until it reads ok"

    @property
    def parity_refusal(self) -> str | None:
        """Why this host takes no unit: a parity failure the unit's needs name, an installed plugin that differs from its shipped
        revision, a clone missing, behind its origin tip or without its pre-commit hook. Judged for the launching host too, and
        not for an unreadable host or one the probe printed no PARITY lines for."""
        if self.error is not None or self.parity is None:
            return None
        failures = [
            f"{f.item}: {f.reason}" for f in self.parity.failures(self.parity_needs)
        ]
        if self.provision_error:
            failures.append(f"provision: {self.provision_error}")
        if not failures:
            return None
        return "PARITY-FAILED: " + "; ".join(failures)

    @property
    def parity_repairs(self) -> list[ParityFailure]:
        """The clone and hook items the runner provisions on this host before a unit starts (OMN-19998), else none."""
        if self.error is not None or self.parity is None:
            return []
        return self.parity.repairs(self.parity_needs)

    def codex_refusal(
        self, allowed: frozenset[str], *, lane: bool = False
    ) -> str | None:
        """Why this host cannot run the Codex engine now, else None. Judged in this order, each reason named: unreadable, the
        launching host (see below), a host the shared table does not mark ``codex: true`` (``allowed``), no Codex login, a
        Codex usage-limit mark, a Codex sandbox that cannot write, then the shared admission bar and slots. A sandbox state of
        ``unknown`` (the smoke is in flight, or not yet run) is not a refusal: a run that finds the sandbox broken is revoked
        and marks the host (landing_controller.revoke_limit_exits).

        ``lane`` is True for a remote LANE (``onex_remote_lane``) and False for a landing worker. A landing worker never runs
        Codex on the launching host (its own process starts only Claude engines, ``landing_worker_run.engine_argv``). A
        Codex lane runs the same remote script there under ``bash -c`` as over ssh, so the launching host takes one when
        its own table row marks ``codex: true`` (operator RULING 2026-10-11T16:48:49Z, OMN-20864) and its LANE bar and lane
        slots admit it (``lane_admission_refusal``, ``lane_slots``)."""
        if self.error is not None:
            return f"UNREADABLE: {self.error[:60]}"
        if self.host.local and not lane:
            return "CODEX-NOT-ON-LAUNCHING-HOST: a landing worker runs the Codex engine on a lab host only"
        if self.host.local and not self.host.codex:
            return "CODEX-NOT-ON-LAUNCHING-HOST: the launching host's table row does not mark codex: true"
        if self.host.name not in allowed:
            return f"CODEX-NOT-ALLOWED: the lab host table does not mark {self.host.name} codex: true"
        if self.codex_unavailable:
            return (
                f"CODEX-UNAVAILABLE: codex login status timed out or could not run on {self.host.name}; "
                "only the Codex engine is refused there"
            )
        if self.codex_login is not True:
            return (
                "NO-CODEX-LOGIN: codex login status did not pass"
                if self.codex_login is False
                else "CODEX-LOGIN-UNREAD"
            )
        if self.codex_limited_until:
            return f"CODEX-LIMITED: a Codex usage limit holds until {self.codex_limited_until}"
        if self.codex_sandbox == "fail":
            why = f" ({self.codex_sandbox_why})" if self.codex_sandbox_why else ""
            return (
                f"CODEX-SANDBOX-UNWRITABLE: the Codex workspace-write sandbox on {self.host.name} could not write the lane "
                f"workdir{why}"
            )
        if self.host.local:
            # Only a lane reaches here (above): the launching host is judged on its lane bar and lane slots.
            if self.lane_slots <= 0:
                return (
                    self.lane_admission_refusal
                    or "NO-LANE-SLOT: no free lane slot on the launching host"
                )
            return None
        if self.slots <= 0:
            return self.no_slot_reason
        return None

    @property
    def coauthor_refusal(self) -> str | None:
        """Why this host takes no worker or lane whatever its load (its settings leave the Claude co-author trailer
        on), else None. Judged for the local host too, and not for an unreadable host."""
        return None if self.error is not None else coauthor_refusal(self.coauthor)

    def _memory_refusals(self) -> list[str]:
        """The shared memory bar: memory under the floor, Linux memory PSI or Linux/macOS swap-in or combined swap
        traffic over their bars, or macOS memory pressure at the bar. Swap occupancy is reported history only."""
        reasons: list[str] = []
        if self.mem_avail_gb < (
            floor := ADMIT_MIN_FREE_GB
            if self.host.min_free_gb is None
            else self.host.min_free_gb
        ):
            reasons.append(f"mem_avail {self.mem_avail_gb:.1f}G < {floor:g}G")
        if self.mem_psi_avg60 is not None and self.mem_psi_avg60 > ADMIT_MAX_MEM_PSI:
            reasons.append(
                f"memory PSI {self.mem_psi_avg60:.1f}% > {ADMIT_MAX_MEM_PSI:.0f}%"
            )
        si, so = self.swap_in_kb_s, self.swap_out_kb_s
        if si is not None and si > ADMIT_MAX_SWAP_IN_KB_S:
            reasons.append(f"swap-in {si:.0f} KB/s > {ADMIT_MAX_SWAP_IN_KB_S} KB/s")
        if si is not None and so is not None and si + so > ADMIT_MAX_SWAP_KB_S:
            reasons.append(
                f"swap traffic {si + so:.0f} KB/s (in {si:.0f}, out {so:.0f}) > {ADMIT_MAX_SWAP_KB_S} KB/s"
            )
        reasons.extend(self._mac_memory_refusals())
        return reasons

    @property
    def mac_free_gb(self) -> float:
        """The free memory a Mac is judged on: the kernel's free percentage of its memory when the probe read both, else the
        available memory (free, inactive and speculative pages)."""
        if self.mem_free_pct is not None and self.mem_total_gb:
            return self.mem_free_pct / 100 * self.mem_total_gb
        return self.mem_avail_gb

    def _mac_free_text(self) -> str:
        """The measured free figure a macOS refusal prints, so a NOTE line can be checked against the host."""
        if self.mem_free_pct is not None and self.mem_total_gb:
            return f"free {self.mem_free_pct:.0f}% (~{self.mac_free_gb:.1f}G of {self.mem_total_gb:.0f}G)"
        return f"free ~{self.mac_free_gb:.1f}G (available memory; no kernel free percentage)"

    def _mac_memory_refusals(self) -> list[str]:
        """The macOS memory bar (OMN-19998): refused at critical pressure, under the free-percentage floor, and at warn while
        the free memory is under a lane's declared need plus the margin. Empty for a host that read no macOS memory figure."""
        level = self.mem_pressure
        if level is None and self.mem_free_pct is None:
            return []
        if level is not None and level >= ADMIT_MAC_CRITICAL_PRESSURE:
            return [
                f"macOS memory pressure critical (level {level} >= {ADMIT_MAC_CRITICAL_PRESSURE}): {self._mac_free_text()}"
            ]
        if self.mem_free_pct is not None and self.mem_free_pct < ADMIT_MAC_MIN_FREE_PCT:
            return [
                f"macOS {self._mac_free_text()} < {ADMIT_MAC_MIN_FREE_PCT:.0f}% floor"
                + ("" if level is None else f" (pressure level {level})")
            ]
        need = ADMIT_LANE_MEM_MAX_GB + ADMIT_MAC_WARN_MARGIN_GB
        if (
            level is not None
            and level >= ADMIT_MAC_WARN_PRESSURE
            and self.mac_free_gb < need
        ):
            return [
                (
                    f"macOS memory pressure warn (level {level}) and {self._mac_free_text()} < {need:.0f}G "
                    f"(a lane's {ADMIT_LANE_MEM_MAX_GB:.0f}G plus a {ADMIT_MAC_WARN_MARGIN_GB:.0f}G margin)"
                )
            ]
        return []

    @property
    def load_refusal(self) -> str | None:
        """Why this host takes nothing whatever its cap: its load bar refuses (``load_bar_refusal``) and its reserved share is
        closed (``reserve_closed``: spent, under the reserve memory floor, or none kept). None while the reserve still admits
        a unit (OMN-20864)."""
        bar = self.load_bar_refusal
        if bar is None or self.reserve <= 0:
            return bar
        if self.reserve_slots > 0:
            return None
        return f"{bar}; {self.reserve_closed}"

    @property
    def reserve(self) -> int:
        """Units this host admits while its load bar refuses: the row's ``reserve``, else ``ADMIT_RESERVED_SLOTS`` on a lab
        host and none on the launching host (OMN-20864)."""
        if self.host.reserve is not None:
            return max(0, self.host.reserve)
        return 0 if self.host.local else ADMIT_RESERVED_SLOTS

    @property
    def _reserve_open(self) -> int:
        """Reserved units not yet placed: the reserve less the lanes placed here (a lane's reading) or every unit placed here (a
        worker's), never past the cap."""
        counted = self.placed if self.lanes_placed is None else self.lanes_placed
        return min(self.reserve - counted, self.cap - self.placed)

    @property
    def _unexplained_load(self) -> float:
        """load1 less ``LAB_WORKER_COST_CORES`` for every unit placed here: the load the host's own placements cannot explain."""
        return self.load1 - LAB_WORKER_COST_CORES * self.placed

    @property
    def _own_load(self) -> bool:
        """True when the load bar refuses and the units placed here explain the load over it."""
        return (
            self.load_bar_refusal is not None
            and self.cores > 0
            and self._unexplained_load <= ADMIT_MAX_LOAD_PER_CORE * self.cores
        )

    @property
    def _reserve_by_mem(self) -> int:
        """Reserved units the memory floor allows: each charged ``ADMIT_LANE_MEM_MAX_GB``, with ``ADMIT_MIN_FREE_GB`` left
        after them, and a unit placed inside the settle window (not yet in ``mem_avail_gb``) charged the same."""
        room = (
            self.mem_avail_gb - ADMIT_MIN_FREE_GB - ADMIT_LANE_MEM_MAX_GB * self.fresh
        )
        return int(room // ADMIT_LANE_MEM_MAX_GB) if room > 0 else 0

    @property
    def reserve_slots(self) -> int:
        """Further units the reserved share admits now, whatever the load bar and the free cores say (OMN-20864). The other
        bars are judged by ``admission_refusal`` as ever and refuse the host on their own."""
        if (
            self.error is not None
            or self.cores <= 0
            or self.reserve <= 0
            or self.fresh >= PLACEMENT_BURST_PER_HOST
            or self._own_load
        ):
            return 0
        return max(0, min(self._reserve_open, self._reserve_by_mem))

    @property
    def reserve_closed(self) -> str | None:
        """Why ``reserve_slots`` is zero, else None."""
        if self.reserve_slots > 0:
            return None
        if self.reserve <= 0:
            return "RESERVE-NONE: the host keeps no reserved share"
        if self.error is not None or self.cores <= 0:
            return "RESERVE-UNREAD: the host was not read"
        if self.fresh >= PLACEMENT_BURST_PER_HOST:
            return (
                f"RESERVE-BURST: {self.fresh} units placed in the last {PLACEMENT_SETTLE_S:.0f}s "
                f"(budget {PLACEMENT_BURST_PER_HOST})"
            )
        if self._own_load:
            return (
                f"RESERVE-OWN-LOAD: the {self.placed} units placed here explain the load (load1 {self.load1:.1f} less "
                f"{LAB_WORKER_COST_CORES:.0f} a unit is {self._unexplained_load:.1f}, not over "
                f"{ADMIT_MAX_LOAD_PER_CORE * self.cores:.0f})"
            )
        if self.placed >= self.cap:
            return f"RESERVE-CAP-FULL: {self.placed} placed >= cap {self.cap}"
        if self._reserve_open <= 0:
            if self.lanes_placed is not None:
                return f"RESERVE-HELD: {self.lanes_placed} lanes placed >= reserve {self.reserve}"
            return f"RESERVE-HELD: {self.placed} placed >= reserve {self.reserve}"
        unsettled = (
            f" less {self.fresh} unsettled units at {ADMIT_LANE_MEM_MAX_GB:.0f}G"
            if self.fresh
            else ""
        )
        return (
            f"RESERVE-MEMORY-FLOOR: {self.mem_avail_gb:.1f}G available{unsettled} leaves under the "
            f"{ADMIT_MIN_FREE_GB:.0f}G floor after a reserved unit's {ADMIT_LANE_MEM_MAX_GB:.0f}G"
        )

    def without_reserve(self) -> Reading:
        """This reading judged with no reserved share: the plain load bar, for work that is itself the load (the lab runner)."""
        return replace(self, host=replace(self.host, reserve=0))

    @property
    def load_bar_refusal(self) -> str | None:
        """Why the load bar refuses this host: its one-minute load average is over ``ADMIT_MAX_LOAD_PER_CORE``
        per core (OMN-20366). Judged on load1, never on the busy-core sample that lowers ``effective_load1``. A Mac with a CPUIDLE sample is
        judged on that idle instead, with load1 as a secondary guard at ``ADMIT_MAC_MAX_LOAD_PER_CORE`` (OMN-20434). The reserved
        share (``reserve_slots``) may still admit the host; ``load_refusal`` is the bar after it."""
        if self.error is not None or self.cores <= 0:
            return None
        if self.cpu_idle_pct is not None:
            # macOS (OMN-20434): measured CPU idle decides; load1 is the secondary guard at a higher bar.
            if self.cpu_idle_pct < ADMIT_MAC_MIN_CPU_IDLE_PCT:
                return f"CPU-IDLE-UNDER-BAR: {self.cpu_idle_pct:.1f}% idle < {ADMIT_MAC_MIN_CPU_IDLE_PCT:.0f}%"
            if self.load1 > ADMIT_MAC_MAX_LOAD_PER_CORE * self.cores:
                return (
                    f"LOAD-OVER-BAR: load1 {self.load1:.1f} on {self.cores} cores is {self.load1 / self.cores:.2f} "
                    f"per core > {ADMIT_MAC_MAX_LOAD_PER_CORE:.1f} (macOS secondary bar)"
                )
            return None
        if self.load1 > ADMIT_MAX_LOAD_PER_CORE * self.cores:
            return (
                f"LOAD-OVER-BAR: load1 {self.load1:.1f} on {self.cores} cores is "
                f"{self.load1 / self.cores:.2f} per core > {ADMIT_MAX_LOAD_PER_CORE:.1f}"
            )
        return None

    def _bar_refusals(self, *, workdir: bool) -> list[str]:
        """The bars every host passes whatever its link, in the order they print: settings, parity, the lane workdir (a lab host), heat, memory, load."""
        parts = [
            self.hook_parse_refusal,
            self.coauthor_refusal,
            self.parity_refusal,
            self.workdir_refusal if workdir else None,
            *self.thermal_refusals,
            *self._memory_refusals(),
            self.load_refusal,
        ]
        return [part for part in parts if part]

    @property
    def link_verdict(self) -> ModelLinkGateVerdict:
        return NodeLinkGateCompute().handle(
            ModelLinkGateRequest(
                loss_pct=self.link_loss_pct,
                prior=tuple(self.link_loss_prior),
                local=self.host.local,
                bus=self.bus_delivered,
            )
        )

    @property
    def admission_refusal(self) -> str | None:
        """Why this lab host takes no lane now (memory under the floor, memory PSI or swap traffic over their bars,
        macOS memory pressure at the bar), else None. The local host and an unreadable host are not judged here
        (the landing controller's rule); a lane on the local host is judged by ``lane_admission_refusal``."""
        if self.error is not None:
            return None
        if self.host.local:
            return (
                "; ".join(
                    [
                        *([self.hook_parse_refusal] if self.hook_parse_refusal else []),
                        *([self.coauthor_refusal] if self.coauthor_refusal else []),
                        *self.thermal_refusals,
                        *([self.parity_refusal] if self.parity_refusal else []),
                    ]
                )
                or None
            )
        reasons = self._bar_refusals(workdir=True)
        if self.link_verdict.refusal:
            reasons.append(self.link_verdict.refusal)
        if self.lan_grant is not None and self.lan_grant != LAN_GRANT_OK:
            reasons.append(
                f"MAC-LAN-GRANT-MISSING: Local Network access {self.lan_grant}"
            )
        return "; ".join(reasons) or None

    @property
    def lane_admission_refusal(self) -> str | None:
        """Why this host takes no LANE now. A lab host: ``admission_refusal``. The launching host (OMN-17427, RULING
        2026-10-02T10:59:04Z) passes the same memory bar, the settings bar and the macOS pressure bar as a lab host, but not the
        two that judge a link from the placing host (link loss, the Local Network grant): there is no link."""
        if self.error is not None:
            return None
        if not self.host.local:
            return self.admission_refusal
        return "; ".join(self._bar_refusals(workdir=False)) or None

    @property
    def effective_load1(self) -> float:
        """The load the slot arithmetic uses: load1, or the sampled busy cores when that is lower."""
        if self.busy_cores is None:
            return self.load1
        return min(self.load1, self.busy_cores)

    @property
    def load_per_core_ceiling(self) -> float:
        return LOCAL_LOAD_PER_CORE if self.host.local else LAB_LOAD_PER_CORE

    @property
    def worker_cost_cores(self) -> float:
        """Cores the slot arithmetic charges each further unit: the row's ``unit_cores``, else the per-kind default."""
        if self.host.unit_cores is not None and self.host.unit_cores > 0:
            return self.host.unit_cores
        return WORKER_COST_CORES if self.host.local else LAB_WORKER_COST_CORES

    @property
    def free_cores(self) -> float:
        if self.error is not None or self.cores <= 0:
            return float("-inf")
        return (
            self.load_per_core_ceiling * self.cores
            - self.effective_load1
            - WORKER_RESIDUAL_CORES * self.fresh
        )

    @property
    def cap(self) -> int:
        """Most workers this host holds at once: the row's cap, else one per ``WORKER_COST_CORES``."""
        if self.host.cap is not None:
            return max(0, self.host.cap)
        return (
            max(1, int(self.cores // self.worker_cost_cores)) if self.cores > 0 else 0
        )

    @property
    def idle(self) -> bool:
        return (
            self.error is None
            and self.cores > 0
            and self.placed == 0
            and self.effective_load1 <= IDLE_LOAD_PER_CORE * self.cores
        )

    def _slots_under(self, refusal: str | None, cap: int) -> int:
        """The free-core and memory arithmetic, or the reserved share when that is more (OMN-20864); none while ``refusal``
        holds or the burst budget is spent."""
        if (
            self.error is not None
            or refusal is not None
            or self.fresh >= PLACEMENT_BURST_PER_HOST
        ):
            return 0
        return max(0, min(self.reserve_slots, cap - self.placed), self._free_slots(cap))

    def _free_slots(self, cap: int) -> int:
        if self.mem_avail_gb < MIN_FREE_GB:
            return 0
        if self.load_bar_refusal is not None and self.reserve_slots > 0:
            # Admitted on the reserve alone: over the load bar the busy-core sample opens no further slot (OMN-20366).
            return 0
        by_cores = (
            int(self.free_cores // self.worker_cost_cores) if self.free_cores > 0 else 0
        )
        by_mem = int((self.mem_avail_gb - MIN_FREE_GB) // WORKER_MEM_GB)
        if self.idle:
            by_cores, by_mem = max(by_cores, 1), max(by_mem, 1)
        return max(0, min(cap - self.placed, by_cores, by_mem))

    @property
    def on_reserve(self) -> bool:
        """True when this host takes a worker only through its reserved share (OMN-20864)."""
        return self.slots > 0 and self._free_slots(self.cap) == 0

    @property
    def slots(self) -> int:
        """Further workers this host can take now: free cores and free memory, capped. A host the admission rule
        refuses (``admission_refusal``) takes none, so the controller never places a worker where the lane runner
        would not start a lane (OMN-20207)."""
        return self._slots_under(self.admission_refusal, self.cap)

    @property
    def no_slot_reason(self) -> str:
        """Why ``slots`` is zero: the admission refusal, else the burst budget when it is spent, else no free capacity."""
        if self.admission_refusal:
            return self.admission_refusal
        if self.fresh >= PLACEMENT_BURST_PER_HOST:
            return (
                f"BURST-BUDGET: {self.fresh} workers placed on {self.host.name} in the last {PLACEMENT_SETTLE_S:.0f}s "
                f"are not yet in its load average (budget {PLACEMENT_BURST_PER_HOST})"
            )
        return "NO-SLOT: no free worker slot"

    @property
    def lane_cap(self) -> int:
        """Most lanes this host holds at once: a lab host's ``cap``; the launching host's row ``cap`` from the pool
        table, else ``LOCAL_LANE_SLOT_CAP``."""
        if not self.host.local:
            return self.cap
        return max(0, LOCAL_LANE_SLOT_CAP if self.host.cap is None else self.host.cap)

    @property
    def lane_slots(self) -> int:
        """Further lanes this host can take now. Equal to ``slots`` for a lab host. The launching host is judged on
        its load per core (``LOCAL_LOAD_PER_CORE``), the memory bar and its ``lane_cap``, so it refuses a lane when
        its load or memory bar fails (OMN-17427)."""
        return self._slots_under(self.lane_admission_refusal, self.lane_cap)

    @property
    def rank_free(self) -> float:
        """Free cores less the table penalty: what ``choose`` ranks by, never what decides slots. Ranked on load1, not on
        the busy-core sample, so the least-loaded eligible host comes first (OMN-20366: an iowait-bound host that
        reads busy=12 beside load1 59 must not outrank an idle one)."""
        if self.error is not None or self.cores <= 0:
            return float("-inf")
        free = (
            self.load_per_core_ceiling * self.cores
            - self.load1
            - WORKER_RESIDUAL_CORES * self.placed
        )
        return free - self.host.penalty * self.cores

    def describe(self) -> str:
        if self.error is not None:
            return f"{self.host.name}:UNREADABLE({self.launcher_gap.line + ' | ' if self.launcher_gap else ''}{self.error[:60]})"
        login = (
            "claude"
            if self.host.engines
            else "none"
            + (f",loginwhy={self.claude_login_why}" if self.claude_login_why else "")
        )  # OMN-21751: the probe word behind it
        busy = "" if self.busy_cores is None else f",busy={self.busy_cores:.1f}"
        if self.cpu_idle_pct is not None:
            busy += f",idle={self.cpu_idle_pct:.1f}%"
        swap = (
            ""
            if self.swap_used_frac is None
            else f",swap={self.swap_used_frac * 100:.0f}%"
        )
        if self.mem_psi_avg60 is not None:
            swap += f",mempsi={self.mem_psi_avg60:.2f}"
        if self.swap_in_kb_s is not None and self.swap_out_kb_s is not None:
            swap += f",si={self.swap_in_kb_s:.0f}KB/s,so={self.swap_out_kb_s:.0f}KB/s"
        if self.mem_pressure is not None:
            swap += f",pressure={self.mem_pressure}"
        if self.mem_free_pct is not None and self.mem_total_gb:
            swap += (
                f",memfree={self.mem_free_pct:.0f}%,memtotal={self.mem_total_gb:.1f}G"
            )
        if self.fresh:
            swap += f",fresh={self.fresh}"
        swap += "" if self.cached_age_s is None else f",cached={self.cached_age_s:.0f}s"
        refused = self.admission_refusal
        admit = "" if refused is None else f",ADMISSION-REFUSED({refused})"
        if self.coauthor is not None:
            swap += f",coauthor={self.coauthor}"
        if self.lan_grant is not None:
            swap += f",langrant={self.lan_grant}"
        if self.link_loss_pct is not None and not self.bus_delivered:
            swap += f",loss={self.link_loss_pct:.0f}%" + (
                ",LINK-DEGRADED"
                if self.link_verdict.degraded and not self.link_verdict.refusal
                else ""
            )
        lane = ""
        if self.host.local:
            lane_refused = self.lane_admission_refusal
            lane = f",lane_cap={self.lane_cap},lane_slots={self.lane_slots}" + (
                "" if lane_refused is None else f",LANE-REFUSED({lane_refused})"
            )
        if self.cpu_temp_c is not None:
            swap += f",cpu={self.cpu_temp_c:.1f}C"
        if self.dimm_temp_c is not None:
            swap += f",dimm={self.dimm_temp_c:.1f}C"
        if self.workdir is not None:
            swap += f",workdir={self.workdir}"
        if self.claude_pool is not None:
            swap += f",claudepool=[{self.claude_pool}]"
        if self.hook_parse is not None:
            swap += f",hookparse={self.hook_parse}"
        if self.parity is not None and self.parity.gh_credential is not None:
            swap += f",gh={self.parity.gh_credential.verdict}/{self.parity.gh_credential.source}"
        if self.codex_unavailable:
            swap += ",codex=unavailable"
        elif self.codex_login is not None:
            swap += f",codex={'login' if self.codex_login else 'nologin'}/{self.codex_sandbox or 'unmeasured'}"
        if self.codex_limited_until:
            swap += f",CODEX-LIMITED-UNTIL={self.codex_limited_until}"
        return (
            f"{self.host.name}:cores={self.cores},load1={self.load1:.1f}{busy},free_gb={self.mem_avail_gb:.1f}{swap},"
            f"placed={self.placed},cap={self.cap},slots={self.slots},login={login}{admit}{lane}"
        )

    @classmethod
    def from_description(cls, host: Host, text: str) -> Reading | None:
        """Read a cached ``describe`` string's measurements for the shared admission decision."""
        if "UNREADABLE(" in text:
            return None
        text = re.split(
            r",(?:ADMISSION-REFUSED\(|LANE-REFUSED\(|lane_cap=)", text, maxsplit=1
        )[0]
        fields = dict(
            part.split("=", 1)
            for part in text.partition(":")[2].split(",")
            if "=" in part
        )
        values: dict[str, Any] = {}
        try:
            values["cores"] = int(fields["cores"])
            values["mem_avail_gb"] = float(fields["free_gb"])
            for key, name, convert in (
                ("load1", "load1", float),
                ("busy", "busy_cores", float),
                ("mempsi", "mem_psi_avg60", float),
                ("pressure", "mem_pressure", int),
                ("placed", "placed", int),
                ("fresh", "fresh", int),
            ):
                if key in fields:
                    values[name] = convert(fields[key])
            for key, name, unit, scale in (
                ("idle", "cpu_idle_pct", "%", 1.0),
                ("swap", "swap_used_frac", "%", 100.0),
                ("loss", "link_loss_pct", "%", 1.0),
                ("cpu", "cpu_temp_c", "C", 1.0),
                ("dimm", "dimm_temp_c", "C", 1.0),
                ("memfree", "mem_free_pct", "%", 1.0),
                ("memtotal", "mem_total_gb", "G", 1.0),
                ("cached", "cached_age_s", "s", 1.0),
                ("si", "swap_in_kb_s", "KB/s", 1.0),
                ("so", "swap_out_kb_s", "KB/s", 1.0),
            ):
                if key in fields:
                    if not fields[key].endswith(unit):
                        return None
                    values[name] = float(fields[key].removesuffix(unit)) / scale
            for key, name in (
                ("coauthor", "coauthor"),
                ("langrant", "lan_grant"),
                ("workdir", "workdir"),
                ("hookparse", "hook_parse"),
            ):
                if key in fields:
                    values[name] = fields[key]
        except (KeyError, ValueError):
            return None
        return cls(host=host, **values)


@dataclass(frozen=True)
class Placement:
    host: Host
    engine: str
    reading: Reading
    readings: tuple[Reading, ...] = field(default_factory=tuple)

    def record(self) -> dict[str, Any]:
        return {
            "host": self.host.name,
            "target": self.host.target,
            "local": self.host.local,
            "engine": self.engine,
            "free_cores": round(self.reading.free_cores, 2),
            "reserve": self.reading.on_reserve,
            "readings": [r.describe() for r in self.readings],
        }


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _row_value(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _table_rows(env: Mapping[str, str] | None) -> tuple[list[dict[str, Any]], str]:
    """The pool rows of the lab host table (``ONEX_LAB_RUN_HOSTS``, else the ``lab_hosts`` overlay) and where they came from.

    Raises :class:`NodeOverlayError` naming ``lab_hosts.hosts`` when neither supplies a table."""
    environ = _env(env)
    table = resolve_lab_hosts_table(environ)
    rows = [
        {
            key: _row_value(value)
            for key, value in row.model_dump(exclude_unset=True).items()
        }
        for row in table.hosts
    ]
    source = environ.get("ONEX_LAB_RUN_HOSTS", "").strip() or "overlay lab_hosts"
    pool: list[dict[str, Any]] = []
    if not any(bool(r.get("local")) for r in rows):
        pool.append({**LOCAL_POOL_ROW, "engines": "live"})
    for row in rows:
        # A row the table marks ``available: false`` is out of the lab mix: the remote-lane runner never places on it,
        # and probing it cost every pool read its 20 s ssh timeout (OMN-20366), so it is not in the pool at all.
        if row.get("available") is False:
            continue
        entry: dict[str, Any] = {
            "name": row["name"],
            "gh_absent": row.get("gh_absent") is True,
            "engines": "live",
            "cap": row.get("cap"),
            "lab_work_root": row.get("lab_work_root"),
            "report_retention_days": row.get("report_retention_days", 7),
            "penalty": float(row.get("penalty", 0) or 0),
        }
        if row.get("ledger_bus_lane") and row.get("ledger_bus_python"):
            entry["ledger_bus_lane"] = str(row["ledger_bus_lane"])
            entry["ledger_bus_python"] = str(row["ledger_bus_python"])
        if row.get("tenant_id"):
            entry["tenant_id"] = str(row["tenant_id"])
        if row.get("parity_clones"):
            clones = row["parity_clones"]
            entry["parity_clones"] = (
                ",".join(clones) if isinstance(clones, list) else str(clones)
            )
        if row.get("parity_python"):
            entry["parity_python"] = str(row["parity_python"])
        if row.get("reserve") is not None:
            entry["reserve"] = row["reserve"]
        if row.get("codex") is True:
            entry["codex"] = True
        entry.update(
            {
                key: row[key]
                for key in (
                    "unit_cores",
                    "max_cpu_c",
                    "min_free_gb",
                    "fallback_targets",
                )
                if row.get(key) is not None
            }
        )
        if row.get("local"):
            entry["local"] = True
        else:
            entry["target"] = row.get("target")
        pool.append(entry)
    return pool, source


def pool_source(env: Mapping[str, str] | None = None) -> str:
    """Where ``load_pool`` took its rows from: ``LANDING_POOL_HOSTS``, the table file, or the overlay."""
    if _env(env).get("LANDING_POOL_HOSTS") is not None:
        return "LANDING_POOL_HOSTS"
    return _table_rows(env)[1]


def parity_clone_names(value: Any) -> tuple[str, ...]:
    """The clone names of a row's ``parity_clones`` (a comma list or a JSON list); a name that is not a plain directory name is dropped."""
    names = (
        value.split(",")
        if isinstance(value, str)
        else value
        if isinstance(value, list)
        else []
    )
    return tuple(
        dict.fromkeys(
            n.strip()
            for n in names
            if isinstance(n, str) and CLONE_NAME_RE.match(n.strip())
        )
    )


def lab_work_root(value: Any) -> str | None:
    """A declared absolute lab-run root; reject shell syntax and traversal."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"/[A-Za-z0-9_./+-]+/lab-run", value)
        or ".." in value.split("/")
    ):
        raise ValueError("lab_work_root must be an absolute lab-run path")
    return value.rstrip("/")


def report_retention_days(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("report_retention_days must be a positive integer")
    return value


def load_pool(env: Mapping[str, str] | None = None) -> list[Host]:
    """Pool from explicit JSON, or the shared host table plus this host; no table raises :class:`NodeOverlayError`."""
    raw = _env(env).get("LANDING_POOL_HOSTS")
    rows: Sequence[Mapping[str, Any]]
    if raw is None:
        rows, _source = _table_rows(env)
    else:
        try:
            parsed = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        rows = [r for r in parsed if isinstance(r, dict)]
    hosts: list[Host] = []
    for row in rows:
        name = str(row.get("name") or "")
        local = bool(row.get("local"))
        target = None if local else str(row.get("target") or "")
        if not name or row.get("available") is False or (not local and not target):
            continue
        declared = row.get("engines")
        live = declared == "live"
        hosts.append(
            Host(
                name=name,
                target=target,
                engines=frozenset()
                if live
                else frozenset(
                    str(e) for e in declared or () if str(e) not in RETIRED_ENGINES
                ),
                cap=None if row.get("cap") is None else int(row["cap"]),
                gh=bool(row.get("gh", False)),
                gh_absent=row.get("gh_absent") is True,
                lab_work_root=lab_work_root(row.get("lab_work_root")),
                report_retention_days=report_retention_days(
                    row.get("report_retention_days", 7)
                ),
                penalty=float(row.get("penalty", 0) or 0),
                engines_live=live,
                ledger_bus_lane=str(row["ledger_bus_lane"])
                if row.get("ledger_bus_lane")
                else None,
                ledger_bus_python=str(row["ledger_bus_python"])
                if row.get("ledger_bus_python")
                else None,
                tenant_id=str(row["tenant_id"]) if row.get("tenant_id") else None,
                parity_clones=parity_clone_names(row.get("parity_clones")),
                parity_python=parity_python_path(row.get("parity_python")),
                reserve=None if row.get("reserve") is None else int(row["reserve"]),
                codex=row.get("codex") is True,
                unit_cores=None
                if row.get("unit_cores") is None
                else float(row["unit_cores"]),
                max_cpu_c=None
                if row.get("max_cpu_c") is None
                else float(row["max_cpu_c"]),
                min_free_gb=None
                if row.get("min_free_gb") is None
                else float(row["min_free_gb"]),
                fallback_targets=()
                if local
                else tuple(
                    n
                    for n in recovery.fallback_names(row.get("fallback_targets"))
                    if n != target
                ),
            )
        )
    return hosts


def placement_dir(env: Mapping[str, str] | None = None) -> Path:
    env = _env(env)
    if env.get("LANDING_PLACEMENT_DIR"):
        return Path(env["LANDING_PLACEMENT_DIR"])
    return (
        Path(env.get("HOME") or str(Path.home()))
        / ".local"
        / "state"
        / "omni"
        / "landing"
        / "placement"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError, OSError):
        return False
    return True


def placed_counts(directory: Path) -> dict[str, int]:
    """Live placed workers per host; markers of dead pids are removed. Only a slot marker (``mark``: ``<host>.<lease>.<pid>``) is
    judged: the directory also holds state files (``PARITY_FILE``, ``GH_FALLBACK_FILE``) that are no marker and are left alone."""
    counts: dict[str, int] = {}
    if not directory.is_dir():
        return counts
    for marker in directory.iterdir():
        if marker.name.startswith("."):
            # ``.lock`` (the placement lock) lives here; removing it would give the next worker a
            # fresh inode to lock, so concurrent placements would all read placed=0.
            continue
        parts = marker.name.rsplit(".", 2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            # ``parity.json`` was unlinked here as malformed, so a placement whose pool read recorded nothing left queue-status
            # with "no record" (2026-10-17).
            continue
        if not _pid_alive(int(parts[2])):
            marker.unlink(missing_ok=True)
            continue
        counts[parts[0]] = counts.get(parts[0], 0) + 1
    return counts


def fresh_counts(
    directory: Path,
    *,
    now: datetime | None = None,
    window_s: float = PLACEMENT_SETTLE_S,
) -> dict[str, int]:
    """Live placed workers per host whose marker is under ``window_s`` old: the placements one probe reading cannot show yet.
    Read-only (``placed_counts`` removes the dead markers); a marker written in the future of ``now`` counts as fresh."""
    counts: dict[str, int] = {}
    if not directory.is_dir():
        return counts
    cutoff = (now or datetime.now(UTC)).timestamp() - window_s
    for marker in directory.iterdir():
        parts = marker.name.rsplit(".", 2)
        if (
            marker.name.startswith(".")
            or len(parts) != 3
            or not parts[2].isdigit()
            or not _pid_alive(int(parts[2]))
        ):
            continue
        try:
            fresh = marker.stat().st_mtime > cutoff
        except OSError:
            continue
        if fresh:
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    return counts


def lane_counts(directory: Path) -> dict[str, int]:
    """Live placed lanes per host: markers whose id is a remote lane's (``LANE_MARKER_MIN_ID``) and whose pid is alive. Read-only
    (``placed_counts`` removes the dead markers)."""
    counts: dict[str, int] = {}
    if not directory.is_dir():
        return counts
    for marker in directory.iterdir():
        parts = marker.name.rsplit(".", 2)
        if (
            marker.name.startswith(".")
            or len(parts) != 3
            or not parts[1].isdigit()
            or not parts[2].isdigit()
            or int(parts[1]) < LANE_MARKER_MIN_ID
            or not _pid_alive(int(parts[2]))
        ):
            continue
        counts[parts[0]] = counts.get(parts[0], 0) + 1
    return counts


def mark(directory: Path, host: str, lease_id: int, pid: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{host}.{lease_id}.{pid}"
    marker.write_text(str(pid), encoding="utf-8")
    return marker


# A host whose Claude account hit a usage limit takes no worker until its marker expires (OMN-17427,
# an earlier lane). On 2026-09-30 from ~21:47Z the h201 account answered every worker with
# "You've hit your weekly limit" and exit 1 within seconds (leases 514 and 518-521), and each exit
# stepped its PR up the escalation ladder, while the probe kept reporting h201 as logged in with the
# most free slots, so placement kept choosing it. A limit on one host's account is not a limit on
# another's, so the marker is per host; the controller's engine-wide engine-claude-unavailable.json is
# written only when every pool host carries one (landing_controller.note_engine_limit). It is a dotfile
# so ``placed_counts`` leaves it alone.
LIMIT_MARKERS = (
    "you've hit your",
    "you have hit your",
    "usage limit reached",
    "limit reached",
    "out of extra usage",
)
LIMIT_BACKOFF_S = 3600.0
WEEKLY_LIMIT_BACKOFF_S = 6 * 3600.0
LIMITED_PREFIX = ".limited-"


def _utc_stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# The reset time a limit notice states ("resets Oct 5, 4am (America/New_York)", "resets Oct 5 at 4am", "resets
# 1:10pm (America/New_York)", OMN-17427): the marker lasts until then, because a flat backoff put a host whose
# weekly limit ran to Oct 5 back into placement six hours in. A notice with no zone is read in the operator's
# zone, the one every observed notice names. A reset beyond a week and a day, a date that has already passed,
# or text this cannot read is not trusted: the host is marked for the flat default instead of left unmarked.
RESET_ZONE_DEFAULT = "America/New_York"
RESET_MARGIN_S = 60.0
RESET_MAX_S = 8 * 86400.0
_RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:\s*,|\s+at)?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap])\.?m\.?"
    r"(?:\s*\((?P<zone>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


def _reset_in_s(text: str, now: datetime) -> float | None:
    """Seconds from ``now`` to the reset time ``text`` states, plus a margin; None when it states none that can
    be trusted."""
    found = _RESET_RE.search(text)
    if found is None:
        return None
    try:
        zone = ZoneInfo((found["zone"] or RESET_ZONE_DEFAULT).strip())
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(RESET_ZONE_DEFAULT)
    hour = int(found["hour"])
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if found["ampm"].lower() == "p" else 0)
    minute = int(found["min"] or 0)
    local = now.astimezone(zone)
    try:
        if found["mon"]:
            if found["mon"].lower() not in _MONTHS:
                return None
            month = _MONTHS.index(found["mon"].lower()) + 1
            reset = datetime(
                local.year, month, int(found["day"]), hour, minute, tzinfo=zone
            )
        else:
            reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset <= local:
                reset = reset + timedelta(days=1)
    except ValueError:
        return None
    seconds = (reset - now).total_seconds()
    if seconds <= 0 or seconds > RESET_MAX_S:
        return None
    return seconds + RESET_MARGIN_S


def limit_backoff_s(text: str, now: datetime | None = None) -> float | None:
    """Seconds a host's Claude login is out when ``text`` (an engine's closing output) reports a usage limit:
    until the reset time the notice states, else (no reset stated, unreadable, or not trusted) six hours for a
    weekly limit and an hour otherwise. None: no limit reported."""
    low = text.lower()
    if not any(m in low for m in LIMIT_MARKERS):
        return None
    reset = _reset_in_s(text, now or datetime.now(UTC))
    if reset is not None:
        return reset
    return WEEKLY_LIMIT_BACKOFF_S if "weekly" in low else LIMIT_BACKOFF_S


# A host whose Claude OAuth session expired (OMN-20498): every lane placed there ended in ~10 minutes with
# "Failed to authenticate: OAuth session expired and could not be refreshed" while the probe still read the host as
# logged in. The mark is a file of its own, so a usage-limit mark and an auth mark for one host never clobber each
# other and ``limited_hosts`` is unchanged. It has no short backoff: it lasts until the operator deletes it, bounded by
# a long safety expiry so a forgotten mark cannot last forever. The probe cannot clear it: its smoke prompt result is
# cached for six hours as a login that works, so a session that expired inside that window still reads present.
AUTH_MARKERS = (
    "failed to authenticate",
    "oauth session expired",
    "could not be refreshed",
    "authentication_error",
    "invalid authentication credentials",
)
_AUTH_401_RE = re.compile(r"\b401\b.{0,200}?authenticat", re.IGNORECASE | re.DOTALL)
AUTH_EXPIRED_PREFIX = ".auth-expired-"
AUTH_EXPIRY_S = 7 * 86400.0
AUTH_ENGINE_CLAUDE = "claude"


def auth_failure(text: str) -> bool:
    """True when ``text`` (an engine's closing output) reports an expired or invalid Claude login."""
    low = text.lower()
    return any(m in low for m in AUTH_MARKERS) or _AUTH_401_RE.search(text) is not None


def _auth_marker(directory: Path, host: str, engine: str) -> Path:
    return directory / f"{AUTH_EXPIRED_PREFIX}{engine}-{host}"


def mark_auth_expired(
    directory: Path,
    host: str,
    *,
    engine: str = AUTH_ENGINE_CLAUDE,
    now: datetime | None = None,
    lease_id: int | None = None,
) -> str:
    """Take ``host`` out of ``engine`` placement until cleared; returns the safety-expiry stamp."""
    now = now or datetime.now(UTC)
    until = _utc_stamp(now + timedelta(seconds=AUTH_EXPIRY_S))
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "host": host,
            "engine": engine,
            "kind": "auth-expired",
            "until": until,
            "at": _utc_stamp(now),
            "lease_id": lease_id,
        },
        sort_keys=True,
    )
    marker = _auth_marker(directory, host, engine)
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    os.replace(tmp, marker)
    return until


def auth_expired_hosts(
    directory: Path, *, engine: str = AUTH_ENGINE_CLAUDE, now: datetime | None = None
) -> dict[str, str]:
    """Hosts whose ``engine`` login expired, to the stamp the failure was seen. An expired or unreadable marker is
    removed. The operator clears one by deleting ``.auth-expired-<engine>-<host>``."""
    out: dict[str, str] = {}
    if not directory.is_dir():
        return out
    stamp = _utc_stamp(now or datetime.now(UTC))
    prefix = f"{AUTH_EXPIRED_PREFIX}{engine}-"
    for marker in directory.glob(f"{prefix}*"):
        if marker.name.endswith(".tmp"):
            continue
        try:
            body = json.loads(marker.read_text(encoding="utf-8"))
            until, at = str(body.get("until") or ""), str(body.get("at") or "")
        except (OSError, ValueError, AttributeError):
            until = at = ""
        if until > stamp:
            out[marker.name[len(prefix) :]] = at
        else:
            marker.unlink(missing_ok=True)
    return out


@contextmanager
def _limit_lock(
    directory: Path, host: str, prefix: str = LIMITED_PREFIX
) -> Iterator[Path]:
    """Serialize worker notices and probe recovery; retain state outside the expiring marker glob."""
    state = directory / ".limit-state" / f"{prefix}{host}"
    state.mkdir(parents=True, exist_ok=True)
    with (state / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield state


def _limit_json(path: Path) -> dict[str, Any]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _write_limit_json(path: Path, body: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _claude_limit(
    operation: Literal["mark", "pool", "revoke", "auth", "probe", "passed", "reprobe"],
    **parameters: Any,
) -> ModelClaudeAccountLimitResult:
    return cast(
        ModelClaudeAccountLimitResult,
        NodeLanePlacementRecoveryEffect(globals()).handle(
            ModelClaudeAccountLimitRequest(operation=operation, **parameters)
        ),
    )


def _passed_at(state: Path, account: str | None) -> datetime | None:
    return _claude_limit("passed", state=state, account=account).recovered_at


def mark_limited(
    directory: Path,
    host: str,
    seconds: float,
    *,
    now: datetime | None = None,
    lease_id: int | None = None,
    prefix: str = LIMITED_PREFIX,
    observed_at: datetime | None = None,
    account: str | None = None,
    notice_id: str | None = None,
) -> str | None:
    """Apply a worker's limit once, returning its until stamp, or None for consumed/stale evidence.

    Receipts survive marker expiry, manual removal and successful probes. ``observed_at`` is the
    worker's evidence time, not the controller tick that happens to consume its old log.
    """
    now = now or datetime.now(UTC)
    observed_at = observed_at or now
    until = _utc_stamp(now + timedelta(seconds=seconds))
    body = {
        "host": host,
        "until": until,
        "at": _utc_stamp(now),
        "lease_id": lease_id,
        "observed_at": _utc_stamp(observed_at),
    }
    if account is not None:
        body["account"] = account
    if notice_id is not None:
        body["notice_id"] = notice_id
    with _limit_lock(directory, host, prefix) as state:
        marker = directory / f"{prefix}{host}"
        receipt = state / f"{lease_id}.json" if lease_id is not None else None
        if receipt is not None:
            if receipt.exists():
                return None
            current = _limit_json(marker)
            if current.get("lease_id") == lease_id:
                # Adopt a standing marker written by a pre-receipt worker.
                _write_limit_json(receipt, current)
                return None
        passed = _passed_at(state, account)
        stale = passed is not None and observed_at <= passed
        if receipt is not None:
            # Consume before the marker write: a crash must never replay a notice after recovery.
            _write_limit_json(receipt, {**body, "stale": stale})
        if stale:
            return None
        _write_limit_json(marker, body)
    return until


def limited_hosts(
    directory: Path, *, now: datetime | None = None, prefix: str = LIMITED_PREFIX
) -> dict[str, str]:
    """Hosts carrying a ``prefix`` mark (default: whose Claude login is out), to their until stamp. An expired or
    unreadable marker is removed, so the host is probed and placed as before."""
    out: dict[str, str] = {}
    if not directory.is_dir():
        return out
    stamp = _utc_stamp(now or datetime.now(UTC))
    for marker in directory.glob(f"{prefix}*"):
        if marker.name.endswith(".tmp"):
            continue
        host = marker.name[len(prefix) :]
        with _limit_lock(directory, host, prefix) as state:
            body = _limit_json(marker)
            if body.get("lease_id") is not None:
                receipt = state / f"{body['lease_id']}.json"
                if not receipt.exists():
                    _write_limit_json(receipt, body)
            until = str(body.get("until") or "")
            if until > stamp:
                out[host] = until
            else:
                marker.unlink(missing_ok=True)
    return out


# The token line prints the pool's status line with the label of the token it exported (OMN-17427); a limit then marks that token.


def _claude_room_verified(
    host: Host, capture: Capture
) -> tuple[bool, str | None, set[str]]:
    result = _claude_limit("probe", probe_host=host, capture=capture)
    return result.has_room, result.account, set(result.identities)


def mark_pool_token(
    host: str, text: str, seconds: float, *, capture: Capture | None = None
) -> bool:
    return _claude_limit(
        "pool", host=host, text=text, seconds=seconds, capture=capture
    ).has_room


def mark_claude_limited(
    directory: Path, host: str, text: str, seconds: float, **kwargs: Any
) -> str | None:
    return _claude_limit(
        "mark", directory=directory, host=host, text=text, seconds=seconds, **kwargs
    ).until


# A usage-limit marker holds a host out until the reset the notice named, but a reset or a token refresh can come hours before it
# (OMN-20864): ``.limited-h200`` stood until 13:01Z on 2026-10-15 after the limit cleared at about 03:00Z, and placement and every
# lab-fill run refused Claude there for hours. So while a marker stands, the host's Claude is asked one cheap question at most
# every LIMIT_REPROBE_S (the marker's ``probed_at``, else its ``at``), and the marker is removed when it answers. Anything but a clean
# answer (a limit notice, a nonzero exit, a timeout, an unreachable host) leaves the host benched.
LIMIT_REPROBE_S = 900.0
LIMIT_REPROBE_TIMEOUT_S = 120.0
LIMIT_REPROBE_SCRIPT = (
    '__BOUND__PATH="$HOME/.local/bin:$PATH"; export PATH\n'
    "command -v claude >/dev/null 2>&1 || exit 127\n"
    "__TOKEN_EXPORT__\n"
    'probe_bound __SMOKE_S__ claude -p "Reply with the single word ok" --model sonnet --output-format text 2>&1\n'
)


def limit_reprobe_script() -> str:
    return (
        LIMIT_REPROBE_SCRIPT.replace("__BOUND__", PROBE_BOUND_SH)
        .replace("__TOKEN_EXPORT__", bind_registry_roots(TOKEN_EXPORT))
        .replace("__SMOKE_S__", str(int(LIMIT_REPROBE_TIMEOUT_S) - 30))
    )


def limit_probe_answered(returncode: int, stdout: str) -> bool:
    """True when the probe's ``claude -p`` answered: exit 0, the word ``ok``, and none of the usage-limit notices."""
    text = stdout.lower()
    answer = "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith(("claude-pool ", "alarm claude-pool ", "claude-auth "))
    )
    return (
        returncode == 0
        and re.search(r"\bok\b", answer) is not None
        and not any(m in text for m in LIMIT_MARKERS)
    )


def _marker_time(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def reprobe_limited(
    directory: Path,
    hosts: Sequence[Host],
    *,
    capture: Capture | None = None,
    now: datetime | None = None,
    interval_s: float = LIMIT_REPROBE_S,
) -> dict[str, str]:
    return _claude_limit(
        "reprobe",
        directory=directory,
        hosts=tuple(hosts),
        capture=capture,
        now=now,
        interval_s=interval_s,
    ).limited


# The Codex engine's own marks (OMN-20531), kept apart from the Claude usage-limit mark so a Codex limit never takes a host's Claude
# placement away and the reverse. ``.codex-limited-<host>``: the host's Codex account ended a worker on a usage limit, until the reset
# the notice names (``limit_backoff_s``). ``.codex-sandbox-<host>``: a Codex worker found its workspace-write sandbox unable to write
# (``codex_sandbox_failure``), held out for CODEX_SANDBOX_BACKOFF_S. Nothing clears it early: ``read_pool`` lets a standing mark outrank the
# probe's cached smoke, so the mark only expires. It held h202 out of Codex for the six hours it then lasted on 2026-10-17 while the host's
# own smoke passed in 6 seconds (OMN-17427), so it now lasts the probe's failed-smoke cache (one hour).
CODEX_LIMITED_PREFIX = ".codex-limited-"
CODEX_SANDBOX_PREFIX = ".codex-sandbox-"
CODEX_SANDBOX_BACKOFF_S = 3600.0
CODEX_SANDBOX_MARKERS = (
    "failed to write file",
    "bwrap:",
    "read-only file system",
    "sandbox denied",
)
_INDEX_LOCK_DENIED_RE = re.compile(
    r"index\.lock.{0,120}(permission|not permitted|read-only)"
    r"|(permission|not permitted|read-only).{0,120}index\.lock",
    re.IGNORECASE | re.DOTALL,
)
CODEX_MESSAGE_PREFIX = "CODEX-MESSAGE: "
CODEX_EXIT_PREFIX = "landing_worker_run: remote engine codex_high "
_CODEX_EXIT_RE = re.compile(
    r"^landing_worker_run: remote engine codex_high .* exited (-?\d+)$", re.MULTILINE
)
_CODEX_WRITE_ERROR_RE = re.compile(
    r"^(?:error:\s*)?(?:bwrap:|failed to write file)", re.IGNORECASE
)


def mark_codex_limited(
    directory: Path,
    host: str,
    seconds: float,
    *,
    now: datetime | None = None,
    lease_id: int | None = None,
) -> str | None:
    """Take ``host`` out of Codex placement once per lease; return the until stamp or None if consumed."""
    return mark_limited(
        directory,
        host,
        seconds,
        now=now,
        lease_id=lease_id,
        prefix=CODEX_LIMITED_PREFIX,
    )


def codex_limited_hosts(
    directory: Path, *, now: datetime | None = None
) -> dict[str, str]:
    """Hosts whose Codex account hit a usage limit, to their until stamp."""
    return limited_hosts(directory, now=now, prefix=CODEX_LIMITED_PREFIX)


def mark_codex_sandbox_failed(
    directory: Path,
    host: str,
    *,
    now: datetime | None = None,
    lease_id: int | None = None,
) -> str | None:
    """Take ``host`` out of Codex placement for CODEX_SANDBOX_BACKOFF_S once per lease, or return None if consumed."""
    return mark_limited(
        directory,
        host,
        CODEX_SANDBOX_BACKOFF_S,
        now=now,
        lease_id=lease_id,
        prefix=CODEX_SANDBOX_PREFIX,
    )


def codex_sandbox_failed_hosts(
    directory: Path, *, now: datetime | None = None
) -> dict[str, str]:
    """Hosts whose Codex sandbox could not write, to their until stamp."""
    return limited_hosts(directory, now=now, prefix=CODEX_SANDBOX_PREFIX)


def codex_sandbox_failure(text: str) -> bool:
    """Judge diagnostics, excluding delegation failures and labelled model messages.

    A failed patch, bubblewrap startup or git index write can fail inside a successful engine turn.
    Other sandbox wording needs a nonzero engine exit; prose alone is not a workspace write probe.
    """
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if "DELEGATION-FAILED" not in ln
        and not ln.lstrip().startswith(CODEX_MESSAGE_PREFIX)
    ]
    for line in lines:
        if _CODEX_WRITE_ERROR_RE.match(line):
            return True
        if line.lower().startswith(
            ("fatal:", "error:")
        ) and _INDEX_LOCK_DENIED_RE.search(line):
            return True
    exits = _CODEX_EXIT_RE.findall("\n".join(lines))
    return bool(
        exits
        and int(exits[-1]) != 0
        and any(
            marker in ln.lower() for ln in lines for marker in CODEX_SANDBOX_MARKERS
        )
    )


# The macOS probe's CPU sample: ``top -l 2`` prints the since-boot average first and the live sample second, so the LAST
# ``CPU usage`` line is the one read. Leaves ``ci`` empty when top prints nothing parseable (the host then keeps the load1 bar).
MAC_CPU_IDLE_SH = r"""ci=$(top -l 2 -n 0 -s 1 2>/dev/null | awk '/CPU usage/{v=$0} END{if (match(v, /[0-9.]+% idle/)) {s=substr(v, RSTART, RLENGTH); sub(/%.*/, "", s); print s}}')"""
# The Mac's free memory (OMN-19998): the kernel's percent of memory free (kern.memorystatus_level, the figure ``memory_pressure``
# prints as the system-wide free percentage) and the machine's memory in bytes. A value that is not a number is dropped.
MAC_MEM_FREE_SH = r"""mf=$(sysctl -n kern.memorystatus_level 2>/dev/null); mt=$(sysctl -n hw.memsize 2>/dev/null)
case "$mf" in ''|*[!0-9]*) mf=;; esac; case "$mt" in ''|*[!0-9]*) mt=;; esac"""

# A per-tool check of the probe runs under ``probe_bound`` (OMN-20864): its own short timeout, in its own process group, killed with the
# whole group when the timeout passes. A hung tool then costs its own few seconds and marks only that tool (``codex=unavailable``, no
# Claude login, ``GHAUTH unreachable``) instead of running the probe into PROBE_TIMEOUT_S and refusing the whole host for every unit
# (2026-10-12: a macOS Gatekeeper prompt hung every ``codex`` call on the launching host, and its orphaned ``codex login status``
# processes piled up). The bound is perl, which every macOS and Linux host carries (a Mac has no ``timeout`` or ``setsid``); a host
# without perl falls back to a poll that kills the one process. The function has no single quote, so a ``nohup sh -c '...'`` block of the
# probe embeds it. Exit status: the tool's own; 124 on a timeout; 126 or 127 when it did not start; 128+n when a signal ended it.
PROBE_TOOL_TIMEOUT_S = 3
# The background smokes (a ``codex exec`` and a ``claude -p`` model call, nohup'd, never waited on by the probe) get a longer bound, so a
# hung one is killed with its group instead of piling up every ten minutes. A smoke the bound kills leaves no pass or fail mark.
PROBE_SMOKE_TIMEOUT_S = 180
PROBE_BOUND_PERL = r"""use POSIX ();
my ($s, @c) = @ARGV;
my $p = fork();
exit 127 unless defined $p;
if (!$p) { POSIX::setpgid(0, 0); open(STDIN, "<", "/dev/null"); exec { $c[0] } @c; exit 127; }
POSIX::setpgid($p, $p);
$SIG{ALRM} = sub { kill("TERM", -$p); select(undef, undef, undef, 0.3); kill("KILL", -$p); waitpid($p, 0); exit 124; };
alarm($s);
waitpid($p, 0);
alarm(0);
my $r = $?;
kill("TERM", -$p);
exit($r & 127 ? 128 + ($r & 127) : $r >> 8);"""
PROBE_BOUND_FN = r"""probe_bound() {
  pb_s=$1; shift
  if command -v perl >/dev/null 2>&1; then perl -e "$PB_PERL" -- "$pb_s" "$@"; return $?; fi
  "$@" </dev/null & pb_p=$!; pb_i=0; pb_tick=1
  while kill -0 "$pb_p" 2>/dev/null; do
    if [ "$pb_i" -ge "$pb_s" ]; then kill -s KILL "$pb_p" 2>/dev/null; wait "$pb_p" 2>/dev/null; return 124; fi
    sleep "$pb_tick"; pb_i=$((pb_i + pb_tick))
  done
  wait "$pb_p"
}"""
PROBE_BOUND_SH = (
    "PB_PERL='" + PROBE_BOUND_PERL + "'; export PB_PERL\n" + PROBE_BOUND_FN + "\n"
)

# The clone half of the parity probe and the Codex half of the pool probe are those nodes' (OMN-17427): the remote-lane runner
# renders the same lines.
_clone_parity: Any = handler_clone_parity


def _codex_preflight_sh(env: Mapping[str, str] | None = None) -> str:
    """The Codex half of the pool probe, rendered by its node. The private data clone's directory name is the
    ``repos.private_data`` overlay key."""
    request = ModelCodexSandboxProbeRequest(
        tool_timeout_s=int(PROBE_TOOL_TIMEOUT_S),
        smoke_timeout_s=int(PROBE_SMOKE_TIMEOUT_S),
        bound_fn=PROBE_BOUND_FN,
        private_data=str(resolve_overlay_key("repos.private_data", environ=_env(env))),
    )
    return str(NodeCodexSandboxProbeCompute().handle(request).preflight_codex_sh)


# The token line of the limit re-probe, the remote-lane runner and the landing worker's remote engine (run after
# ``unset ANTHROPIC_API_KEY``, with claude on PATH). The host's own login is kept while it answers: on h200 the host's claude.ai
# login answered while the token's account answered a weekly limit, and exporting the token first benched the host for Claude all
# night (OMN-20864). The token is exported when the host reads no login (a keychain locked over ssh, no login at all), and also
# when the host login reads loggedIn but a real call answers a usage-limit notice: on h201 the native login was signed in and at
# its weekly limit while the token file held a second account, so every landing worker ended in 0.7 s on "You've hit your weekly
# limit" and re-marked the host limited (OMN-19998, lease 9528). The call is bounded and its verdict is stamped for
# ``TOKEN_HOST_STAMP_MIN`` minutes under ``$HOME/.cache/omni``, so a burst of workers asks once. Any other failure of the call
# keeps the host login, as before. The call is spelled ``--print`` so that a script's first ``claude -p`` is still its engine.
TOKEN_HOST_STAMP_MIN = 10
TOKEN_HOST_CALL_S = 60
# The host's Claude token pool (OMN-17427): one local-store entry per account, served by omnimarket's node_claude_token_pool_effect
# under the host's dispatch venv (``$CPY``; ``ONEX_CLAUDE_POOL_PY`` names another interpreter, as the tests do). The token line
# exports the first token whose limit has passed and prints the pool's status line, which names its label (never its value);
# with no such token the token file and the host login apply as before.
CLAUDE_POOL_MODULE = "omnimarket.nodes.node_claude_token_pool_effect"
# The roots are the ones every other remote script here searches (``registry_roots_sh``): over a non-login ssh session the
# registry-root variable is unset, and a host's venv may live only under one of the overlay's candidates (2026-10-18: without it
# the probe saw no pool and read the token file's account).
CLAUDE_POOL_SH = (
    'CPY="${ONEX_CLAUDE_POOL_PY:-}"; [ -n "$CPY" ] || for r in __REGISTRY_ROOTS__; do if [ -n "$r" ] && [ -x "$r/.onex-dispatch-venv/bin/python" ]; '
    'then CPY="$r/.onex-dispatch-venv/bin/python"; break; fi; done'
)
TOKEN_EXPORT = (
    "unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN\n"
    + PROBE_BOUND_SH
    + CLAUDE_POOL_SH
    + f"""\nT="$HOME/{CLAUDE_TOKEN_FILE}"; K="$HOME/.cache/omni"; mkdir -p "$K"
CPL=$(mktemp "$K/claude-pool.XXXXXX") || exit 70
if [ -n "$CPY" ] && CP=$(probe_bound 30 "$CPY" -m {CLAUDE_POOL_MODULE} resolve 2>"$CPL"); then
  export CLAUDE_CODE_OAUTH_TOKEN="$CP"; unset CP; cat "$CPL"; mv -f "$CPL" "$K/claude-pool.line"
elif [ -r "$T" ]; then
  if ! claude auth status 2>/dev/null | tr -d ' \\n' | grep -v '"authMethod":"oauth_token"' | grep -q '"loggedIn":true'; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$T")"
  elif [ -n "$(find "$K/claude-host-limited" -mmin -{TOKEN_HOST_STAMP_MIN} 2>/dev/null)" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$T")"
  elif [ -z "$(find "$K/claude-host-ok" -mmin -{TOKEN_HOST_STAMP_MIN} 2>/dev/null)" ]; then
    tb={TOKEN_HOST_CALL_S}
    h=$(probe_bound "$tb" claude --print "Reply with the single word ok" --model sonnet --output-format text 2>&1); hrc=$?
    h=$(printf "%s" "$h" | tr "[:upper:]" "[:lower:]")
    case "$h" in
      *"hit your"*|*"have hit"*|*"limit reached"*|*"out of extra usage"*)
        touch "$K/claude-host-limited"; rm -f "$K/claude-host-ok"
        export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$T")" ;;
      *)
        if [ "$hrc" = 0 ]; then touch "$K/claude-host-ok"; fi ;;
    esac
  fi
fi
rm -f "$CPL"
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
  python3 -c 'import hashlib, os; print("claude-auth account=token-" + hashlib.sha256(os.environ["CLAUDE_CODE_OAUTH_TOKEN"].encode()).hexdigest())'
else
  probe_bound 10 claude auth status 2>/dev/null | python3 -c '
import hashlib, json, sys
try:
    status = json.load(sys.stdin)
    email = status.get("email")
    if status.get("loggedIn") and isinstance(email, str) and email.strip():
        print("claude-auth account=native-" + hashlib.sha256(email.strip().lower().encode()).hexdigest())
except (ValueError, AttributeError):
    pass'
fi"""
)

# The preflight half of the probe (OMN-20531), run only over ssh (a lab host): the launching host's workdir is the worktree root, which the
# controller already writes. WORKDIR: a write test of ``$HOME/lab-run`` (the worker's worktree, prompt and result live there).
# CODEXLOGIN: ``codex login status``. CODEXSANDBOX: the cached smoke below. It runs ``codex exec -s workspace-write`` with the
# worker's own shape (a linked worktree of a repository, its git directory and one more ``--add-dir`` directory) and asks it to commit a
# file in the worktree and touch a file in the other directory; both must have happened. A pass is
# kept for six hours, a failure for one (with its cause, and only while the codex version, bwrap and AppArmor state it ran against are unchanged), and a check runs in the background at most every ten minutes, so a placement read never waits
# on a model call and the smoke costs a few tokens a host a day. Until a result exists the line says ``unknown``, which
# ``Reading.codex_refusal`` does not refuse. The Claude login check above is the same shape.
# The Codex half also runs on the launching host when ``probe_script(codex_local=True)`` sets PROBE_CODEX_LOCAL (its table row marks
# ``codex: true``, OMN-20864), so ``Reading.codex_refusal`` reads its login and sandbox like a lab host's; WORKDIR stays ssh-only.
PREFLIGHT_SH = r"""
if [ -n "$SSH_CLIENT" ] || [ -n "$SSH_CONNECTION" ]; then
  wd=fail
  if mkdir -p "$HOME/lab-run/runs" 2>/dev/null; then
    t=$(mktemp "$HOME/lab-run/runs/.probe.XXXXXX" 2>/dev/null) && rm -f "$t" && wd=ok
  fi
  echo "WORKDIR $wd"
fi
"""

# The parity half of the probe (OMN-20266). ``PARITY_PY`` runs on the host under its own python3 (3.6 or newer, stdlib only) and
# prints one line per fact: PARITY-HOME (the registry root), PARITY-CLONE <name> <present|missing> <head> <tip> <hook 1|0|->,
# PARITY-CORE-WORKTREE <name> <directory> (only for a clone whose core.worktree names a directory other than the clone),
# PARITY-VENV <omnimarket commit of the dispatch venv|none>, PARITY-PLUGIN <name> <ok|modified|unverifiable> <installed tree digest>
# <shipped tree digest> <revision> <detail>. A tree digest is a sha256 over the sorted (path, git blob id) pairs of every file but the
# build residue (.venv, __pycache__, .git, caches, .pyc) and Claude Code's top-level ``.in_use/`` marker, so the installed tree and ``git ls-tree -r`` of the revision are comparable.
# The shell half adds PARITY-GH, PARITY-PYTEST and PARITY-ERROR. The registry root is ``$ONEX_REGISTRY_ROOT`` when it is a directory, else the
# first candidate that holds the registry's ``omnibase_infra`` clone (the order the remote-lane runner uses).

PARITY_PY = _PARITY_PY.replace(
    "__CLONE_PARITY_PY__", getattr(_clone_parity, "CLONE_PARITY_PY", "")
)


# The GitHub credential check (OMN-21016): sets ghv (the verdict) and g (1 when ok) and prints the GHAUTH line. POSIX sh, so it
# runs under dash, bash and a Mac's zsh login shell; the timeout is a background kill because a Mac has no ``timeout``. The
# check is ``gh api rate_limit``, which GitHub counts against no bucket and the lane gh shim allows; ``gh auth status`` reports
# a 5xx as an invalid token, which is the failure this replaces.
GH_CREDENTIAL_SH = r"""
__BOUND__
ghv=missing; ghs=live; gha=-; ghd=no-gh
GK="$HOME/.cache/omni"; GO="$GK/__GH_OK__"
if command -v gh >/dev/null 2>&1; then
  mkdir -p "$GK" 2>/dev/null; ghd=-
  if [ -n "$(find "$GO" -mmin -__GH_FRESH_MIN__ 2>/dev/null)" ]; then ghv=ok; ghs=cached
  elif probe_bound __TOOL_S__ gh auth token >/dev/null 2>&1; then
    GT="$GK/gh-credential-check.$$"
    probe_bound __GH_TIMEOUT_S__ env ONEX_GH_WARN=0 gh api rate_limit --include >"$GT" 2>&1; grc=$?
    gst=$(sed -n -e 's/^HTTP\/[0-9.]* \([0-9][0-9][0-9]\).*/\1/p' -e 's/.*(HTTP \([0-9][0-9][0-9]\)).*/\1/p' "$GT" 2>/dev/null | head -n 1)
    rm -f "$GT"
    case "$gst" in
      2??) ghv=ok; ghd="http=$gst"; touch "$GO" ;;
      401) ghv=expired; ghd=http=401; rm -f "$GO" ;;
      ?*) ghv=unreachable; ghd="http=$gst" ;;
      *) ghv=unreachable; ghd=no-response; [ "$grc" -ge 124 ] && ghd=timeout ;;
    esac
  else
    if [ "$?" -ge 124 ]; then ghv=unreachable; ghd=timeout
    else ghv=expired; ghd=no-credential; rm -f "$GO"; fi
  fi
  if [ "$ghv" = unreachable ] && [ -n "$(find "$GO" -mmin -__GH_FALLBACK_MIN__ 2>/dev/null)" ]; then ghv=ok; ghs=fallback; fi
  if [ -f "$GO" ]; then
    gm=$(stat -c %Y "$GO" 2>/dev/null || stat -f %m "$GO" 2>/dev/null); gn=$(date +%s)
    case "$gm" in ''|*[!0-9]*) ;; *) gha=$((gn - gm)) ;; esac
  fi
fi
g=0; [ "$ghv" = ok ] && g=1
echo "GHAUTH $ghv $ghs $gha $ghd"
"""


def gh_credential_script(
    *,
    fresh_min: int = GH_FRESH_MIN,
    fallback_min: int = GH_FALLBACK_MIN,
    timeout_s: int = GH_CHECK_TIMEOUT_S,
) -> str:
    """The shell block that sets the host's GitHub credential verdict and prints its GHAUTH line (OMN-21016)."""
    return (
        GH_CREDENTIAL_SH.replace("__BOUND__", PROBE_BOUND_SH)
        .replace("__TOOL_S__", str(PROBE_TOOL_TIMEOUT_S))
        .replace("__GH_OK__", GH_OK_FILE)
        .replace("__GH_FRESH_MIN__", str(int(fresh_min)))
        .replace("__GH_FALLBACK_MIN__", str(int(fallback_min)))
        .replace("__GH_TIMEOUT_S__", str(int(timeout_s)))
    )


PARITY_SH = r"""
PARITY_H=""
for d in __REGISTRY_ROOTS__; do
  if [ -n "$d" ] && [ -d "$d" ] && { [ "$d" = "${ONEX_REGISTRY_ROOT:-}" ] || [ -d "$d/omnibase_infra" ]; }; then PARITY_H="$d"; break; fi
done
[ -n "${ghv:-}" ] || {
__GH_CREDENTIAL__
}
pg=0; [ "$ghv" = ok ] && pg=1
PYT=__PYTEST_PYTHON_SH__
pp=0; command -v "$PYT" >/dev/null 2>&1 && "$PYT" -c 'import pytest' >/dev/null 2>&1 && pp=1
echo "PARITY-GH $pg"
echo "PARITY-PYTEST $pp __PYTEST_PYTHON__"
# Where gh resolves for the runner's PR head fetch: the ssh session's own PATH (recorded before the probe exports its own) under the
# fetch's export, the one line both use (``fetch_path_export``).
gp=$( __FETCH_PATH_EXPORT__; command -v gh 2>/dev/null ) || gp=""
echo "PARITY-GHPATH ${gp:-none}"
# Where pre-commit and uvx resolve with the PATH lanes run with (the same export as gh above): hook-preflight runs ``pre-commit``
# and lab-run runs ``uvx`` tools, and a host with neither answers exit 127.
hp=$( __FETCH_PATH_EXPORT__; command -v pre-commit 2>/dev/null ) || hp=""
hx=$( __FETCH_PATH_EXPORT__; command -v uvx 2>/dev/null ) || hx=""
echo "PARITY-HOOKTOOLS ${hp:-none} ${hx:-none}"
if command -v python3 >/dev/null 2>&1; then
python3 - "$PARITY_H" "__CLONES__" "__PLUGINS__" "__PLUGIN_REPO__" "__BUS_LANE__" <<'PARITY_PY_EOF'
__PARITY_PY__
PARITY_PY_EOF
prc=$?; [ "$prc" -eq 0 ] || echo "PARITY-ERROR the parity probe exited $prc"
else
echo "PARITY-ERROR python3 is not installed"
fi
"""


def parity_python_path(value: Any) -> str | None:
    """A row's ``parity_python`` when it is a plain path under the registry root, else None (the check then uses ``python3``)."""
    ok = (
        isinstance(value, str)
        and PARITY_PYTHON_RE.match(value)
        and ".." not in value.split("/")
    )
    return value if ok else None


def parity_probe_script(
    clones: Sequence[str],
    pytest_python: str | None = None,
    *,
    bus_lane: str | None = None,
    extra_clones: Sequence[str] = (),
    github_cli: bool = True,
    peers: Sequence[str] = (),
) -> str:
    """PARITY lines for clones, plugins, the declared interpreter and capabilities (``peers``: other hosts' ssh targets); node-only hosts never execute gh."""
    # ``extra_clones`` (the plugin repository's clone and the clones units ask for) are read too, so a unit whose repository is
    # not on the host's row is still judged (``ParityNeeds.clones``).
    names = ",".join(
        dict.fromkeys(c for c in (*clones, *extra_clones) if CLONE_NAME_RE.match(c))
    )
    declared = parity_python_path(pytest_python)
    # A declared interpreter is under the registry root the block found; with no root it names no file, so the check fails.
    interpreter = (
        f'"${{PARITY_H:-/nonexistent-registry-root}}/{declared}"'
        if declared
        else PARITY_DEFAULT_PYTHON
    )
    lane = bus_lane if bus_lane and CLONE_NAME_RE.match(bus_lane) else ""
    # ``PROBE_ENTRY_PATH`` is the PATH the ssh session started with, which the whole probe records before it exports its own.
    entry = fetch_path_export("${PROBE_ENTRY_PATH-$PATH}")
    credential = (
        gh_credential_script()
        if github_cli
        else 'g=0; ghv=missing\necho "PARITY-GH-POLICY absent"'
    )
    return bind_registry_roots(PARITY_SH).replace(
        "__GH_CREDENTIAL__", credential
    ).replace("__CLONES__", names).replace("__FETCH_PATH_EXPORT__", entry).replace(
        "__BUS_LANE__", lane
    ).replace("__PLUGINS__", ",".join(PARITY_PLUGINS)).replace(
        "__PLUGIN_REPO__", parity_plugin_repo()
    ).replace("__PARITY_PY__", PARITY_PY).replace(
        "__PYTEST_PYTHON_SH__", interpreter
    ).replace(
        "__PYTEST_PYTHON__", declared or PARITY_DEFAULT_PYTHON
    ) + host_capability.capability_probe_sh(
        peers, bound=PROBE_BOUND_SH, path_export=entry
    )


# The hook parse half of the probe (OMN-20961). ``HOOKPARSE_PY`` runs under the host's python3 (3.6 or newer, stdlib only) and
# judges every hook script the host's Claude loads with the host's own /bin/bash (``bash -n``, which parses and runs nothing): the
# ``hooks/`` tree of each installed plugin ``~/.claude/settings.json`` does not disable (its cache copy, and for a ``directory``
# marketplace the checkout Claude loads it from), every ``${CLAUDE_PLUGIN_ROOT}/...sh`` its
# ``hooks.json`` names outside that tree, and the same for each tree named in its second argument (paths under ``$HOME``, the
# remote-lane runner's shipped plugin trees; one that is not there yet is skipped). A file with a shebang naming no sh or bash is
# not a bash script and is skipped. It prints one line: ``HOOKPARSE ok <files> <trees>``, ``HOOKPARSE fail <n> <tree>:<file>
# <first error line>``, or ``HOOKPARSE unknown <why>`` when it cannot judge (the plugin list unreadable, bash missing or hung).
HOOK_PARSE_OK, HOOK_PARSE_FAIL, HOOK_PARSE_UNKNOWN = "ok", "fail", "unknown"
HOOKPARSE_PY = r'''import json, os, re, subprocess, sys
bash, named = sys.argv[1], sys.argv[2]
home = os.path.expanduser("~")
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}


def clean(text, limit=200):
    return ("_".join(str(text).split()) or "-")[:limit]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


try:
    listed = load(os.path.join(home, ".claude/plugins/installed_plugins.json")).get("plugins") or {}
    enabled = load(os.path.join(home, ".claude/settings.json")).get("enabledPlugins") or {}
    markets = load(os.path.join(home, ".claude/plugins/known_marketplaces.json"))
except (OSError, ValueError, AttributeError) as exc:
    print("HOOKPARSE unknown plugin-config-unreadable:" + clean(exc, 120))
    sys.exit(0)


def directory_tree(key):
    """A plugin of a ``directory`` marketplace is loaded from the marketplace's own checkout, not from its cache copy (h101,
    2026-10-11: the cached onex parsed, the clone it is loaded from did not). None: not a directory marketplace."""
    name, _, market = key.partition("@")
    row = markets.get(market) if isinstance(markets, dict) else None
    source = row.get("source") if isinstance(row, dict) else None
    if not isinstance(source, dict) or source.get("source") != "directory":
        return None
    top = str(row.get("installLocation") or source.get("path") or "")
    manifest = load(os.path.join(top, ".claude-plugin/marketplace.json"))
    rel = next((p.get("source") for p in manifest.get("plugins") or () if isinstance(p, dict) and p.get("name") == name), None)
    if not isinstance(rel, str):
        raise ValueError("no-directory-source-for-" + key)
    return os.path.realpath(os.path.join(top, rel))


trees = []
try:
    for key, entries in sorted(listed.items()) if isinstance(listed, dict) else []:
        if isinstance(enabled, dict) and enabled.get(key) is False:
            continue
        for entry in entries if isinstance(entries, list) else []:
            where = str((entry or {}).get("installPath") or "") if isinstance(entry, dict) else ""
            if where and os.path.isdir(where):
                trees.append((key, where))
        loaded = directory_tree(key)
        if loaded is not None:
            if not os.path.isdir(loaded):
                raise ValueError("directory-source-missing-" + loaded)
            trees.append((key + ":directory", loaded))
except (OSError, ValueError, AttributeError) as exc:
    print("HOOKPARSE unknown marketplace-unreadable:" + clean(exc, 160))
    sys.exit(0)
for rel in [r for r in named.split(",") if r]:
    where = os.path.join(home, rel)
    if os.path.isdir(where):
        trees.append((rel, where))
files = []
for name, top in trees:
    hooks = os.path.join(top, "hooks")
    for here, dirs, names in os.walk(hooks):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        files += [(name, top, os.path.join(here, f)) for f in sorted(names) if f.endswith(".sh")]
    try:
        with open(os.path.join(hooks, "hooks.json")) as f:
            spec = f.read()
    except OSError:
        spec = ""
    for rel in sorted(set(re.findall(r"\$\{?CLAUDE_PLUGIN_ROOT\}?/([A-Za-z0-9_./-]+\.sh)", spec))):
        full = os.path.normpath(os.path.join(top, rel))
        if os.path.isfile(full) and not full.startswith(hooks + os.sep):
            files.append((name, top, full))
bad = []
for name, top, path in files:
    try:
        with open(path, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
    except OSError as exc:
        print("HOOKPARSE unknown unreadable:" + clean(os.path.relpath(path, top)) + ":" + clean(exc, 80))
        sys.exit(0)
    if first.startswith("#!") and not re.search(r"(^|[/ ])(ba)?sh(\s|$)", first[2:]):
        continue
    try:
        done = subprocess.run([bash, "-n", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except subprocess.TimeoutExpired:
        print("HOOKPARSE unknown bash-n-timed-out:" + clean(os.path.relpath(path, top)))
        sys.exit(0)
    except OSError as exc:
        print("HOOKPARSE unknown " + clean(bash) + "-did-not-run:" + clean(exc, 80))
        sys.exit(0)
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip().splitlines() or ["exit %d" % done.returncode]
        bad.append((name, os.path.relpath(path, top), err[0].replace(path + ": ", "")))
if bad:
    name, rel, err = bad[0]
    print("HOOKPARSE fail %d %s:%s %s" % (len(bad), clean(name), clean(rel), clean(err)))
else:
    print("HOOKPARSE ok %d %d" % (len(files), len(trees)))
'''

HOOKPARSE_SH = r"""
if [ ! -x /bin/bash ]; then echo "HOOKPARSE unknown no-/bin/bash"
elif ! command -v python3 >/dev/null 2>&1; then echo "HOOKPARSE unknown no-python3"
else
python3 - /bin/bash "__HOOK_TREES__" <<'HOOKPARSE_PY_EOF'
__HOOKPARSE_PY__
HOOKPARSE_PY_EOF
hrc=$?; [ "$hrc" -eq 0 ] || echo "HOOKPARSE unknown the-hook-parse-check-exited-$hrc"
fi
"""
HOOK_TREE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def hook_parse_script(hook_trees: Sequence[str] = ()) -> str:
    """The shell block that prints a host's HOOKPARSE line for its installed plugins and ``hook_trees`` (paths under the host's
    ``$HOME``; a name that is not a plain relative path is dropped)."""
    names = ",".join(
        dict.fromkeys(
            t.strip("/")
            for t in hook_trees
            if HOOK_TREE_RE.match(t) and ".." not in t.split("/")
        )
    )
    return HOOKPARSE_SH.replace("__HOOK_TREES__", names).replace(
        "__HOOKPARSE_PY__", HOOKPARSE_PY
    )


def parse_hook_parse(text: str) -> tuple[str, str] | None:
    """(state, detail) from the probe's last HOOKPARSE line, or None when it printed none. A state the probe never prints
    reads ``unknown``."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split(" ", 2)
        if parts[0] == "HOOKPARSE" and len(parts) >= 2:
            state = (
                parts[1]
                if parts[1] in (HOOK_PARSE_OK, HOOK_PARSE_FAIL, HOOK_PARSE_UNKNOWN)
                else HOOK_PARSE_UNKNOWN
            )
            return state, (parts[2].strip() if len(parts) == 3 else "")
    return None


_PROBE_BASE = (
    r"""
PROBE_ENTRY_PATH="$PATH"
export PATH="__FETCH_PATH_DIRS__:$PATH:/usr/sbin:/sbin"
unset ANTHROPIC_API_KEY
__BOUND__
[ -n "$USER" ] || { USER=$(id -un); LOGNAME=$USER; export USER LOGNAME; }
u=$(uname -s)
if [ "$u" = Linux ]; then
  tm() { cpu=; dimm=; for h in /sys/class/hwmon/hwmon*; do
    case "$(cat "$h/name" 2>/dev/null)" in
      k10temp|coretemp) for f in "$h"/temp*_label; do
        case "$(cat "$f" 2>/dev/null)" in Tctl|"Package id "*) cpu=$(cat "${f%_label}_input" 2>/dev/null);; esac; done;;
      spd5118) v=$(cat "$h/temp1_input" 2>/dev/null)
        case "$v" in ''|*[!0-9]*) ;; *) { [ -z "$dimm" ] || [ "$v" -gt "$dimm" ]; } && dimm=$v;; esac;;
    esac; done
    case "$cpu" in ''|*[!0-9]*) cpu=-;; esac; echo "$cpu ${dimm:--}"; }
  n=$(nproc); l=$(cut -d' ' -f1 /proc/loadavg); m=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  w1=$(tm)
  x1=$(awk '/^pswpin /{i=$2} /^pswpout /{o=$2} END{print i+0, o+0}' /proc/vmstat)
  c1=$(awk '/^cpu /{t=0; for(i=2;i<=9;i++) t+=$i; print $5+$6, t}' /proc/stat); sleep 1
  c2=$(awk '/^cpu /{t=0; for(i=2;i<=9;i++) t+=$i; print $5+$6, t}' /proc/stat)
  x2=$(awk '/^pswpin /{i=$2} /^pswpout /{o=$2} END{print i+0, o+0}' /proc/vmstat)
  w2=$(tm)
  sleep 1
  x3=$(awk '/^pswpin /{i=$2} /^pswpout /{o=$2} END{print i+0, o+0}' /proc/vmstat)
  g=$(getconf PAGESIZE 2>/dev/null)
  sio=$(echo "$g $x1 $x2 $x3" | awk 'NF==7 && $1>0 {printf "%d %d %d %d %d", $1/1024, $4-$2, $5-$3, $6-$4, $7-$5}')
  th=$(echo "$w1 $w2" | awk '{c=0;nc=0;d=0;nd=0; if($1!="-"){c+=$1;nc++} if($3!="-"){c+=$3;nc++} if($2!="-"){d+=$2;nd++} if($4!="-"){d+=$4;nd++}
    if(nc||nd) printf "%s %s", (nc?sprintf("%.1f",c/nc/1000):"-"), (nd?sprintf("%.1f",d/nd/1000):"-")}')
  k=$(awk '/^some /{for(i=2;i<=NF;i++) if ($i ~ /^avg60=/) {sub(/^avg60=/, "", $i); print $i}}' /proc/pressure/memory 2>/dev/null)
  b=$(echo "$c1 $c2" | awk -v n="$n" '{di=$3-$1; dt=$4-$2; if (dt>0) printf "%.2f", n*(1-di/dt)}')
  w=$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{print t+0, f+0}' /proc/meminfo)
else
  n=$(sysctl -n hw.ncpu); l=$(sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1}')
  p=$(sysctl -n hw.pagesize)
  q=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)
  m=$(vm_stat | awk -v p="$p" '/Pages free/{f=$3} /Pages inactive/{i=$3} /Pages speculative/{s=$3} END{printf "%d",(f+i+s)*p/1024}')
  y1=$(vm_stat | awk '/^Swapins/{i=$2} /^Swapouts/{o=$2} END{print i+0, o+0}')
  sleep 1
  y2=$(vm_stat | awk '/^Swapins/{i=$2} /^Swapouts/{o=$2} END{print i+0, o+0}')
  sleep 1
  y3=$(vm_stat | awk '/^Swapins/{i=$2} /^Swapouts/{o=$2} END{print i+0, o+0}')
  sio=$(echo "$p $y1 $y2 $y3" | awk 'NF==7 && $1>0 {printf "%d %d %d %d %d", $1/1024, $4-$2, $5-$3, $6-$4, $7-$5}')
__MAC_CPU_IDLE__
__MAC_MEM_FREE__
  a=${SSH_CLIENT%% *}; lg=unknown
  if [ -n "$a" ] && command -v python3 >/dev/null 2>&1; then
    lg=$(python3 -c 'import errno,socket,sys
s=socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET); s.settimeout(2)
try: s.connect((sys.argv[1], 22)); print("ok")
except OSError as e: print("denied" if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH) else "ok")' "$a" 2>/dev/null) || lg=unknown
  fi
  echo "LANGRANT ${lg:-unknown}"
fi
echo "PROBE $n $l $m"
[ -n "$b" ] && echo "BUSY $b"
[ -n "$ci" ] && echo "CPUIDLE $ci"
[ -n "$w" ] && echo "SWAP $w"
[ -n "$sio" ] && echo "SWAPIO $sio"
[ -n "$k" ] && echo "MEMPSI $k"
[ -n "$q" ] && echo "PRESSURE $q"
[ -n "$mf" ] && [ -n "$mt" ] && echo "MEMFREE $mf $mt"
[ -n "$th" ] && echo "THERMAL $th"
__CLAUDE_POOL__
cp=""; [ -n "$CPY" ] && cp=$(probe_bound 10 "$CPY" -m __POOL_MODULE__ status) && echo "CLAUDEPOOL $cp"
c=0; cw=no-claude
if command -v claude >/dev/null 2>&1; then
  # Preserve the bound status before stripping output, so a hung auth check starts no smoke.
  s=$(probe_bound __TOOL_S__ claude auth status 2>/dev/null); arc=$?
  s=$(printf "%s" "$s" | tr -d ' \n')
  if [ "$arc" -ge 124 ]; then cw=auth-unavailable
  else
    case "$s" in
      *'"authMethod":"oauth_token"'*) ;;
      *'"loggedIn":true'*) c=1; cw=host-login ;;
    esac
  fi
  # A pool with a token whose limit has passed is a login (OMN-17427): the token line exports it.
  case "$cp" in "claude-pool "*" tokens=0 "*) ;; "claude-pool "*) [ "$c" = 1 ] || { c=1; cw=pool; } ;; esac
  if [ "$c" = 0 ]; then
    T="$HOME/__TOKEN__"; [ "$arc" -lt 124 ] && [ -r "$T" ] && export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$T")"
    K="$HOME/.cache/omni"; mkdir -p "$K"
    # A timed-out check reads the six-hour cache of a real answered call (no process started); only the background smoke below waits
    # for a check that finished, so a hung check starts no second Claude process (OMN-21751).
    if [ -n "$(find "$K/claude-login-ok" -mmin -360 2>/dev/null)" ]; then c=1; cw=call-ok
    elif [ "$arc" -lt 124 ]; then
      cw=unchecked
      if [ -r "$K/claude-login-failed" ]; then
        f=$(head -c 40 "$K/claude-login-failed" | tr -cd 'a-z-')
        if [ -r "$T" ]; then cw="token-failed:$f"
        elif [ "$f" = auth ]; then cw=no-login
        else cw="call-failed:$f"; fi
      fi
      if [ -z "$(find "$K/claude-login-check" -mmin -10 2>/dev/null)" ]; then
        touch "$K/claude-login-check"
        nohup sh -c '__BOUND_FN__
          o=$(probe_bound __SMOKE_S__ claude -p "Reply with the single word ok" --model sonnet --output-format text 2>"$0/claude-login-err"); r=$?
          if [ "$r" = 0 ] && printf "%s\n" "$o" | grep -qiwx ok; then touch "$0/claude-login-ok"; rm -f "$0/claude-login-failed"
          else
            case "$(printf "%s %s" "$o" "$(cat "$0/claude-login-err")" | tr "[:upper:]" "[:lower:]")" in
              *limit*) w=usage-limit ;;
              *"failed to authenticate"*|*"not logged in"*|*authentication_error*|*"invalid api key"*|*"/login"*) w=auth ;;
              " ") w=no-answer ;;
              *) w=error ;;
            esac
            echo "$w" > "$0/claude-login-failed"; rm -f "$0/claude-login-ok"
          fi; rm -f "$0/claude-login-err"' "$K" >/dev/null 2>&1 </dev/null &
      fi
    fi
  fi
fi
echo "CLAUDEWHY $cw"
__GH_CREDENTIAL__
ca=unreadable; f="$HOME/.claude/settings.json"
if [ ! -e "$f" ]; then ca=nofile
elif command -v python3 >/dev/null 2>&1; then
  ca=$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get("includeCoAuthoredBy"); print("false" if v is False else "unset" if v is None else "other")' "$f" 2>/dev/null) || ca=unreadable
elif grep -Eq '"includeCoAuthoredBy"[[:space:]]*:[[:space:]]*false' "$f" 2>/dev/null; then ca=false; fi
echo "COAUTHOR ${ca:-unreadable}"
__PARITY__
__HOOKPARSE__
echo "LOGIN $c $g"
__PREFLIGHT__
""".replace("__MAC_CPU_IDLE__", MAC_CPU_IDLE_SH)
    .replace("__MAC_MEM_FREE__", MAC_MEM_FREE_SH)
    .replace("__TOKEN__", CLAUDE_TOKEN_FILE)
)
_PROBE_BASE = _PROBE_BASE.replace("__CLAUDE_POOL__", CLAUDE_POOL_SH).replace(
    "__POOL_MODULE__", CLAUDE_POOL_MODULE
)
_PROBE_BASE = (
    _PROBE_BASE.replace("__BOUND__", PROBE_BOUND_SH)
    .replace("__BOUND_FN__", PROBE_BOUND_FN)
    .replace("__TOOL_S__", str(PROBE_TOOL_TIMEOUT_S))
    .replace("__SMOKE_S__", str(PROBE_SMOKE_TIMEOUT_S))
)
_PROBE_BASE = _PROBE_BASE.replace("__FETCH_PATH_DIRS__", FETCH_PATH_DIRS)


def probe_script(
    clones: Sequence[str] = (),
    hook_trees: Sequence[str] = (),
    *,
    codex_local: bool = False,
    pytest_python: str | None = None,
    bus_lane: str | None = None,
    extra_clones: Sequence[str] = (),
    github_cli: bool = True,
    peers: Sequence[str] = (),
) -> str:
    """Capacity, login, parity and hook probes; ``github_cli`` follows the host's declared cutover policy."""
    credential = (
        gh_credential_script()
        if github_cli
        else 'g=0; ghv=missing\necho "PARITY-GH-POLICY absent"'
    )
    script = (
        _PROBE_BASE.replace("__GH_CREDENTIAL__", credential)
        .replace(
            "__PARITY__",
            parity_probe_script(
                clones,
                pytest_python,
                bus_lane=bus_lane,
                extra_clones=extra_clones,
                github_cli=github_cli,
                peers=peers,
            ),
        )
        .replace("__HOOKPARSE__", hook_parse_script(hook_trees))
    )
    script = bind_registry_roots(script).replace(
        "__PREFLIGHT__", PREFLIGHT_SH + _codex_preflight_sh()
    )
    return ("PROBE_CODEX_LOCAL=1\n" + script) if codex_local else script


def host_path_env(
    registry_root: str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Every path-valued variable a lane runs with, derived from the CHOSEN HOST's own registry root (the one its
    resolution step found on that host), never from the launching host's environment or another host's
    (OMN-20110: h105 and h101 journalled into h202's ``/data/omninode``, read-only there). The remote-lane runner and
    the landing worker launch both set this one set (OMN-17427)."""
    state = f"{registry_root}/.onex_state"
    return {
        REGISTRY_ROOT_ENV: registry_root,
        "ONEX_WORKSPACE_CONFIG_ROOT": str(
            Path(registry_root)
            / ".."
            / str(resolve_overlay_key("repos.private_data", environ=_env(env)))
        ),
        "ONEX_STATE_DIR": state,
        "ONEX_HOOK_EMIT_JOURNAL_DIR": f"{state}/hook_emit_journal",
        "ONEX_WORKTREES_ROOT": f"{registry_root}/omni_worktrees",
    }


# OMN-20637: the lab host's own env file. It is READ, never sourced or exported wholesale (the plugin's
# ``hook_edge_lane.read_operator_env_file``, OMN-17204): only the names a caller asks for are taken from it, as data,
# so the file can neither run anything in the lane's shell nor shadow a name the runner owns. The remote-lane runner
# and a landing worker's run step both read it through ``host_env_lines`` (OMN-20884).
HOST_ENV_FILE = "$HOME/.omnibase/.env"
# The parse rules are read_operator_env_file's: blank and ``#`` lines skipped, an optional ``export `` prefix, the
# key alphanumeric with underscores, one pair of matching outer quotes stripped, no expansion. Prints one shell
# export per named key found (the value shell-quoted) and the log line naming the keys found, never a value.
HOST_ENV_PROGRAM = r"""
import shlex, sys
path, wanted = sys.argv[1], sys.argv[2:]
try:
    text = open(path, encoding="utf-8").read()
except (OSError, UnicodeDecodeError):
    print("echo REMOTE-LANE-HOST-ENV-UNREADABLE " + shlex.quote(path))
    raise SystemExit(0)
found = {}
for line in text.splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    if s.startswith("export "):
        s = s[len("export "):].lstrip()
    key, _, value = s.partition("=")
    key, value = key.strip(), value.strip()
    if key not in wanted:
        continue
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    found[key] = value
for key in wanted:
    if key in found:
        print("export %s=%s" % (key, shlex.quote(found[key])))
print("echo REMOTE-LANE-HOST-ENV %s keys=%s" % (shlex.quote(path), ",".join(k for k in wanted if k in found) or "none"))
"""


def host_env_lines(keys: Sequence[str]) -> list[str]:
    """Take ``keys`` from the host's env file (``HOST_ENV_PROGRAM``). A host with no file prints one
    REMOTE-LANE-HOST-ENV-ABSENT line, and a reader that cannot run prints REMOTE-LANE-HOST-ENV-UNREADABLE; the
    script runs either way, without those names."""
    return [
        f'HE="{HOST_ENV_FILE}"',
        f"HE_SRC={shlex.quote(HOST_ENV_PROGRAM)}",
        (
            'if [ ! -r "$HE" ]; then echo "REMOTE-LANE-HOST-ENV-ABSENT $HE"; '
            f'elif HEX=$(python3 -c "$HE_SRC" "$HE" {" ".join(keys)} </dev/null 2>/dev/null); then eval "$HEX"; '
            'else echo "REMOTE-LANE-HOST-ENV-UNREADABLE $HE"; fi; unset HE HE_SRC HEX'
        ),
    ]


# OMN-20884: the public-repo hygiene hook reads the private vocabulary from the file ``OMNI_HYGIENE_VOCAB_PATH`` names and,
# given no ``--lab-vocabulary``, the lab vocabulary beside it. It fails closed (exit 2, THE GATE DID NOT RUN) on either being
# absent, and the repositories that enforce the lab-config class refuse a commit without the second. On a lab host the two
# files live in the private data clone, in different directories, so they are copied into one directory of the lane's own
# context under the names the gate looks for. The two paths inside the clone are the ``hygiene_vocab.vocabulary_rel`` and
# ``hygiene_vocab.lab_vocabulary_rel`` overlay keys. The gate and its vocabularies are not touched.
EXIT_HYGIENE_VOCAB_ABSENT = (
    91  # the run script's own exit: the host's private data clone lacks a vocabulary
)


def hygiene_vocab_lines(env: Mapping[str, str] | None = None) -> list[str]:
    """Export ``OMNI_HYGIENE_VOCAB_PATH`` and ``OMNI_HYGIENE_LAB_VOCAB_PATH`` from the host's private data clone,
    found the way the knowledge-base clone is: the first variable ``repos.private_data_home_envs`` lists that is set (the host
    env file's value is loaded by ``host_env_lines``), else the host's login-profile value, else the registry root's sibling
    directory named by ``repos.private_data``. Never a default path (rule 8). A host with no clone, or a clone missing either
    file, gets one REMOTE-LANE-HYGIENE-VOCAB-ABSENT line naming the file and the script ends with ``EXIT_HYGIENE_VOCAB_ABSENT``
    before any engine starts: a commit made without the vocabulary is refused unscanned. Needs ``C`` (the lane's
    context directory) and, for the sibling directory, the registry root variable."""
    environ = _env(env)
    vocab = shlex.quote(
        str(resolve_overlay_key("hygiene_vocab.vocabulary_rel", environ=environ))
    )
    lab = shlex.quote(
        str(resolve_overlay_key("hygiene_vocab.lab_vocabulary_rel", environ=environ))
    )
    names = list(resolve_overlay_key("repos.private_data_home_envs", environ=environ))
    sibling = str(resolve_overlay_key("repos.private_data", environ=environ))
    home_vars = " ".join(f'"${{{name}:-}}"' for name in names)
    login_vars = " ".join(
        f'"$(bash -lc "printf %s \\"\\${{{name}:-}}\\"" 2>/dev/null </dev/null)"'
        for name in names
    )
    return [
        (
            f'OI=""; for d in {home_vars} {login_vars} '
            f'"${{ONEX_REGISTRY_ROOT:+$ONEX_REGISTRY_ROOT/../{sibling}}}"; do '
            '[ -n "$d" ] && [ -d "$d" ] && { OI=$(cd -P "$d" && pwd -P); break; }; done'
        ),
        f'HV="${{OI:-${{ONEX_REGISTRY_ROOT:-}}/../{sibling}}}/"{vocab}; HL="${{OI:-${{ONEX_REGISTRY_ROOT:-}}/../{sibling}}}/"{lab}',
        (
            'if [ -r "$HV" ] && [ -r "$HL" ]; then HD="$C/hygiene-vocab"; mkdir -p "$HD" && '
            'cp "$HV" "$HD/$(basename "$HV")" && cp "$HL" "$HD/$(basename "$HL")" || exit '
            f"{EXIT_HYGIENE_VOCAB_ABSENT}; "
            'export OMNI_HYGIENE_VOCAB_PATH="$HD/$(basename "$HV")" OMNI_HYGIENE_LAB_VOCAB_PATH="$HD/$(basename "$HL")"; '
            'echo "REMOTE-LANE-HYGIENE-VOCAB $OI"; '
            'else for f in "$HV" "$HL"; do [ -r "$f" ] || echo "REMOTE-LANE-HYGIENE-VOCAB-ABSENT $f is not a readable '
            f"file on $(hostname) (the private data clone: {' or '.join(names)} in the host env file or the login "
            f'profile, $ONEX_REGISTRY_ROOT/../{sibling})"; done; '
            f"unset OI HV HL; exit {EXIT_HYGIENE_VOCAB_ABSENT}; fi; unset OI HV HL HD"
        ),
    ]


def lane_env(
    lane: str,
    parent: str | None = None,
    host: str | None = None,
    run_id: str | None = None,
    plugin_root: str | None = None,
) -> dict[str, str]:
    """The lane's own name, as ``ONEX_LANE`` (OMN-19860). ``onex delegate`` stamps it on every delegation as
    ``caller_lane`` (omnibase_infra ``delegate_caller.py`` reads ``ONEX_LANE`` first), the gh shim and the
    PR-ownership guard resolve the lane from it, so a push or a PR mutation is attributed to this lane. A
    landing worker also names its run (``ONEX_RUN_ID``, which the guard reads before any session id: a Codex
    worker has none) and the runner passes no ``run_id``, so its set is unchanged (OMN-17427).
    ``plugin_root`` is the installed omni plugin's root on the host that runs the lane (the directory holding
    ``skills/`` and ``scripts/``), exported as ``OMNI_PLUGIN_ROOT``: the one handle a brief's commands name the
    plugin's scripts by, in place of a placeholder the lane would have to resolve itself (OMN-17427)."""
    env = {"ONEX_LANE": lane}
    if parent:
        env["ONEX_PARENT_LANE"] = (
            parent  # the orchestrator lane (--parent); the shim writes it as ``parent``
        )
    if host:
        env["ONEX_LANE_HOST"] = (
            host  # the chosen lab host's name; the shim writes it as ``host`` (OMN-20420)
        )
    if run_id:
        env["ONEX_RUN_ID"] = run_id
    if plugin_root:
        env["OMNI_PLUGIN_ROOT"] = plugin_root
    return env


def _with_logins(host: Host, text: str) -> Host:
    """A live row's engines and gh from the probe's LOGIN line; no LOGIN line means no login."""
    if not host.engines_live:
        return host
    claude = gh = False
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 3 and parts[0] == "LOGIN":
            claude, gh = parts[1] == "1", parts[2] == "1"
            break
    return replace(
        host, engines=frozenset(CLAUDE_ENGINES) if claude else frozenset(), gh=gh
    )


def _swap_used_frac(text: str) -> float | None:
    """The probe's SWAP line (``SWAP <total kB> <free kB>``) as the fraction in use; None when there is no
    swap or no line."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 3 and parts[0] == "SWAP":
            try:
                total, free = int(parts[1]), int(parts[2])
            except ValueError:
                return None
            return None if total <= 0 else min(1.0, max(0.0, (total - free) / total))
    return None


def _swap_io(text: str) -> tuple[float, float] | None:
    """The last SWAPIO line's quieter one-second sample as (swap-in, swap-out) KB/s; first wins a tie."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if parts and parts[0] == "SWAPIO":
            if len(parts) != 6 or any(
                re.fullmatch(r"[0-9]+", value) is None for value in parts[1:]
            ):
                return None
            kb, in1, out1, in2, out2 = map(int, parts[1:])
            if kb <= 0:
                return None
            si, so = (in1, out1) if in1 + out1 <= in2 + out2 else (in2, out2)
            return float(si * kb), float(so * kb)
    return None


def _mem_pressure(text: str) -> int | None:
    """The probe's PRESSURE line (macOS ``kern.memorystatus_vm_pressure_level``), or None."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[0] == "PRESSURE" and parts[1].isdigit():
            return int(parts[1])
    return None


def _mem_free(text: str) -> tuple[float, float] | None:
    """The probe's MEMFREE line (macOS free percent, total memory in bytes) as (percent, GB), or None."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if (
            len(parts) == 3
            and parts[0] == "MEMFREE"
            and parts[1].isdigit()
            and parts[2].isdigit()
            and int(parts[2]) > 0
        ):
            return min(100.0, float(parts[1])), int(parts[2]) / 1024**3
    return None


def _probe_number(text: str, key: str) -> float | None:
    """The value of the probe's last ``<key> <number>`` line, or None."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return max(0.0, float(parts[1]))
            except ValueError:
                return None
    return None


def parse_thermal(text: str) -> tuple[float | None, float | None]:
    """The probe's ``THERMAL <cpu C|-> <dimm C|->`` line as (cpu, dimm) degrees C; ``None`` for a sensor the host does
    not have, and (None, None) when the probe printed no such line."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 3 and parts[0] == "THERMAL":
            out: list[float | None] = []
            for raw in parts[1:]:
                try:
                    out.append(None if raw == "-" else float(raw))
                except ValueError:
                    out.append(None)
            return out[0], out[1]
    return None, None


def probe_word(text: str, key: str) -> str | None:
    """The word of the probe's last ``<key> <word>`` line (WORKDIR, CODEXLOGIN, CODEXSANDBOX), or None. CODEXLOGIN is ``1``, ``0``
    or ``unavailable`` (the check timed out or could not run, OMN-20864)."""
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            return parts[1]
    return None


def probe_rest(text: str, key: str) -> str | None:
    """The rest of the probe's last ``<key> <text>`` line, or None."""
    for line in reversed(text.strip().splitlines()):
        head, _, rest = line.partition(" ")
        if head == key and rest.strip():
            return rest.strip()
    return None


parse_link_loss, ping_argv = link_gate.parse_link_loss, link_gate.ping_argv


def parse_probe(host: Host, text: str, placed: int) -> Reading:
    host = _with_logins(host, text)
    busy = _probe_number(text, "BUSY")  # Linux CPU busy cores over a one-second sample
    idle_pct = _probe_number(text, "CPUIDLE")
    if idle_pct is not None:
        idle_pct = min(100.0, idle_pct)
    swap = _swap_used_frac(text)
    psi = _probe_number(text, "MEMPSI")
    mem_free = _mem_free(text)
    swap_io = _swap_io(text)
    cpu_temp, dimm_temp = parse_thermal(text)
    hooks = parse_hook_parse(text)
    for line in reversed(text.strip().splitlines()):
        parts = line.split()
        if (
            len(parts) == 4
            and parts[0] == "PROBE"
            and parts[1].isdigit()
            and parts[3].isdigit()
        ):
            try:
                load1 = float(parts[2])
            except ValueError:
                break
            cores = int(parts[1])
            if busy is None and idle_pct is not None:
                busy = cores * (1 - idle_pct / 100)
            return Reading(
                host=host,
                cores=cores,
                load1=load1,
                mem_avail_gb=int(parts[3]) / (1024 * 1024),
                placed=placed,
                busy_cores=busy,
                cpu_idle_pct=idle_pct,
                swap_used_frac=swap,
                mem_pressure=_mem_pressure(text),
                mem_free_pct=None if mem_free is None else mem_free[0],
                mem_total_gb=None if mem_free is None else mem_free[1],
                mem_psi_avg60=psi,
                swap_in_kb_s=None if swap_io is None else swap_io[0],
                swap_out_kb_s=None if swap_io is None else swap_io[1],
                coauthor=coauthor_state(text),
                lan_grant=probe_word(text, "LANGRANT"),  # a Mac only
                cpu_temp_c=cpu_temp,
                dimm_temp_c=dimm_temp,
                workdir=probe_word(text, "WORKDIR"),
                codex_login=None
                if (login := probe_word(text, "CODEXLOGIN")) in (None, "unavailable")
                else login == "1",
                codex_unavailable=login == "unavailable",
                codex_sandbox=probe_word(text, "CODEXSANDBOX"),
                codex_sandbox_why=probe_rest(text, "CODEXSANDBOXWHY"),
                claude_login_why=probe_word(text, "CLAUDEWHY"),
                claude_pool=next(
                    (
                        ln[len("CLAUDEPOOL ") :]
                        for ln in reversed(text.splitlines())
                        if ln.startswith("CLAUDEPOOL ")
                    ),
                    None,
                ),
                parity=parse_parity(text),
                hook_parse=None if hooks is None else hooks[0],
                hook_parse_detail=None if hooks is None else hooks[1],
            )
    return Reading(host=host, placed=placed, error=f"unparseable probe: {text[:80]!r}")


def parse_parity(
    text: str, reference_omnimarket: str | None = None
) -> HostParity | None:
    """Typed PARITY record or None; compare the venv against ``reference_omnimarket``."""
    clones: list[CloneParity] = []
    core_worktrees: list[tuple[str, str]] = []
    plugins: list[PluginParity] = []
    venv: str | None = None
    fseventsd_rss_kib: int | None = None
    gh = pytest = None
    gh_absent = False
    pytest_python = PARITY_DEFAULT_PYTHON
    gh_path: str | None = None
    bus: BusHost | None = None
    hook_tools: HookTools | None = None
    precommit_cache: PrecommitCache | None = None
    ages: list[PluginAge] = []
    errors: list[str] = []
    seen = False
    for line in text.splitlines():
        if not line.startswith("PARITY-"):
            continue
        seen = True
        key, _, rest = line.partition(" ")
        parts = rest.split()
        if key == "PARITY-CLONE" and len(parts) == 5:
            name, state, head, tip, hook = parts
            present = state == "present"
            clones.append(
                CloneParity(
                    name,
                    present,
                    None if head == "-" else head,
                    None if tip == "-" else tip,
                    None if not present else hook == "1",
                )
            )
        elif key == "PARITY-CORE-WORKTREE" and len(parts) == 2:
            core_worktrees.append((parts[0], parts[1]))
        elif key == "PARITY-VENV" and parts:
            venv = None if parts[0] == "none" else parts[0]
        elif key == "PARITY-FSEVENTSD-RSS-KIB" and len(parts) == 1:
            fseventsd_rss_kib = int(parts[0]) if parts[0].isdigit() else None
        elif key == "PARITY-GH" and parts:
            gh = parts[0] == "1"
        elif key == "PARITY-GH-POLICY" and parts == ["absent"]:
            gh_absent = True
        elif key == "PARITY-PYTEST" and parts:
            pytest = parts[0] == "1"
            pytest_python = parts[1] if len(parts) > 1 else PARITY_DEFAULT_PYTHON
        elif key == "PARITY-GHPATH" and parts:
            gh_path = rest.strip()
        elif key == "PARITY-BUS":
            fields = rest.split(None, 3)
            if len(fields) == 4 and fields[2] in ("ok", "unresolved", "unknown"):
                bus = BusHost(
                    fields[0],
                    "" if fields[1] == "-" else fields[1],
                    fields[2],
                    fields[3],
                )
        elif key == "PARITY-HOOKTOOLS" and len(parts) == 2:
            hook_tools = HookTools(
                None if parts[0] == "none" else parts[0],
                None if parts[1] == "none" else parts[1],
            )
        elif key == "PARITY-PCCACHE" and parts and parts[0] in ("ok", "bad", "none"):
            precommit_cache = PrecommitCache(
                parts[0], rest.split(None, 1)[1] if len(parts) > 1 else ""
            )
        elif key == "PARITY-PLUGIN-AGE" and len(parts) == 6:
            name, revision, tip, behind, tree, age = parts
            ages.append(
                PluginAge(
                    name,
                    revision,
                    tip,
                    int(behind) if behind.isdigit() else None,
                    None if tree == "-" else tree == "equal",
                    int(age) if age.isdigit() else None,
                )
            )
        elif key == "PARITY-PLUGIN":
            fields = rest.split(None, 5)
            if len(fields) == 6:
                name, state, installed, shipped, revision, detail = fields
                plugins.append(
                    PluginParity(
                        name,
                        state,
                        "" if installed == "-" else installed,
                        "" if shipped == "-" else shipped,
                        "" if revision == "-" else revision,
                        detail,
                    )
                )
        elif key == "PARITY-ERROR":
            errors.append(rest.strip() or "the parity probe failed")
    if not seen:
        return None
    return HostParity(
        clones=tuple(clones),
        venv_omnimarket=venv,
        fseventsd_rss_kib=fseventsd_rss_kib,
        reference_omnimarket=reference_omnimarket,
        gh=gh,
        pytest=pytest,
        pytest_python=pytest_python,
        plugins=tuple(plugins),
        error="; ".join(errors) or None,
        gh_credential=parse_gh_credential(text),
        gh_absent=gh_absent,
        bus=bus,
        gh_path=None if gh_path in (None, "none") else gh_path,
        gh_path_ok=None if gh_path is None else gh_path != "none",
        hook_tools=hook_tools,
        precommit_cache=precommit_cache,
        plugin_ages=tuple(ages),
        core_worktrees=tuple(core_worktrees),
        git_identity=_clone_parity and _clone_parity.parse_git_identity(text),
        capabilities=host_capability.parse_capability_lines(text),
    )


def dispatch_venv_floor(path: Path | None = None) -> str | None:
    """The plugin's omnimarket floor for a dispatch venv (``DISPATCH_VENV_FLOOR_FILE``), or None when the file is missing or
    names no full commit, so the venv is judged against the launching host's as before."""
    try:
        value = json.loads(
            (path or DISPATCH_VENV_FLOOR_FILE).read_text(encoding="utf-8")
        ).get("omnimarket")
    except (OSError, ValueError, AttributeError):
        return None
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
        else None
    )


def omnimarket_clone(env: Mapping[str, str] | None = None) -> Path | None:
    """The launching host's omnimarket registry clone, whose history judges a venv's revision against the floor; None when
    the registry root variable is unset or holds no such clone."""
    home = _env(env).get(REGISTRY_ROOT_ENV)
    clone = Path(home) / "omnimarket" if home else None
    return clone if clone is not None and (clone / ".git").exists() else None


def venv_floor_verdict(
    venv: str | None, floor: str | None, clone: Path | None
) -> bool | None:
    """Whether a dispatch venv's omnimarket revision ``venv`` is at or after ``floor`` and on omnimarket's merged history
    (reachable from ``origin/dev`` or ``origin/main`` of ``clone``). None when it cannot be judged: no venv revision, no floor,
    no clone, or a commit the clone does not hold (a stale fetch), so the caller keeps the launching-host comparison."""
    if not venv or not floor or clone is None:
        return None

    def git_rc(*args: str) -> int | None:
        try:
            return subprocess.run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                timeout=VENV_FLOOR_GIT_TIMEOUT_S,
                check=False,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            return None

    if any(git_rc("cat-file", "-e", f"{c}^{{commit}}") != 0 for c in (venv, floor)):
        return None
    above = git_rc("merge-base", "--is-ancestor", floor, venv)
    if above != 0:
        return False if above == 1 else None
    tips = [
        ref
        for ref in ("refs/remotes/origin/dev", "refs/remotes/origin/main")
        if git_rc("rev-parse", "--verify", "-q", f"{ref}^{{commit}}") == 0
    ]
    merged = [git_rc("merge-base", "--is-ancestor", venv, ref) for ref in tips]
    if 0 in merged:
        return True
    return False if merged and all(rc == 1 for rc in merged) else None


def record_parity(
    directory: Path,
    readings: Sequence[Reading],
    *,
    now: datetime | None = None,
    append_friction: Callable[[str], tuple[bool, str]] | None = None,
) -> None:
    """Keep the latest parity record of each host read, with its read time, in ``<directory>/parity.json`` for queue-status. A
    host read again replaces its record, so a repaired host stops being reported; a host not read keeps the record it had. The
    failures recorded are those against everything a unit may need (``PARITY_EVERYTHING``). A host admitted on its last good
    gh verdict gets one FRICTION row per episode (``note_gh_fallbacks``, OMN-21016)."""
    note_gh_fallbacks(directory, readings, now=now, append=append_friction)
    stamp = _utc_stamp(now or datetime.now(UTC))
    path = parity_path(directory)
    try:
        hosts = json.loads(path.read_text(encoding="utf-8")).get("hosts", {})
    except (OSError, ValueError, AttributeError):
        hosts = {}
    changed = False
    for r in readings:
        if r.error is not None or r.parity is None:
            continue
        hosts[r.host.name] = {
            "host": r.host.name,
            "read_at": stamp,
            **r.parity.record(PARITY_EVERYTHING),
        }
        changed = True
    if not changed:
        return
    directory.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{PARITY_FILE}.{os.getpid()}")
    tmp.write_text(json.dumps({"hosts": hosts}, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def ledger_append_row(row: str) -> tuple[bool, str]:
    """No ledger writer is bound here: a caller injects one as ``append`` (the ledger writer is the caller's node, never a
    script this module runs). Never raises."""
    return False, "no ledger writer is bound: pass append= to note_gh_fallbacks"


def note_gh_fallbacks(
    directory: Path,
    readings: Sequence[Reading],
    *,
    now: datetime | None = None,
    append: Callable[[str], tuple[bool, str]] | None = None,
) -> list[dict[str, Any]]:
    """One FRICTION row for each host a placement admitted on its last good gh verdict because GitHub gave no answer
    (OMN-21016): written once per episode, which ends when the host is read with any other verdict source, and a refused append
    is retried no sooner than ``GH_FRICTION_RETRY_S`` later. The episodes live in ``<directory>/GH_FALLBACK_FILE``. Returns what
    was done, one entry per row attempted; never raises."""
    when = now or datetime.now(UTC)
    path = directory / GH_FALLBACK_FILE
    try:
        episodes = json.loads(path.read_text(encoding="utf-8"))
        episodes = episodes if isinstance(episodes, dict) else {}
    except (OSError, ValueError):
        episodes = {}
    before = json.dumps(episodes, sort_keys=True)
    out: list[dict[str, Any]] = []
    for r in readings:
        cred = (
            None if r.error is not None or r.parity is None else r.parity.gh_credential
        )
        if cred is None:
            continue
        name = r.host.name
        if cred.source != "fallback":
            episodes.pop(name, None)
            continue
        ep = episodes.setdefault(name, {"since": _utc_stamp(when), "friction": False})
        if ep.get("friction"):
            continue
        try:
            tried = datetime.strptime(
                str(ep.get("attempted_at")), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            tried = None
        if tried is not None and (when - tried).total_seconds() < GH_FRICTION_RETRY_S:
            continue
        age = (
            "unknown"
            if cred.last_ok_age_s is None
            else f"{cred.last_ok_age_s // 60} min"
        )
        text = (
            f"lab admission kept {name} on its last good gh credential verdict ({age} old) because the gh credential "
            f"check got no answer from GitHub ({cred.detail}); a logged-out answer still refuses the host, and it refuses "
            f"as GITHUB-UNREACHABLE once that verdict is older than {GH_FALLBACK_MIN // 60}h"
        )
        stamp = _utc_stamp(when)
        row = " | ".join(
            [
                stamp,
                "FRICTION",
                "lane=landing-placement",
                f"ticket={GH_FRICTION_TICKET}",
                "actor=script:landing-placement",
                "model=none",
                "class=gh-credential-fallback",
                f"host={name}",
                "cost=~5 lane-minutes for a reader to confirm whether GitHub or the credential failed",
                " ".join(text.replace("|", "/").split()),
            ]
        )
        try:
            ok, err = (append or ledger_append_row)(row)
        except Exception as exc:  # the placement must never fail on its friction note
            logging.getLogger(__name__).exception("Lab worker operation failed")
            ok, err = False, f"{type(exc).__name__}: {exc}"[:300]
        ep.update(friction=ok, attempted_at=stamp)
        out.append(
            {
                "host": name,
                "outcome": "written" if ok else "failed",
                "detail": "" if ok else err,
            }
        )
    if json.dumps(episodes, sort_keys=True) != before:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{GH_FALLBACK_FILE}.{os.getpid()}")
            tmp.write_text(json.dumps(episodes, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    return out


def parity_path(directory: Path | None = None) -> Path:
    # The one file the parity records live in: ``record_parity`` writes it and ``recorded_parity`` (queue-status) reads it.
    return (directory if directory is not None else placement_dir()) / PARITY_FILE


def recorded_parity(directory: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    """The latest parity record of each host that has one, by host name. Empty when none was ever recorded."""
    # An unreadable or malformed file is empty too unless ``strict``, which raises ValueError naming the file: queue-status
    # read such a file as "no record" (OMN-17427).
    path = parity_path(directory)
    try:
        hosts = json.loads(path.read_text(encoding="utf-8")).get("hosts", {})
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError) as exc:
        if strict:
            raise ValueError(f"{path}: {exc}") from exc
        return []
    return [hosts[name] for name in sorted(hosts) if isinstance(hosts[name], dict)]


def ssh_argv(host: Host, lane: str, script: str) -> list[str]:
    # OMN-17427: the destination is the node's, so every ssh of one read goes where its probe was answered.
    return ["ssh", *SSH_OPTS, recovery.resolve_target(host), f"# lane={lane}\n{script}"]


# What a login shell over ssh would have given a script on a lab host, for the launching host: nothing of the
# calling session's environment (no CLAUDECODE markers, no ANTHROPIC_* or GH_TOKEN values, no ONEX_* overrides).
LOCAL_EXEC_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"


def exec_argv(
    host: Host, lane: str, script: str, *, env: Mapping[str, str] | None = None
) -> list[str]:
    """The argv that runs ``script`` on ``host``: ``ssh_argv`` for a lab host, and for the launching host (OMN-17427)
    the same script under ``bash -c`` in a scrubbed environment (HOME, USER, LOGNAME, a fixed PATH), so a lane there
    sees what a lane over ssh sees and not the placing session's variables."""
    if not host.local:
        return ssh_argv(host, lane, script)
    env = _env(env)
    home = env.get("HOME") or str(Path.home())
    user = env.get("USER") or env.get("LOGNAME") or Path(home).name
    return [
        "/usr/bin/env",
        "-i",
        f"HOME={home}",
        f"USER={user}",
        f"LOGNAME={user}",
        f"PATH={LOCAL_EXEC_PATH}",
        "/bin/bash",
        "-c",
        f"# lane={lane}\n{script}",
    ]


_capture = recovery.probe_capture


def read_pool(
    hosts: Sequence[Host],
    *,
    placed: Mapping[str, int] | None = None,
    fresh: Mapping[str, int] | None = None,
    capture: Capture = _capture,
    lane: str = "landing-controller",
    limited: Mapping[str, str] | None = None,
    auth_expired: Mapping[str, str] | None = None,
    codex_limited: Mapping[str, str] | None = None,
    codex_sandbox_failed: Mapping[str, str] | None = None,
    hook_trees: Sequence[str] = (),
    need_clones: Sequence[str] = (),
    link_history: Path | None = None,
    now: float | None = None,
    readings_dir: Path | None = None,
    bound: Callable[
        [float], float
    ] = float,  # OMN-19815: caps each probe's and ping's timeout (the throughput tick's budget); float is no cap
) -> list[Reading]:
    """Probe every host in parallel; the local host runs the same probe without ssh, and every other host is also
    pinged (``LINK_PING_COUNT`` pings) through the same ``capture``. A host in ``limited`` (``limited_hosts``) is read
    and reported but holds no engine, so it takes no worker; so is a host in ``auth_expired``
    (``auth_expired_hosts``), whose Claude login expired. A host in ``codex_limited`` (``codex_limited_hosts``) or
    ``codex_sandbox_failed`` (``codex_sandbox_failed_hosts``) keeps its Claude engines and is refused for the Codex engine only
    (``Reading.codex_refusal``); a sandbox failure a worker observed outranks the probe's cached smoke (OMN-20531).
    ``hook_trees`` names plugin trees under each host's ``$HOME`` the probe parse-checks beside the installed plugins (OMN-20961).
    ``need_clones`` names registry clones the probe reads beyond each host's row (the plugin repository's and
    the knowledge base's always, OMN-21470: lab clones of it were 8 to 10 days old); a unit that needs one says so in ``ParityNeeds.clones``."""
    if not hosts:
        return []
    extra = tuple(
        dict.fromkeys(
            (
                parity_plugin_repo(),
                str(resolve_overlay_key("repos.knowledge_base")),
                *need_clones,
            )
        )
    )
    placed, fresh, now = (
        placed or {},
        fresh or {},
        datetime.now(UTC).timestamp() if now is None else now,
    )
    asked = recovery.ModelPoolReadingRequest(
        None if readings_dir is None else str(readings_dir),
        "",
        subprocess.CompletedProcess([], 0),
        now,
        os.environ.get(PRIORITY_CONTRACT_ENV) or str(PRIORITY_CONTRACT_FILE),
    )  # OMN-17427: a host's request is this with its name and target

    def loss(host: Host) -> float | None:
        return parse_link_loss(
            capture(
                ping_argv(recovery.resolve_target(host)),
                timeout=bound(link_gate.LINK_PING_TIMEOUT_S),
            ).stdout
        )

    def one(host: Host) -> Reading:
        argv = (
            [
                "/bin/sh",
                "-c",
                probe_script(
                    host.parity_clones,
                    hook_trees,
                    codex_local=host.codex,
                    pytest_python=host.parity_python,
                    bus_lane=host.ledger_bus_lane,
                    extra_clones=extra,
                    github_cli=not host.gh_absent,
                ),
            ]
            if host.local
            else ssh_argv(
                host,
                f"{lane} placement-probe",
                probe_script(
                    host.parity_clones,
                    hook_trees,
                    pytest_python=host.parity_python,
                    bus_lane=host.ledger_bus_lane,
                    extra_clones=extra,
                    github_cli=not host.gh_absent,
                    peers=[h.target for h in hosts if h.target and h.name != host.name],
                ),
            )
        )
        # OMN-21809: a probe that ends on its timeout under concurrent readers answers with the host's last good text while it is under the
        # placement contract's pool_reading_max_age_s (the timeout itself is not raised); the Reading carries its age.
        done = (
            kept := recovery.pool_probe(
                replace(
                    asked,
                    host=host.name,
                    target=host.target if host.local else recovery.resolve_target(host),
                ),
                lambda: capture(
                    argv,
                    timeout=bound(
                        LOCAL_PROBE_TIMEOUT_S if host.local else PROBE_TIMEOUT_S
                    ),
                ),
            )
        ).done
        count = int(placed.get(host.name, 0))
        if done.returncode != 0:
            return Reading(
                host=host,
                placed=count,
                error=f"rc={done.returncode}{recovery.target_note(host)} {(done.stderr or done.stdout or '').strip()[:100]}",
                launcher_gap=kept.gap,
            )
        return replace(
            parse_probe(host, done.stdout, count),
            fresh=min(count, int(fresh.get(host.name, 0))),
            cached_age_s=kept.cached_age_s,
        )

    if not hosts:
        return []
    # ``link_history`` names the file the reads are kept in (``node_link_history_effect``): with it a host is benched on sustained loss
    # (two consecutive reads over the bar); without it each read stands alone and one read never benches.
    # OMN-17427: each remote row's destination is decided here, once, with this read's own capture, before any ping or probe runs; the
    # kept launcher-side verdict is keyed by the destination this read uses, so a row that falls back does not answer from the verdict
    # kept against its committed target.
    remote = [
        h
        for h in hosts
        if not h.local
        and recovery.launcher_gap(
            replace(
                asked,
                host=h.name,
                target=recovery.resolve_target(h, capture=capture, bound=bound),
            )
        )
        is None
    ]  # a kept verdict is not pinged either
    with ThreadPoolExecutor(max_workers=len(hosts) + len(remote)) as pool:
        pinged = {
            h.name: pool.submit(loss, h) for h in remote
        }  # the pings run beside the probes, never before them
        probed = list(pool.map(one, hosts))
    losses = {name: fut.result() for name, fut in pinged.items()}
    priors = (
        NodeLinkHistoryEffect()
        .handle(ModelLinkHistoryRequest(path=str(link_history), losses=losses, now=now))
        .priors
        if link_history
        else {}
    )
    readings = [
        replace(
            r,
            link_loss_pct=losses.get(r.host.name),
            link_loss_prior=priors.get(r.host.name, ()),
        )
        for r in probed
    ]
    # Each host's dispatch venv is judged against the plugin's floor (OMN-20864), and where that cannot be judged against the
    # launching host's, read by the same pass (OMN-20266). The launching host's plugin is the session's own.
    reference = next(
        (
            r.parity.venv_omnimarket
            for r in readings
            if r.host.local and r.parity is not None
        ),
        None,
    )
    floor = dispatch_venv_floor()
    clone = omnimarket_clone()
    verdicts: dict[str | None, bool | None] = {}
    for r in readings:
        if r.parity is not None and r.parity.venv_omnimarket not in verdicts:
            verdicts[r.parity.venv_omnimarket] = venv_floor_verdict(
                r.parity.venv_omnimarket, floor, clone
            )
    readings = [
        r
        if r.parity is None
        else replace(
            r,
            parity=replace(
                r.parity,
                reference_omnimarket=reference,
                own_host=r.host.local,
                venv_floor=floor,
                venv_floor_ok=verdicts.get(r.parity.venv_omnimarket),
                optional_clones=frozenset(extra) - frozenset(r.host.parity_clones),
            ),
        )
        for r in readings
    ]
    limited, auth_expired, codex_limited, codex_sandbox_failed = (
        limited or {},
        auth_expired or {},
        codex_limited or {},
        codex_sandbox_failed or {},
    )
    out_of_claude = {*limited, *auth_expired}
    marked: list[Reading] = []
    for r in readings:
        name = r.host.name
        if name in out_of_claude:
            r = replace(r, host=replace(r.host, engines=frozenset()))
        if (
            name in limited
            or name in auth_expired
            or name in codex_limited
            or name in codex_sandbox_failed
        ):
            r = replace(
                r,
                claude_limited_until=limited.get(name),
                claude_auth_expired_at=auth_expired.get(name),
                codex_limited_until=codex_limited.get(name),
                codex_sandbox="fail"
                if name in codex_sandbox_failed
                else r.codex_sandbox,
            )
        marked.append(r)
    return marked


def pool_slots(readings: Sequence[Reading]) -> int:
    """Further workers the whole pool can take now (any engine)."""
    return sum(r.slots for r in readings if r.host.engines)


# Lane priority classes (OMN-20864, operator RULING 2026-10-15T08:14:02Z: "Let's try to make sure merges for mvp and process
# fixes get priority on lanes"; OMN-20074, RULING 2026-10-15T19:56:37Z: M4 work first, OCC retirement second, then everything
# else). Three dispatchers place on this pool (the landing controller's fix workers, lab-fill and the orchestrator's remote
# lanes), first come, first served, so whichever fired first took the last free slot. Each unit declares a class; when the pool
# is short, the last free slots are held for the higher classes: a class at rank i leaves up to i free slots to the classes above
# it, one for each of them with a unit waiting (OMN-20074; none for a class nobody of which is queued). A class held back waits for its next placement; nothing running is displaced. The order, the repositories and the tickets of
# each class are DATA, read from merge-drain/scripts/lane_priority_classes.json (the placement contract), never a list in this module.
PRIORITY_OTHER = "other"
PRIORITY_CONTRACT_FILE = (
    Path(__file__).resolve().parent.parent / "lane_priority_classes.json"
)
PRIORITY_CONTRACT_ENV = "ONEX_LANE_PRIORITY_CLASSES"
# The env the lab-fill dispatch command sets for the remote-lane runner.
PRIORITY_CLASS_ENV = "ONEX_LANE_PRIORITY_CLASS"
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]*-[0-9]+\b")


# OMN-20074: the names a merge rank may take. Each is a test the controller knows how to make (``merge_rank_of``); the
# order they rank in is the contract's ``merge_ranks``, never this tuple's.
MERGE_RANK_NAMES = ("cause-fix", "process-fix", "m4", "pin-bump", PRIORITY_OTHER)


@dataclass(frozen=True)
class PriorityContract:
    """The lane priority contract: ``classes`` highest first (``other`` last), the repositories and the tickets of each class."""

    classes: tuple[str, ...]
    repos: Mapping[str, frozenset[str]]
    tickets: Mapping[str, frozenset[str]]
    # OMN-20074: the PR ranks the landing controller follows, highest first (``other`` last), and the pin-bump titles
    merge_ranks: tuple[str, ...] = ()
    pin_bump_titles: tuple[re.Pattern[str], ...] = ()
    # A class whose repositories are named by role: each role is a ``repos`` overlay key (``repos.<role>``), resolved when read.
    repo_roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def holdback(self) -> dict[str, int]:
        """Free pool slots each class leaves for the classes above it: its rank."""
        return {cls: rank for rank, cls in enumerate(self.classes)}


def load_priority_contract(path: Path | str | None = None) -> PriorityContract:
    """Read the declared priority contract; unreadable or malformed content raises ``ValueError``."""
    where = Path(
        path or os.environ.get(PRIORITY_CONTRACT_ENV) or PRIORITY_CONTRACT_FILE
    )
    try:
        doc = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"lane priority contract unreadable: {where}: {exc}") from exc
    classes = doc.get("classes") if isinstance(doc, dict) else None
    if (
        not isinstance(classes, list)
        or not classes
        or not all(isinstance(c, str) and c for c in classes)
        or len(set(classes)) != len(classes)
        or classes[-1] != PRIORITY_OTHER
    ):
        raise ValueError(
            f"lane priority contract malformed: {where}: classes must be unique names, {PRIORITY_OTHER} last"
        )

    def members(key: str, norm: Callable[[str], str]) -> dict[str, frozenset[str]]:
        section = doc.get(key) or {}
        if not isinstance(section, dict):
            raise ValueError(
                f"lane priority contract malformed: {where}: {key} must map a class to a list"
            )
        out: dict[str, frozenset[str]] = {}
        for cls, names in section.items():
            if (
                cls not in classes
                or not isinstance(names, list)
                or not all(isinstance(n, str) and n for n in names)
            ):
                raise ValueError(
                    f"lane priority contract malformed: {where}: {key}.{cls} must list names of a class in classes"
                )
            out[cls] = frozenset(norm(n.strip()) for n in names)
        return out

    # a contract written before OMN-20074 ranks no PR: every PR is other, and the node's order stands
    ranks = doc.get("merge_ranks", [PRIORITY_OTHER])
    if (
        not isinstance(ranks, list)
        or not ranks
        or not all(isinstance(r, str) and r in MERGE_RANK_NAMES for r in ranks)
        or len(set(ranks)) != len(ranks)
        or ranks[-1] != PRIORITY_OTHER
    ):
        raise ValueError(
            f"lane priority contract malformed: {where}: merge_ranks must be unique names of "
            f"{', '.join(MERGE_RANK_NAMES)}, {PRIORITY_OTHER} last"
        )
    patterns = doc.get("pin_bump_titles") or []
    try:
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p for p in patterns
        ):
            raise TypeError("a list of patterns")
        pins = tuple(re.compile(p) for p in patterns)
    except (TypeError, re.error) as exc:
        raise ValueError(
            f"lane priority contract malformed: {where}: pin_bump_titles: {exc}"
        ) from exc
    roles = doc.get("repo_roles") or {}
    if not isinstance(roles, dict) or not all(
        c in classes
        and isinstance(v, list)
        and all(isinstance(r, str) and r for r in v)
        for c, v in roles.items()
    ):
        raise ValueError(
            f"lane priority contract malformed: {where}: repo_roles must map a class in classes to a list of repos overlay roles"
        )
    return PriorityContract(
        tuple(classes),
        members("repos", str.lower),
        members("tickets", str.upper),
        tuple(ranks),
        pins,
        {c: tuple(v) for c, v in roles.items()},
    )


_CONTRACT = load_priority_contract()
PRIORITY_CLASSES = _CONTRACT.classes
# Free pool slots each class leaves for the classes above it.
PRIORITY_HOLDBACK = _CONTRACT.holdback


def ticket_of_title(title: str | None) -> str | None:
    """The first ticket id a PR title cites (``fix(OMN-20074): ...``), else None."""
    match = TICKET_RE.search(str(title or ""))
    return match.group(0) if match else None


def _role_repos(contract: PriorityContract, cls: str) -> frozenset[str]:
    """The lowercase directory names of the repositories a class names by role (the ``repos.<role>`` overlay keys)."""
    return frozenset(
        str(resolve_overlay_key(f"repos.{role}")).lower()
        for role in contract.repo_roles.get(cls, ())
    )


def priority_class_of_repo(repo: str | None) -> str:
    """The class of a unit that lands a PR in ``repo`` (``owner/repo``, ``repo`` or ``repo#n``): the class the contract lists
    the repository under (mvp-landing for an MVP repository, process-fix for a process repository), else other."""
    name = str(repo or "").split("#", 1)[0].rsplit("/", 1)[-1].strip().lower()
    return next(
        (
            cls
            for cls in PRIORITY_CLASSES
            if name in _CONTRACT.repos.get(cls, ())
            or name in _role_repos(_CONTRACT, cls)
        ),
        PRIORITY_OTHER,
    )


def priority_class_of_ticket(ticket: str | None, parent: str | None = None) -> str:
    """The class of a unit whose work is ticket ``ticket`` (OMN-20074), or a child of ``parent``: the class the contract lists the
    ticket, else its parent, under, else other."""
    for candidate in (ticket, parent):
        key = str(candidate or "").strip().upper()
        found = next(
            (
                cls
                for cls in PRIORITY_CLASSES
                if key and key in _CONTRACT.tickets.get(cls, ())
            ),
            None,
        )
        if found:
            return found
    return PRIORITY_OTHER


def priority_class_of(
    repo: str | None, ticket: str | None = None, parent: str | None = None
) -> str:
    """The class of a unit working ``ticket`` in ``repo``: a ticket the contract names outranks the repository (an OCC
    retirement PR in an M4 repository is OCC retirement work, not M4 work: M4 first means M4's own tickets), else the repository's."""
    by_ticket = priority_class_of_ticket(ticket, parent)
    return by_ticket if by_ticket != PRIORITY_OTHER else priority_class_of_repo(repo)


def merge_rank_of(
    short: str,
    *,
    title: str | None,
    fixes: Iterable[str],
    m4: Iterable[str],
    contract: PriorityContract | None = None,
) -> tuple[int, str]:
    """The merge rank of open PR ``short`` (``repo#n``), 1 highest, and its name: the first of the contract's
    ``merge_ranks`` the PR meets (OMN-20074). cause-fix: a declared cause fix (``fixes``); process-fix: a PR in a
    process-fix repository or of a process-fix ticket; m4: an M4 delegation PR (``m4``); pin-bump: a title a
    ``pin_bump_titles`` pattern matches; other: everything else."""
    met = merge_ranks_met(short, title=title, fixes=fixes, m4=m4, contract=contract)
    return met[0], (contract or _CONTRACT).merge_ranks[met[0] - 1]


def merge_ranks_met(
    short: str,
    *,
    title: str | None,
    fixes: Iterable[str],
    m4: Iterable[str],
    contract: PriorityContract | None = None,
) -> tuple[int, ...]:
    """Every merge rank ``short`` meets, highest first, ending at other's: the sort key the controller orders by
    (OMN-20074). The first is the PR's rank; the rest break a tie inside it, so in a process-fix repository, where
    every PR is a process fix, an M4 PR still goes ahead of its peers."""
    c = contract or _CONTRACT
    repo = short.partition("#")[0].rsplit("/", 1)[-1].strip().lower()
    ticket = str(ticket_of_title(title) or "").upper()
    tests: dict[str, Callable[[], bool]] = {
        "cause-fix": lambda: short in set(fixes),
        "process-fix": lambda: (
            repo in c.repos.get("process-fix", ())
            or ticket in c.tickets.get("process-fix", ())
        ),
        "m4": lambda: short in set(m4),
        "pin-bump": lambda: any(p.search(str(title or "")) for p in c.pin_bump_titles),
        PRIORITY_OTHER: lambda: True,
    }
    return tuple(
        rank for rank, name in enumerate(c.merge_ranks, start=1) if tests[name]()
    )


def merge_priority_list(
    titles: Mapping[str, str],
    *,
    fixes: Iterable[str],
    m4: Iterable[str],
    contract: PriorityContract | None = None,
) -> dict[str, dict[str, Any]]:
    """The ranked PR list per repository (OMN-20074): ``repo -> {ranked: [{pr, rank, class}], other: n}``, the PRs of
    a named rank in rank order (then by number), and the count of the rest, from ``titles`` (``repo#n -> title``)."""
    c = contract or _CONTRACT
    fixes, m4 = frozenset(fixes), frozenset(m4)
    out: dict[str, dict[str, Any]] = {}
    for short, title in titles.items():
        repo, _, n = short.partition("#")
        entry = out.setdefault(repo, {"ranked": [], "other": 0})
        rank, name = merge_rank_of(short, title=title, fixes=fixes, m4=m4, contract=c)
        if name == PRIORITY_OTHER:
            entry["other"] += 1
        else:
            entry["ranked"].append(
                {"pr": int(n) if n.isdigit() else n, "rank": rank, "class": name}
            )
    for entry in out.values():
        entry["ranked"].sort(key=lambda e: (e["rank"], str(e["pr"]).zfill(9)))
    return dict(sorted(out.items()))


def pool_lane_slots(
    readings: Sequence[Reading], *, codex_allowed: frozenset[str] | None = None
) -> int:
    """Further lanes the pool can take now: the Claude-engine hosts, or with ``codex_allowed`` the hosts that run Codex now (a Claude-limited host holds no engine but runs Codex, OMN-17427)."""
    return sum(
        r.lane_slots
        for r in readings
        if (
            r.host.engines
            if codex_allowed is None
            else r.codex_refusal(codex_allowed, lane=True) is None
        )
    )


# OMN-20074 (throughput tick 2026-10-16T08:33Z): the holdback held each class's whole share whether or not anybody of a higher
# class was queued, so five lab-fill units ended no-host beside idle slots on h200, h201 and h101. A slot is now held for a
# higher class only while a unit of that class waits: a unit no placement took (held back, or no host with a free slot) records
# itself here and counts as waiting for PRIORITY_WAITING_WINDOW_S, or until it is placed. A landing worker probes again every
# minute and refreshes its mark; a remote lane refused once stays waiting for the window. A dotfile, so placed_counts leaves it.
PRIORITY_WAITING_PREFIX = ".priority-waiting-"
PRIORITY_WAITING_WINDOW_S = 900.0


def _waiting_marker(directory: Path, unit: str) -> Path:
    return (
        directory / f"{PRIORITY_WAITING_PREFIX}{re.sub(r'[^A-Za-z0-9_.-]', '_', unit)}"
    )


def note_priority_waiting(
    directory: Path, priority: str, unit: str, *, now: datetime | None = None
) -> None:
    """Record that ``unit`` (``lane-<lane>``, ``lease-<id>``) of class ``priority`` waits for a slot."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = _waiting_marker(directory, unit)
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "class": priority,
                "unit": unit,
                "at": _utc_stamp(now or datetime.now(UTC)),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, marker)


def clear_priority_waiting(directory: Path, unit: str) -> None:
    """``unit`` was placed: it waits no more."""
    _waiting_marker(directory, unit).unlink(missing_ok=True)


def waiting_priority_classes(
    directory: Path,
    *,
    now: datetime | None = None,
    window_s: float = PRIORITY_WAITING_WINDOW_S,
) -> frozenset[str]:
    """The classes with a unit waiting: a mark under ``window_s`` old. An expired or unreadable mark is removed."""
    if not directory.is_dir():
        return frozenset()
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=window_s)
    out: set[str] = set()
    for marker in directory.glob(f"{PRIORITY_WAITING_PREFIX}*"):
        if marker.name.endswith(".tmp"):
            continue
        try:
            doc = json.loads(marker.read_text(encoding="utf-8"))
            cls, at = str(doc.get("class") or ""), _marker_time(doc.get("at"))
        except (OSError, ValueError, AttributeError):
            cls, at = "", None
        if cls and at is not None and at >= cutoff:
            out.add(cls)
        else:
            marker.unlink(missing_ok=True)
    return frozenset(out)


def priority_holdback_refusal(
    priority: str, free_slots: int, waiting: Collection[str]
) -> str | None:
    """Why a unit of class ``priority`` takes none of the pool's ``free_slots``, else None. A class leaves one free slot to
    each class above it that has a unit ``waiting`` (``waiting_priority_classes``), and none to a class nobody of which is
    queued (OMN-20074). With no free slot every class is refused by the hosts themselves, so the holdback refuses only while
    0 < free_slots <= its share. An unknown class is judged as other."""
    cls = priority if priority in PRIORITY_HOLDBACK else PRIORITY_OTHER
    above = [c for c in PRIORITY_CLASSES[: PRIORITY_CLASSES.index(cls)] if c in waiting]
    if 0 < free_slots <= len(above):
        return (
            f"PRIORITY-HOLDBACK: the pool has {free_slots} free slot(s), held for {', '.join(above)}; "
            f"a {cls} unit waits for the next placement"
        )
    return None


def spread_key(
    r: Reading, cap: int | None = None
) -> tuple[bool, bool, float, float, float, bool, str]:
    """Rank for ``max``: a host whose link is not degraded (a degraded one still admits) before one whose is, then a host with nothing placed (an idle host takes the next worker), then the lowest placed-over-cap
    fraction, then the most free capacity, then memory, the launching host, the name (OMN-20531; the last three are the order before it).
    Ranking on free cores alone sent 75 percent of the workers to h202, the host with the most cores, until it was full."""
    cap = r.cap if cap is None else cap
    fill = 1.0 if cap <= 0 else min(1.0, r.placed / cap)
    return (
        not r.link_verdict.degraded,
        r.placed == 0,
        -fill,
        r.rank_free,
        r.mem_avail_gb,
        r.host.local,
        r.host.name,
    )


def choose(engine: str, readings: Sequence[Reading]) -> Placement | None:
    """The host to run ``engine`` on: of the hosts holding a login for it with a free slot, the one ``spread_key`` ranks first
    (an idle host, then the lowest fill, then the most free capacity). None: no host holding that login has headroom."""

    def best(eng: str) -> Reading | None:
        fit = [r for r in readings if eng in r.host.engines and r.slots > 0]
        if not fit:
            return None
        return max(fit, key=spread_key)

    chosen_engine = resolve_engine(engine)
    pick = best(chosen_engine)
    if pick is None:
        return None
    return Placement(
        host=pick.host, engine=chosen_engine, reading=pick, readings=tuple(readings)
    )


def choose_codex(
    readings: Sequence[Reading], allowed: frozenset[str]
) -> Placement | None:
    """The lab host to run the Codex engine on (OMN-20531): of the hosts ``Reading.codex_refusal`` passes (a host the shared table marks
    ``codex: true``, with a Codex login, no Codex limit mark, a Codex sandbox that can write, and a free slot), the one
    ``spread_key`` ranks first. None: no host can, and the worker falls back to the node's Claude engine."""
    fit = [r for r in readings if r.codex_refusal(allowed) is None]
    if not fit:
        return None
    pick = max(fit, key=spread_key)
    return Placement(
        host=pick.host, engine=CODEX_ENGINE, reading=pick, readings=tuple(readings)
    )


def claude_login_refusal(why: str | None) -> str:
    """The refusal for a host the probe admitted no Claude login on, from its CLAUDEWHY word (OMN-20864): a real call that
    failed or no claude binary refuses the login; an unfinished or unavailable check says so. A failed call names whether
    a readable token file was behind it (``token-failed``) or the host had neither a native login nor a token file
    (``no-login``), so a dead token is not read as a host that was never logged in (OMN-19998)."""
    if why == "no-claude":
        return "NO-CLAUDE-LOGIN: claude is not installed on the host"
    if why == "auth-unavailable":
        return "CLAUDE-AUTH-UNAVAILABLE: claude auth status timed out or could not run; the next probe retries it"
    if why == "unchecked":
        return "CLAUDE-LOGIN-UNCHECKED: no claude -p check has finished yet; the probe started one and the next probe reads it"
    if why == "no-login":
        return "NO-CLAUDE-LOGIN: the host has no native login and no token file, and a real claude -p answered not logged in"
    if why and why.startswith("token-failed:"):
        return (
            f"CLAUDE-TOKEN-FAILED: the token file on the host is readable and a real claude -p with it answered "
            f"{why.partition(':')[2] or 'error'}"
        )
    if why and why.startswith("call-failed:"):
        return f"CLAUDE-CALL-FAILED: a real claude -p on the host answered {why.partition(':')[2] or 'error'}"
    return "NO-CLAUDE-LOGIN: the probe read no Claude login"


def preflight_skips(
    readings: Sequence[Reading],
    engine: str,
    allowed: frozenset[str] = frozenset(),
    *,
    lane: bool = False,
) -> dict[str, str]:
    """Why each host cannot take ``engine`` now, by host name, for the placement record (OMN-20531): the named refusal a worker reads
    instead of a bare ``host: null``. A host that can take it is absent. For the Codex engine each reason is ``codex_refusal`` (``lane``
    for a remote lane, which the launching host may take when its row marks ``codex: true``); for a
    Claude engine it is a Claude usage-limit or expired-login mark (the host's engines are empty), no Claude login, the admission
    refusal, or no free slot."""
    skips: dict[str, str] = {}
    for r in readings:
        if engine == CODEX_ENGINE:
            why = r.codex_refusal(allowed, lane=lane)
        elif r.error is not None:
            why = f"UNREADABLE: {r.error[:60]}"
        elif r.claude_limited_until:
            why = f"CLAUDE-LIMITED: a Claude usage limit holds until {r.claude_limited_until}"
        elif r.claude_auth_expired_at:
            why = f"CLAUDE-AUTH-EXPIRED: the Claude login expired (seen {r.claude_auth_expired_at})"
        elif engine not in r.host.engines:
            why = claude_login_refusal(r.claude_login_why)
        elif (r.lane_slots if lane else r.slots) <= 0:
            why = (
                (r.lane_admission_refusal or r.no_slot_reason)
                if lane
                else r.no_slot_reason
            )
        else:
            why = None
        if why:
            skips[r.host.name] = why
    return skips


# ---------------------------------------------------------------------------------------------------
# THE RECEIPT RELAY (OMN-17427). A lab lane's cited ``runs=`` and ``codex=`` ids name artifacts that exist
# only on the lab host; the launching host's counters (queue-status) cannot see them. At close the runner
# (``onex_remote_lane``) and a remote landing worker (``landing_worker_run``) call ``relay_receipts``: ONE ssh
# call per host copies ``run.json`` and ``receipt.json`` of each cited run id the host holds into
# ``<registry root>/.onex_state/runs/<id>/`` (never over an existing directory), and for each cited Codex thread
# the host holds, writes an existence record (no content) under
# ``<registry root>/.claude_scratch/delegation-receipts-relayed/<lane>/codex-<id>.json``. That is a separate root
# from ``delegation-receipts``: ``delegation_step.read_lane`` and ``_dispose_target`` read every
# ``<lane>/*.json`` there as the lane's OWN step receipt, so a relay file there would be miscounted.
# The relay never raises and never changes a caller's outcome: its summary is recorded by the caller.
# ``backfill_receipts`` repairs older TERMINAL citations (``onex_remote_lane.py backfill-receipts``).
# ---------------------------------------------------------------------------------------------------

RECEIPT_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
RELAYED_ROOT = "delegation-receipts-relayed"
# A cited run id is an onex delegate run (.onex_state/runs/<id>) or, for GLM through Claude Code since OMN-20173,
# a harness-delegate run (.onex_state/harness-delegate/<id>/receipt.json, OMN-20374). Both roots are relayed.
HARNESS_ROOT = "harness-delegate"
RUN_ROOTS = ("runs", HARNESS_ROOT)
_RECEIPT_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_RECEIPT_ROW_HEAD_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| TERMINAL \|"
)
_RECEIPT_LANE_RE = re.compile(r"(?:^|[\s|])lane=([A-Za-z0-9._-]+)")
_RECEIPT_CELL_RE = re.compile(r"(?:^|[\s|,;])(runs|codex)=([^\s|]*)")
_RECEIPT_MAX_MEMBER = 2 * 1024 * 1024


def _receipt_id_valid(value: object) -> bool:
    return isinstance(value, str) and RECEIPT_UUID_RE.fullmatch(value) is not None


def _unique_ids(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def receipt_ids_from_line(runs: str, codex: str) -> tuple[list[str], list[str]]:
    """Only valid, distinct UUIDs, in citation order."""
    run_ids, codex_ids = (
        _unique_ids(i for i in cell.split(",") if _receipt_id_valid(i))
        for cell in (runs, codex)
    )
    return run_ids, codex_ids


def receipt_ids_from_log(text: str) -> list[str]:
    """Successful run ids from the landing worker's delegation.jsonl."""
    ids: list[str] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(item, dict)
            and item.get("status") == "ok"
            and _receipt_id_valid(item.get("run_id"))
        ):
            ids.append(item["run_id"])
    return _unique_ids(ids)


def split_local_receipts(
    local_registry_root: str | Path,
    run_ids: Iterable[str],
    codex_ids: Iterable[str],
    codex_home: str | Path | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """An existing run directory or Codex evidence is sufficient to avoid a host query."""
    home = Path(local_registry_root)
    codex_root = Path(
        codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    )
    already: list[str] = []
    run_need: list[str] = []
    codex_need: list[str] = []
    for rid in _unique_ids(run_ids):
        if _receipt_id_valid(rid) and any(
            (home / ".onex_state" / root / rid).is_dir() for root in RUN_ROOTS
        ):
            already.append(rid)
        else:
            run_need.append(rid)
    for cid in _unique_ids(codex_ids):
        if _receipt_id_valid(cid) and (
            any(
                p.is_file()
                for p in (home / ".claude_scratch" / RELAYED_ROOT).glob(
                    f"*/codex-{cid}.json"
                )
            )
            or any(
                p.is_file()
                for p in (codex_root / "sessions").glob(f"*/*/*/rollout-*-{cid}.jsonl")
            )
        ):
            already.append(cid)
        else:
            codex_need.append(cid)
    return _unique_ids(already), run_need, codex_need


def _receipt_relay_body(
    remote_registry_root: str | Path | None,
    run_ids: Sequence[str],
    codex_ids: Sequence[str],
) -> str:
    if remote_registry_root:
        home = f"H={shlex.quote(str(remote_registry_root))}"
    else:
        home = bind_registry_roots("""H=""
for candidate in __REGISTRY_ROOTS__; do
    if [ -n "$candidate" ] && [ -d "$candidate/omnibase_infra" ]; then
        H="$candidate"
        break
    fi
done""")
    return (
        home
        + """
if [ -z "$H" ]; then printf 'RELAY-NO-HOME\\n'; exit 0; fi
printf 'RELAY-HOME %s\\n' "$H"
files=()
harness=()
"""
        + f"for id in {' '.join(run_ids)}; do\n"
        + """    if [ -f "$H/.onex_state/runs/$id/receipt.json" ]; then
        printf 'RELAY-RUN %s\\n' "$id"
        files+=("$id/receipt.json")
        if [ -f "$H/.onex_state/runs/$id/run.json" ]; then files+=("$id/run.json"); fi
    elif [ -f "$H/.onex_state/harness-delegate/$id/receipt.json" ]; then
        printf 'RELAY-HARNESS %s\\n' "$id"
        harness+=("harness-delegate/$id/receipt.json")
    fi
done
"""
        + f"for id in {' '.join(codex_ids)}; do\n"
        + """    p=$(ls "${CODEX_HOME:-$HOME/.codex}"/sessions/*/*/*/rollout-*-"$id".jsonl 2>/dev/null | head -n 1)
    if [ -n "$p" ]; then printf 'RELAY-CODEX %s %s\\n' "$id" "$p"; fi
done
printf 'RELAY-TAR\\n'
rc=0
# GNU tar refuses a -C that no member follows, so each -C goes in only with its own members.
members=()
if [ "${#files[@]}" -gt 0 ]; then members+=(-C "$H/.onex_state/runs" "${files[@]}"); fi
if [ "${#harness[@]}" -gt 0 ]; then members+=(-C "$H/.onex_state" "${harness[@]}"); fi
if [ "${#members[@]}" -gt 0 ]; then
    tar -cf - "${members[@]}"
    rc=$?
fi
exit "$rc"
"""
    )


def _as_bytes(value: str | bytes | None) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else (value or b"")


def _receipt_tar_records(
    payload: bytes | None, reported: Sequence[str], harness: Sequence[str] = ()
) -> tuple[dict[str, dict[str, tuple[bytes, float]]], set[str]]:
    """Validate the full archive before writing anything; no extraction to paths.

    ``<id>/receipt.json`` and ``<id>/run.json`` are onex delegate runs; ``harness-delegate/<id>/receipt.json``
    is a harness-delegate run, accepted only for an id the host reported as RELAY-HARNESS."""
    records: dict[str, dict[str, tuple[bytes, float]]] = {
        rid: {} for rid in (*reported, *harness)
    }
    invalid: set[str] = set()
    if not payload:
        return records, invalid
    with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
        for member in archive:
            parts = member.name.split("/")
            if (
                len(parts) == 3
                and parts[0] == HARNESS_ROOT
                and parts[1] in harness
                and parts[2] == "receipt.json"
            ):
                parts = parts[1:]
            elif (
                len(parts) != 2
                or parts[0] not in reported
                or parts[1] not in ("receipt.json", "run.json")
            ):
                continue
            if not member.isfile():
                continue
            rid, filename = parts
            if member.size > _RECEIPT_MAX_MEMBER or filename in records[rid]:
                invalid.add(rid)
                continue
            source = archive.extractfile(member)
            if source is None:
                invalid.add(rid)
                continue
            with source:
                data = source.read(_RECEIPT_MAX_MEMBER + 1)
            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    invalid.add(rid)
                    continue
            except (ValueError, UnicodeError):
                invalid.add(rid)
                continue
            records[rid][filename] = (data, member.mtime)
    return records, invalid


def _receipt_lane_dir(lane: object) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(lane))
    return name if name and name not in (".", "..") else "unknown"


def relay_receipts(
    *,
    target: str,
    remote_registry_root: str | Path | None,
    run_ids: Sequence[str],
    codex_ids: Sequence[str],
    local_registry_root: str | Path | None,
    lane: object,
    host_name: str,
    codex_lanes: Mapping[str, str] | None = None,
    codex_home: str | Path | None = None,
    run: Callable[..., Any] = subprocess.run,
    timeout: float = 180,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Best effort only: return a summary, including any failure, without raising."""
    summary: dict[str, Any] = {
        "copied": [],
        "already_local": [],
        "missing": [],
        "codex_relayed": [],
        "error": None,
    }
    stage = None
    order = []
    try:
        if not local_registry_root:
            summary["error"] = "ONEX_REGISTRY_ROOT is unset on the launching host"
            return summary
        run_ids, codex_ids = _unique_ids(run_ids), _unique_ids(codex_ids)
        order = _unique_ids([*run_ids, *codex_ids])
        summary["missing"] = [i for i in order if not _receipt_id_valid(i)]
        already, run_need, codex_need = split_local_receipts(
            local_registry_root,
            [i for i in run_ids if _receipt_id_valid(i)],
            [i for i in codex_ids if _receipt_id_valid(i)],
            codex_home,
        )
        summary["already_local"] = already
        if not run_need and not codex_need:
            return summary
        body = _receipt_relay_body(remote_registry_root, run_need, codex_need)
        # A comment cannot let a newline in a caller's lane become a shell command.
        comment_lane = str(lane).replace("\n", "_").replace("\r", "_")
        script = f"# lane={comment_lane}\nbash -c {shlex.quote(body)}"
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=30",
            target,
            script,
        ]
        try:
            proc = run(argv, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            summary["error"] = f"timed out after {timeout}s"
            return summary
        except OSError as exc:
            summary["error"] = str(exc)
            return summary
        if proc.returncode != 0:
            stderr = _as_bytes(proc.stderr).decode("utf-8", errors="replace")
            summary["error"] = f"ssh rc={proc.returncode}: {stderr[-200:]}"
            return summary
        stdout = _as_bytes(proc.stdout)
        head, separator, tail = stdout.partition(b"RELAY-TAR\n")
        lines = head.decode("utf-8", errors="replace").splitlines()
        if "RELAY-NO-HOME" in lines:
            summary["error"] = "no registry root on the host"
            return summary
        if not separator:
            summary["error"] = "host report lacks RELAY-TAR"
            return summary
        reported_runs, reported_harness, reported_codex = set(), set(), {}
        for line in lines:
            if line.startswith("RELAY-RUN "):
                rid = line[len("RELAY-RUN ") :]
                if rid in run_need:
                    reported_runs.add(rid)
            elif line.startswith("RELAY-HARNESS "):
                rid = line[len("RELAY-HARNESS ") :]
                if rid in run_need and rid not in reported_runs:
                    reported_harness.add(rid)
            elif line.startswith("RELAY-CODEX "):
                parts = line.split(" ", 2)
                if len(parts) == 3 and parts[1] in codex_need:
                    reported_codex[parts[1]] = parts[2]
        records, invalid = _receipt_tar_records(
            tail, sorted(reported_runs), sorted(reported_harness)
        )
        home = Path(local_registry_root)
        for rid in run_need:
            files = records.get(rid, {})
            if rid in invalid or "receipt.json" not in files:
                summary["missing"].append(rid)
                continue
            state = home / ".onex_state"
            runs_root = state / (HARNESS_ROOT if rid in reported_harness else "runs")
            runs_root.mkdir(parents=True, exist_ok=True)
            if stage is None:
                stage = Path(tempfile.mkdtemp(prefix=".relay-", dir=state))
            staged = stage / rid
            staged.mkdir()
            for filename, (data, mtime) in files.items():
                path = staged / filename
                path.write_bytes(data)
                os.utime(path, (mtime, mtime))
            latest = max(mtime for _, mtime in files.values())
            os.utime(staged, (latest, latest))
            dest = runs_root / rid
            if dest.is_dir():
                summary["already_local"].append(rid)
                continue
            try:
                os.rename(staged, dest)
            except OSError:
                if not dest.is_dir():
                    raise
                summary["already_local"].append(rid)
            else:
                summary["copied"].append(rid)
        timestamp = (
            (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        for cid in codex_need:
            if cid not in reported_codex:
                summary["missing"].append(cid)
                continue
            record_lane = (codex_lanes or {}).get(cid, lane)
            directory = (
                home / ".claude_scratch" / RELAYED_ROOT / _receipt_lane_dir(record_lane)
            )
            directory.mkdir(parents=True, exist_ok=True)
            record = {
                "status": "ok",
                "thread_id": cid,
                "host": host_name,
                "remote_path": reported_codex[cid],
                "relayed_at": timestamp,
                "lane": record_lane,
            }
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=directory,
                    prefix=".relay-",
                    delete=False,
                ) as tmp:
                    temp_path = Path(tmp.name)
                    tmp.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
                os.replace(temp_path, directory / f"codex-{cid}.json")
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            summary["codex_relayed"].append(cid)
    except Exception as exc:
        logging.getLogger(__name__).exception("Lab worker operation failed")
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if stage is not None:
            try:
                shutil.rmtree(stage)
            except Exception as exc:
                logging.getLogger(__name__).exception("Lab worker operation failed")
                summary["error"] = summary["error"] or f"{type(exc).__name__}: {exc}"
        for key in ("copied", "already_local", "missing", "codex_relayed"):
            summary[key] = [i for i in order if i in summary[key]]
    return summary


def backfill_receipts(
    *,
    since: str,
    ledger: str | Path,
    local_registry_root: str | Path,
    hosts: Iterable[Mapping[str, Any]],
    run: Callable[..., Any] = subprocess.run,
    codex_home: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retry unresolved TERMINAL citations against each available host, in table order."""
    if not isinstance(since, str) or not _RECEIPT_TS_RE.fullmatch(since):
        raise ValueError("since must be a UTC timestamp: YYYY-MM-DDTHH:MM:SSZ")
    runs: list[str] = []
    codex: list[str] = []
    codex_lanes: dict[str, str] = {}
    for line in Path(ledger).read_text(encoding="utf-8").splitlines():
        head = _RECEIPT_ROW_HEAD_RE.match(line)
        if not head or head[1] < since:
            continue
        lane_match = _RECEIPT_LANE_RE.search(line)
        lane = lane_match[1] if lane_match else "unknown"
        for cell, values in _RECEIPT_CELL_RE.findall(line):
            ids = [i for i in values.split(",") if _receipt_id_valid(i)]
            if cell == "runs":
                runs.extend(ids)
            else:
                codex.extend(ids)
                for cid in ids:
                    codex_lanes[cid] = lane
    runs, codex = _unique_ids(runs), _unique_ids(codex)
    _, run_need, codex_need = split_local_receipts(
        local_registry_root, runs, codex, codex_home
    )
    report: dict[str, Any] = {
        "since": since,
        "cited_runs": len(runs),
        "cited_codex": len(codex),
        "hosts": {},
        "unresolved": [],
    }
    for host in hosts:
        if not run_need and not codex_need:
            break
        if (
            host.get("local")
            or not host.get("target")
            or host.get("available") is False
        ):
            continue
        summary = relay_receipts(
            target=host["target"],
            remote_registry_root=None,
            run_ids=run_need,
            codex_ids=codex_need,
            local_registry_root=local_registry_root,
            lane="backfill",
            host_name=host["name"],
            codex_lanes=codex_lanes,
            codex_home=codex_home,
            run=run,
            now=now,
        )
        report["hosts"][host["name"]] = summary
        resolved = set(
            summary["copied"] + summary["codex_relayed"] + summary["already_local"]
        )
        run_need = [i for i in run_need if i not in resolved]
        codex_need = [i for i in codex_need if i not in resolved]
    report["unresolved"] = _unique_ids([*run_need, *codex_need])
    return report


def choose_lane(engine: str, readings: Sequence[Reading]) -> Placement | None:
    """The host for one LANE (OMN-17427, RULING 2026-10-02T10:59:04Z): the lab host with the most free capacity that holds a
    login for ``engine`` and has a lane slot, and only when no lab host has one, the launching host if it passes its
    own bar (``lane_slots``). None: no host has a slot, the launching host included."""
    chosen_engine = resolve_engine(engine)
    held = [r for r in readings if chosen_engine in r.host.engines]
    for tier in (
        [r for r in held if not r.host.local],
        [r for r in held if r.host.local],
    ):
        fit = [r for r in tier if r.lane_slots > 0]
        if fit:
            pick = max(fit, key=lambda r: spread_key(r, r.lane_cap))
            return Placement(
                host=pick.host,
                engine=chosen_engine,
                reading=pick,
                readings=tuple(readings),
            )
    return None


# ---- the lane queue: a lane that finds no host waits here and starts on the next free slot (OMN-19998) -----------------------
#
# 2026-10-17 13:14-13:21Z: three lanes each ended no-host twice because h201 flapped (swap traffic, CPU temperature) at the
# moment the runner read the pool, while the next throughput tick read 9-10 free slots on it. A pinned host gave no-host with
# no fallback, and the tick's one MISSING line per lane was acted on only when the orchestrator re-sent the lane by hand.
# A no-host run now writes one entry per lane here (the runner's own relaunch argv, model, priority class, pin), the throughput
# tick starts entries when its pool read shows a free slot for the engine, and the runner removes the entry when the lane places.
# The state is dotted so ``placed_counts`` and ``lane_counts`` leave it alone. One event line per enqueue, start and expire.
LANE_QUEUE_DIR = ".lane-queue"
LANE_QUEUE_EVENTS = ".lane-queue-events.jsonl"
LANE_QUEUE_TTL_S = (
    3 * 3600.0
)  # a lane unplaced this long is dropped from the queue, and the dispatch finding reports it
LANE_QUEUE_RELAUNCH_S = 300.0  # an entry just relaunched is left alone while the relaunch places or ends no-host again


def _lane_queue_file(directory: Path, lane: str) -> Path:
    return directory / LANE_QUEUE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', lane)}.json"


@contextmanager
def _lane_queue_lock(directory: Path) -> Iterator[None]:
    (directory / LANE_QUEUE_DIR).mkdir(parents=True, exist_ok=True)
    with (directory / LANE_QUEUE_DIR / ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _lane_queue_write(path: Path, entry: Mapping[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _lane_queue_event(
    directory: Path, event: str, lane: str, now: datetime, **cells: Any
) -> None:
    line = json.dumps(
        {"at": _utc_stamp(now), "event": event, "lane": lane, **cells}, sort_keys=True
    )
    with (directory / LANE_QUEUE_EVENTS).open("a", encoding="utf-8") as events:
        events.write(line + "\n")


def lane_queue_events(directory: Path) -> list[dict[str, Any]]:
    """The queue's event lines, oldest first; a line that does not parse is skipped."""
    out: list[dict[str, Any]] = []
    try:
        text = (directory / LANE_QUEUE_EVENTS).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def lane_queue_enqueue(
    directory: Path,
    lane: str,
    *,
    argv: Sequence[str],
    model: str,
    priority_class: str,
    pin: str | None,
    run_id: str,
    reason: str,
    now: datetime | None = None,
    ttl_s: float = LANE_QUEUE_TTL_S,
) -> dict[str, Any]:
    """Queue ``lane`` after a no-host outcome, or note that it ended no-host again. ``argv`` is the runner's own command
    for the lane again (after the script), without ``--host``: the pin is the entry's, applied by ``lane_queue_pin``. The first
    enqueue fixes ``enqueued_at``, ``pin`` and ``expires_at`` and writes the one ``enqueue`` event; a lane that ends
    no-host again (a flapping host) keeps all three and only counts the attempt."""
    now = now or datetime.now(UTC)
    path = _lane_queue_file(directory, lane)
    with _lane_queue_lock(directory):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entry = None
        if not isinstance(entry, dict) or not entry.get("enqueued_at"):
            entry = {
                "lane": lane,
                "enqueued_at": _utc_stamp(now),
                "pin": pin,
                "expires_at": _utc_stamp(now + timedelta(seconds=ttl_s)),
                "attempts": 0,
            }
            _lane_queue_event(
                directory,
                "enqueue",
                lane,
                now,
                model=model,
                priority_class=priority_class,
                pin=pin,
                run_id=run_id,
                expires_at=entry["expires_at"],
            )
        entry.update(
            argv=list(argv),
            model=model,
            priority_class=priority_class,
            last_run_id=run_id,
            last_reason=reason,
            updated_at=_utc_stamp(now),
            attempts=int(entry.get("attempts") or 0) + 1,
        )
        _lane_queue_write(path, entry)
    return entry


def lane_queue_entries(directory: Path) -> list[dict[str, Any]]:
    """The queued lanes, most urgent first: by priority class, then by how long each has waited. An entry that does not
    parse is skipped, so one bad file never stops the queue."""
    entries: list[dict[str, Any]] = []
    for path in sorted((directory / LANE_QUEUE_DIR).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(doc, dict)
            and doc.get("lane")
            and _marker_time(doc.get("enqueued_at")) is not None
        ):
            entries.append(doc)

    def key(entry: Mapping[str, Any]) -> tuple[int, str, str]:
        cls = entry.get("priority_class")
        return (
            PRIORITY_CLASSES.index(cls)
            if cls in PRIORITY_CLASSES
            else len(PRIORITY_CLASSES),
            str(entry.get("enqueued_at")),
            str(entry.get("lane")),
        )

    return sorted(entries, key=key)


def lane_queue_pin(entry: Mapping[str, Any]) -> str | None:
    """The host the lane is pinned to. A pin is a requirement (OMN-21718): the entry keeps it until it expires, so the relaunch
    asks that host and no other."""
    pin = entry.get("pin")
    return str(pin) if pin else None


def lane_queue_start(
    directory: Path, lane: str, *, run_id: str, host: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """The lane was placed: take its entry out and write the one ``start`` event. None when it was not queued (no event)."""
    now = now or datetime.now(UTC)
    path = _lane_queue_file(directory, lane)
    with _lane_queue_lock(directory):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        path.unlink(missing_ok=True)
        queued = (
            _marker_time(entry.get("enqueued_at")) if isinstance(entry, dict) else None
        )
        _lane_queue_event(
            directory,
            "start",
            lane,
            now,
            host=host,
            run_id=run_id,
            attempts=entry.get("attempts"),
            wait_s=int((now - queued).total_seconds()) if queued else None,
        )
    return entry if isinstance(entry, dict) else None


def lane_queue_expire(
    directory: Path, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Remove each entry past its ``expires_at``, one ``expire`` event each, and return them."""
    now = now or datetime.now(UTC)
    gone: list[dict[str, Any]] = []
    with _lane_queue_lock(directory):
        for entry in lane_queue_entries(directory):
            expires = _marker_time(entry.get("expires_at"))
            if expires is not None and expires > now:
                continue
            _lane_queue_file(directory, str(entry["lane"])).unlink(missing_ok=True)
            queued = _marker_time(entry.get("enqueued_at"))
            _lane_queue_event(
                directory,
                "expire",
                str(entry["lane"]),
                now,
                attempts=entry.get("attempts"),
                reason=entry.get("last_reason"),
                wait_s=int((now - queued).total_seconds()) if queued else None,
            )
            gone.append(entry)
    return gone


def lane_queue_launched(directory: Path, lane: str, now: datetime) -> None:
    """Record that the queue's owner has just relaunched ``lane``, so the next read leaves it alone for a while."""
    path = _lane_queue_file(directory, lane)
    with _lane_queue_lock(directory):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entry["launched_at"] = _utc_stamp(now)
        _lane_queue_write(path, entry)


def lane_queue_relaunch_due(entry: Mapping[str, Any], now: datetime) -> bool:
    """False while a relaunch from the last ``LANE_QUEUE_RELAUNCH_S`` is still placing and has not ended no-host again."""
    launched, updated = (
        _marker_time(entry.get("launched_at")),
        _marker_time(entry.get("updated_at")),
    )
    if launched is None or (updated is not None and updated > launched):
        return True
    return (now - launched).total_seconds() >= LANE_QUEUE_RELAUNCH_S


def lane_queue_summary(
    entries: Sequence[Mapping[str, Any]], now: datetime
) -> dict[str, Any]:
    """The queue's depth and its oldest wait."""
    oldest = min(entries, key=lambda e: str(e.get("enqueued_at")), default=None)
    queued = _marker_time(oldest.get("enqueued_at")) if oldest else None
    return {
        "depth": len(entries),
        "oldest_lane": oldest.get("lane") if oldest else None,
        "oldest_wait_s": int((now - queued).total_seconds()) if queued else 0,
    }


# ---- The node's canonical entry ---------------------------------------------------------------------------------------
class HandlerLandingPlacement:
    """``handle(request) -> result``: the node's one entry. The probe transport is a constructor-injected :class:`Capture`."""

    def __init__(self, capture: Capture | None = None) -> None:
        self._capture: Capture = capture or _capture

    def handle(
        self, request: ModelLandingPlacementRequest
    ) -> ModelLandingPlacementResult:
        """Read the pool. No host table and no ``LANDING_POOL_HOSTS`` refuses with exit 75 naming ``lab_hosts.hosts``;
        ``LANDING_POOL_HOSTS=[]`` reads an empty pool (every worker runs on the launching host)."""
        environ = request.environ if request.environ is not None else os.environ
        try:
            hosts = load_pool(environ)
            source = pool_source(environ)
        except NodeOverlayError as exc:
            return ModelLandingPlacementResult(exit_code=EXIT_NO_POOL, message=str(exc))
        readings = read_pool(
            hosts, placed=request.placed, capture=self._capture, lane=request.lane
        )
        return ModelLandingPlacementResult(
            exit_code=0,
            message=f"read {len(readings)} host(s) from {source}",
            pool_source=source,
            hosts=tuple(
                ModelPoolHostReading(
                    name=r.host.name,
                    engines=tuple(sorted(r.host.engines)),
                    cap=r.cap,
                    slots=r.slots,
                    placed=r.placed,
                    verdict=r.admission_refusal,
                    error=r.error,
                )
                for r in readings
            ),
        )


def __getattr__(name: str) -> str:
    """``_PROBE``: the pool probe script with no parity clones, rendered when read (it needs the overlay's registry roots)."""
    if name == "_PROBE":
        return probe_script()
    raise AttributeError(name)
