# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab collector: hosts, lanes, per-host census and model servers.

**Read-only, always.** Every command this module issues, local or over ssh, passes
:func:`is_read_only_command` first. It allows exactly: ``hostname``; ``docker ps`` (any flags);
``docker network ls``; ``cat`` of a lane-lock holder sidecar path; ``kill -0 <pid>`` (liveness
check only -- no other signal); and a read-only ``curl``. Everything else -- ``docker
compose up``, ``rm``, ``restart``, ``kill`` of a container, a mutating ``curl -X POST`` -- is
refused with :class:`NotReadOnlyError` before a subprocess is ever started (rule: no mutation on
any lab host).

**FREE requires all three (tri-state discipline).** A lane is
:attr:`LaneStatus.FREE` only when its containers are
running per the same-minute docker read, no ledger ``HOLD`` in force names its compose project as
``surface=``, and no live lane-lock holder sidecar names a running pid. Any of those three failing
makes it :attr:`~...LaneStatus.BUSY` (declared, reachable, just not available right now); an
unreadable probe -- an ssh timeout, a lock-holder file that will not parse -- makes it
:attr:`~...LaneStatus.UNKNOWN`, never ``FREE`` and never silently ``BUSY``. A host
``static_facts.yaml`` lists under ``out_of_service_hosts`` is not probed at all and every one of
its lanes renders :attr:`~...LaneStatus.OUT_OF_SERVICE` with the ruling that took it out.

**Every read is dependency-injected.** :data:`Runner` is the one seam between this module's pure
logic and a real subprocess: :func:`test_collect_lab.py` replays a recorded ``docker ps -a``,
a HOLD-bearing ledger read, and an ssh timeout (exit 124) through the same seam a live run uses,
so the FREE/BUSY/UNKNOWN decision is unit-tested without a lab host.
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..models.model_capability_catalog import (
    SCHEMA_ID,
    Catalog,
    Host,
    Lane,
    LaneStatus,
    ModelServer,
    Probe,
    ProbeState,
)
from ..models.model_capability_catalog_collect_request import (
    ModelCapabilityCatalogCollectRequest,
)
from ..models.model_capability_catalog_collect_result import (
    ModelCapabilityCatalogCollectResult,
)
from ..models.model_capability_catalog_sources import (
    HostSource,
    LaneSource,
    ModelServerSource,
    RemoteResult,
    Sources,
)
from .handler_capability_catalog_render import (
    load_static_facts,
)

DEFAULT_SOURCES_PATH = Path(__file__).with_name("sources.yaml")

# The lane-lock holder sidecar lives at exactly this shape (lane_lock.py in
# omnibase_infra, ``holder_path``). ``cat`` of anything else is refused.
_HOLDER_SUFFIX = ".lock.holder"
_HOLDER_DIR_MARKER = ".omnibase/state/lane-locks/"

# curl flags that would make a request mutate or upload something. Their
# absence, not an allowlist of "safe" flags, is what makes a curl read-only:
# a probe URL and a small number of read-shaping flags (-s, -m, -H, -k, -L)
# are all fine.
_CURL_MUTATING_FLAGS = frozenset(
    {
        "-X",
        "--request",
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "-F",
        "--form",
        "-T",
        "--upload-file",
    }
)

# Docker subcommands read-only by construction. Anything else (`up`, `down`,
# `rm`, `restart`, `kill` <container>, `compose`, `exec`, `stop`, `pause`) is
# refused; this is an allowlist of subcommands, not a denylist of verbs, so a
# future docker subcommand this module has never heard of is refused too.
_DOCKER_READONLY_SUBCOMMANDS = frozenset({"ps", "inspect", "images", "version", "info"})


class NotReadOnlyError(Exception):
    """A command argv failed the read-only allowlist. Never executed."""


# --------------------------------------------------------------------------
# The read-only allowlist (falsifier: test_collect_lab.py -k read_only_allowlist)


def is_read_only_command(argv: Sequence[str]) -> bool:
    """True iff `argv` is on the read-only allowlist. Refuses everything else."""
    if not argv:
        return False
    head = argv[0]
    if head == "hostname":
        return len(argv) == 1
    if head == "cat":
        return len(argv) == 2 and _is_holder_path(argv[1])
    if head == "kill":
        # kill -0 <pid> only -- a liveness probe, never a signal that kills anything.
        return len(argv) == 3 and argv[1] == "-0" and argv[2].lstrip("-").isdigit()
    if head == "curl":
        return _is_read_only_curl(argv)
    if head == "docker":
        return _is_read_only_docker(argv)
    if head in {"zsh", "bash", "sh"}:
        return _is_read_only_shell_wrap(argv)
    return False


def _is_holder_path(path: str) -> bool:
    normalized = path.replace("$HOME", "~")
    return _HOLDER_DIR_MARKER in normalized and normalized.endswith(_HOLDER_SUFFIX)


def _is_read_only_curl(argv: Sequence[str]) -> bool:
    if len(argv) < 2:
        return False
    return not any(token in _CURL_MUTATING_FLAGS for token in argv[1:])


def _is_read_only_docker(argv: Sequence[str]) -> bool:
    if len(argv) < 2:
        return False
    sub = argv[1]
    if sub == "ps":
        return True
    if sub == "network":
        return len(argv) >= 3 and argv[2] == "ls"
    return sub in _DOCKER_READONLY_SUBCOMMANDS


def _is_read_only_shell_wrap(argv: Sequence[str]) -> bool:
    """`zsh -lc "<cmd>"` (the .101/.105 login-shell quirk): check the inner command."""
    if "-lc" not in argv:
        return False
    inner = argv[argv.index("-lc") + 1 :]
    if len(inner) != 1:
        return False
    try:
        inner_argv = shlex.split(inner[0])
    except ValueError:
        return False
    return is_read_only_command(inner_argv)


# --------------------------------------------------------------------------
# Sources config


def load_sources(path: Path | None = None) -> Sources:
    target = path or DEFAULT_SOURCES_PATH
    with target.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    hosts: dict[str, HostSource] = {}
    for host_id, raw in (data.get("hosts") or {}).items():
        lanes = tuple(
            LaneSource(
                lane_id=str(lane["lane_id"]),
                compose_project=str(lane["compose_project"]),
            )
            for lane in raw.get("lanes", [])
        )
        servers = tuple(
            ModelServerSource(name=str(s["name"]), url=str(s["url"]))
            for s in raw.get("model_servers", [])
        )
        hosts[str(host_id)] = HostSource(
            host_id=str(host_id),
            display_name=str(raw.get("display_name", host_id)),
            local=bool(raw.get("local", False)),
            ssh_target=raw.get("ssh_target"),
            login_shell_quirk=bool(raw.get("login_shell_quirk", False)),
            lanes=lanes,
            model_servers=servers,
            onex_lab_kubeconfig_probe=bool(raw.get("onex_lab_kubeconfig_probe", False)),
        )
    return Sources(
        orchestrator_host=str(data.get("orchestrator_host", "")),
        lane_manifest_path=str(data.get("lane_manifest_path", "")),
        census_planner_path=str(data.get("census_planner_path", "")),
        ssh_timeout_s=float(data.get("ssh_timeout_s", 8)),
        curl_timeout_s=float(data.get("curl_timeout_s", 5)),
        hosts=hosts,
    )


# --------------------------------------------------------------------------
# Running a read-only command, local or over ssh


Runner = Callable[[Sequence[str], float], RemoteResult]


def default_runner(argv: Sequence[str], timeout: float) -> RemoteResult:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
        return RemoteResult(
            stdout=done.stdout, exit_code=done.returncode, timed_out=False
        )
    except subprocess.TimeoutExpired:
        return RemoteResult(stdout="", exit_code=124, timed_out=True)
    except OSError as exc:
        return RemoteResult(stdout=str(exc), exit_code=127, timed_out=False)


def _ssh_login() -> str:
    """The ssh login name: $ONEX_LAB_SSH_USER, else the local account name.

    Never a literal name committed in ``sources.yaml`` (repo rule: no person names in this
    repository) -- every lab host uses the same account this process already runs as, by
    convention, so the local account name is the correct default everywhere this collector runs.
    """
    return os.environ.get("ONEX_LAB_SSH_USER") or getpass.getuser()


def build_remote_argv(
    host: HostSource, argv: Sequence[str], timeout: float
) -> list[str]:
    """Wrap `argv` for execution on `host`: bare for local, ssh (+ login shell) otherwise."""
    if host.local:
        return list(argv)
    inner = shlex.join(argv)
    if host.login_shell_quirk:
        inner = f"zsh -lc {shlex.quote(inner)}"
    ssh_timeout = max(1, int(timeout))
    target = host.ssh_target or ""
    login_target = target if "@" in target else f"{_ssh_login()}@{target}"
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={ssh_timeout}",
        "-o",
        "StrictHostKeyChecking=no",
        login_target,
        inner,
    ]


def probe_label(host: HostSource, argv: Sequence[str]) -> str:
    cmd = " ".join(argv)
    return cmd if host.local else f"ssh {host.host_id} {cmd}"


def run_readonly(
    host: HostSource,
    argv: Sequence[str],
    timeout: float,
    runner: Runner = default_runner,
) -> RemoteResult:
    """Run `argv` on `host` iff it passes :func:`is_read_only_command`."""
    if not is_read_only_command(argv):
        raise NotReadOnlyError(
            f"refusing non-read-only command on {host.host_id}: {argv!r}"
        )
    return runner(build_remote_argv(host, argv, timeout), timeout)


# --------------------------------------------------------------------------
# Docker inventory


def parse_docker_ps_lines(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def parse_docker_network_names(stdout: str) -> list[str]:
    names: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        name = row.get("Name")
        if name:
            names.append(str(name))
    return names


def _labels_of(row: Mapping[str, Any]) -> dict[str, str]:
    labels = row.get("Labels")
    if isinstance(labels, dict):
        return {str(k): str(v) for k, v in labels.items()}
    out: dict[str, str] = {}
    for pair in str(labels or "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _is_running(row: Mapping[str, Any]) -> bool:
    state = str(row.get("State", "")).lower()
    status = str(row.get("Status", "")).lower()
    return state == "running" or status.startswith("up")


def lane_container_counts(
    containers: Sequence[Mapping[str, Any]], compose_project: str
) -> tuple[int, int]:
    """(total, running) containers labeled with `compose_project`."""
    total = running = 0
    for row in containers:
        if _labels_of(row).get("com.docker.compose.project") == compose_project:
            total += 1
            if _is_running(row):
                running += 1
    return total, running


# --------------------------------------------------------------------------
# HOLD rows (onex-ledger reader) and the lane-lock holder sidecar

LedgerHoldsRunner = Callable[[str], RemoteResult]


def _onex_ledger_bin() -> str:
    """`onex-ledger`'s path, resolved next to this interpreter first.

    Under launchd the PATH is deliberately restricted to a short system list and does
    not include this package's own venv `bin/`, where `onex-ledger` (this same package's entry
    point) actually lives. Falling back to the bare name keeps `uv run pytest` (PATH already has
    it) and any other caller working unchanged.
    """
    candidate = Path(sys.executable).parent / "onex-ledger"
    return str(candidate) if candidate.is_file() else "onex-ledger"


def _default_holds_runner(surface: str) -> RemoteResult:
    try:
        done = subprocess.run(
            [_onex_ledger_bin(), "holds", "--surface", surface],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A reader that did not answer is an unreadable ledger (`hold_in_force` returns None and
        # the lane reports UNKNOWN), not a failed collection of every other lane.
        return RemoteResult(stdout="", exit_code=None, timed_out=True)
    except OSError as exc:
        return RemoteResult(stdout=str(exc), exit_code=127, timed_out=False)
    return RemoteResult(stdout=done.stdout, exit_code=done.returncode, timed_out=False)


def hold_in_force(
    compose_project: str, *, runner: LedgerHoldsRunner = _default_holds_runner
) -> bool | None:
    """True/False, or None when the ledger reader itself could not be read (fails UNKNOWN)."""
    result = runner(compose_project)
    if result.timed_out or result.exit_code != 0:
        return None
    return not result.stdout.strip().startswith("HOLD none")


def lock_holder_alive(
    host: HostSource,
    compose_project: str,
    timeout: float,
    runner: Runner = default_runner,
) -> bool | None:
    """True: a live holder pid. False: no holder file (lock free). None: unreadable (UNKNOWN)."""
    holder_path = f"~/{_HOLDER_DIR_MARKER}{compose_project}{_HOLDER_SUFFIX}"
    cat_result = run_readonly(host, ["cat", holder_path], timeout, runner)
    if cat_result.timed_out:
        return None
    if cat_result.exit_code != 0 or not cat_result.stdout.strip():
        return False
    try:
        data = json.loads(cat_result.stdout)
    except ValueError:
        return None
    pid = data.get("pid")
    if pid is None:
        return None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    kill_result = run_readonly(host, ["kill", "-0", str(pid_int)], timeout, runner)
    if kill_result.timed_out:
        return None
    return kill_result.exit_code == 0


# --------------------------------------------------------------------------
# Model servers


def probe_model_server(
    url: str, timeout: float, now: str, runner: Runner = default_runner
) -> Probe:
    """A model server's URL already names a lab host's IP, so this is a direct curl -- never
    wrapped in ssh, even when the server lives on a remote lab host (rule: LAN-reachable model
    servers are reached directly, matching the live verification)."""
    argv = ["curl", "-s", "-m", str(max(1, int(timeout))), url]
    if not is_read_only_command(argv):
        raise NotReadOnlyError(f"refusing non-read-only model-server probe: {argv!r}")
    result = runner(argv, timeout + 1)
    if result.timed_out:
        state = ProbeState.UNKNOWN
    elif result.exit_code == 0 and result.stdout.strip():
        state = ProbeState.OK
    else:
        state = ProbeState.DOWN
    return Probe(
        state=state, probe=" ".join(argv), exit_code=result.exit_code, read_at=now
    )


# --------------------------------------------------------------------------
# The census planner (pure, per host; lane_census_plan.py in omnibase_infra)

PlannerRunner = Callable[[Mapping[str, Any], Path, Path], dict[str, Any] | None]


def feed_census_planner(
    envelope: Mapping[str, Any], planner_path: Path, manifest_path: Path
) -> dict[str, Any] | None:
    """Pipe `envelope` to the pure planner; None on any non-zero exit (never raises)."""
    if not planner_path.is_file():
        return None
    env = dict(os.environ)
    env["LANE_MANIFEST"] = str(manifest_path)
    env["LANE_CENSUS_HOST"] = str(envelope.get("host", ""))
    try:
        done = subprocess.run(
            [sys.executable, str(planner_path)],
            input=json.dumps(dict(envelope)),
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        result: dict[str, Any] = json.loads(done.stdout)
        return result
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Lane status derivation (falsifier: test_collect_lab.py -k free_requires_all_three)


def determine_lane_status(
    *,
    containers_running: bool,
    hold_in_force_value: bool | None,
    lock_holder_alive_value: bool | None,
    docker_unknown: bool,
) -> LaneStatus:
    """FREE requires all three: running containers, no hold, no live lock holder.

    Any unreadable input (`None`) makes the lane UNKNOWN, never a guessed FREE or BUSY -- except
    that an unreadable hold or lock check on an otherwise-reachable host is treated as the unsafe
    case (BUSY, not FREE): the tri-state rule says UNKNOWN never renders as free, and "cannot prove nobody
    holds it" must not read as "free".
    """
    if docker_unknown:
        return LaneStatus.UNKNOWN
    hold = bool(hold_in_force_value) or hold_in_force_value is None
    locked = bool(lock_holder_alive_value) or lock_holder_alive_value is None
    if hold or locked:
        return LaneStatus.BUSY
    if containers_running:
        return LaneStatus.FREE
    return LaneStatus.BUSY


# --------------------------------------------------------------------------
# Orchestration: one Host + its Lanes, per configured host


def _out_of_service_map(static_facts: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(entry["host_id"]): str(entry["reason"])
        for entry in static_facts.get("out_of_service_hosts") or []
    }


def collect_host(
    host: HostSource,
    sources: Sources,
    *,
    registry_root: Path,
    now: str,
    out_of_service_reason: str | None,
    runner: Runner = default_runner,
    holds_runner: LedgerHoldsRunner = _default_holds_runner,
    planner_run: Callable[
        [Mapping[str, Any], Path, Path], dict[str, Any] | None
    ] = feed_census_planner,
) -> tuple[Host, list[Lane], list[ModelServer]]:
    if out_of_service_reason is not None:
        probe = Probe(
            state=ProbeState.UNKNOWN,
            probe="not probed (out of service)",
            exit_code=None,
            read_at=now,
        )
        host_record = Host(
            host_id=host.host_id,
            display_name=host.display_name,
            state=ProbeState.UNKNOWN,
            probe=probe,
            out_of_service_reason=out_of_service_reason,
        )
        lanes = [
            Lane(
                lane_id=lane.lane_id,
                host_id=host.host_id,
                compose_project=lane.compose_project,
                status=LaneStatus.OUT_OF_SERVICE,
                probe=probe,
                note=out_of_service_reason,
            )
            for lane in host.lanes
        ]
        return host_record, lanes, []

    model_servers: list[ModelServer] = []
    for server in host.model_servers:
        server_probe = probe_model_server(
            server.url, sources.curl_timeout_s, now, runner
        )
        model_servers.append(
            ModelServer(
                name=server.name,
                host_id=host.host_id,
                url=server.url,
                state=server_probe.state,
                probe=server_probe,
            )
        )

    ps_argv = ["docker", "ps", "-a", "--format", "{{json .}}"]
    ps_result = run_readonly(host, ps_argv, sources.ssh_timeout_s, runner)
    ps_label = probe_label(host, ps_argv)

    if ps_result.timed_out:
        probe = Probe(
            state=ProbeState.UNKNOWN,
            probe=ps_label,
            exit_code=ps_result.exit_code,
            read_at=now,
        )
        host_record = Host(
            host_id=host.host_id,
            display_name=host.display_name,
            state=ProbeState.UNKNOWN,
            probe=probe,
        )
        lanes = [
            Lane(
                lane_id=lane.lane_id,
                host_id=host.host_id,
                compose_project=lane.compose_project,
                status=LaneStatus.UNKNOWN,
                probe=probe,
                note=f"ssh exit {ps_result.exit_code}",
            )
            for lane in host.lanes
        ]
        return host_record, lanes, model_servers

    ok = ps_result.exit_code == 0
    containers = parse_docker_ps_lines(ps_result.stdout) if ok else []
    host_state = ProbeState.OK if ok else ProbeState.DOWN
    host_probe = Probe(
        state=host_state, probe=ps_label, exit_code=ps_result.exit_code, read_at=now
    )
    host_record = Host(
        host_id=host.host_id,
        display_name=host.display_name,
        state=host_state,
        probe=host_probe,
    )

    if ok:
        net_argv = ["docker", "network", "ls", "--format", "{{json .}}"]
        net_result = run_readonly(host, net_argv, sources.ssh_timeout_s, runner)
        networks = (
            parse_docker_network_names(net_result.stdout)
            if not net_result.timed_out and net_result.exit_code == 0
            else []
        )
        envelope = {
            "host": host.host_id,
            "containers": containers,
            "networks": networks,
        }
        planner_path = registry_root / sources.census_planner_path
        manifest_path = registry_root / sources.lane_manifest_path
        planner_run(envelope, planner_path, manifest_path)

    lanes = []
    for lane in host.lanes:
        if not ok:
            lanes.append(
                Lane(
                    lane_id=lane.lane_id,
                    host_id=host.host_id,
                    compose_project=lane.compose_project,
                    status=LaneStatus.UNKNOWN,
                    probe=host_probe,
                    note=f"docker exit {ps_result.exit_code}",
                )
            )
            continue
        total, running_ct = lane_container_counts(containers, lane.compose_project)
        hold = hold_in_force(lane.compose_project, runner=holds_runner)
        holder_alive = lock_holder_alive(
            host, lane.compose_project, sources.ssh_timeout_s, runner
        )
        status = determine_lane_status(
            containers_running=running_ct > 0,
            hold_in_force_value=hold,
            lock_holder_alive_value=holder_alive,
            docker_unknown=False,
        )
        lanes.append(
            Lane(
                lane_id=lane.lane_id,
                host_id=host.host_id,
                compose_project=lane.compose_project,
                status=status,
                probe=host_probe,
                note=f"{total} containers, {running_ct} up",
            )
        )
    return host_record, lanes, model_servers


def collect_lab(
    sources: Sources,
    *,
    registry_root: Path,
    now: str,
    runner: Runner = default_runner,
    holds_runner: LedgerHoldsRunner = _default_holds_runner,
    planner_run: Callable[
        [Mapping[str, Any], Path, Path], dict[str, Any] | None
    ] = feed_census_planner,
    static_facts: Mapping[str, Any] | None = None,
) -> tuple[list[Host], list[Lane], list[ModelServer]]:
    """Collect every configured host, its lanes and its model servers. Read-only, never mutates."""
    facts = static_facts if static_facts is not None else load_static_facts()
    out_of_service = _out_of_service_map(facts)
    hosts_out: list[Host] = []
    lanes_out: list[Lane] = []
    model_servers_out: list[ModelServer] = []
    for host_id in sorted(sources.hosts):
        host_cfg = sources.hosts[host_id]
        host_record, lanes, model_servers = collect_host(
            host_cfg,
            sources,
            registry_root=registry_root,
            now=now,
            out_of_service_reason=out_of_service.get(host_id),
            runner=runner,
            holds_runner=holds_runner,
            planner_run=planner_run,
        )
        hosts_out.append(host_record)
        lanes_out.extend(lanes)
        model_servers_out.extend(model_servers)
    return hosts_out, lanes_out, model_servers_out


class HandlerCapabilityCatalogCollectLab:
    """Collect the lab's hosts, lanes and model servers into one catalog. Read-only."""

    def handle(
        self, request: ModelCapabilityCatalogCollectRequest
    ) -> ModelCapabilityCatalogCollectResult:
        hosts, lanes, model_servers = collect_lab(
            request.sources, registry_root=request.registry_root, now=request.now
        )
        return ModelCapabilityCatalogCollectResult(
            catalog=Catalog(
                schema=SCHEMA_ID,
                generated_at=request.now,
                hosts=tuple(hosts),
                lanes=tuple(lanes),
                model_servers=tuple(model_servers),
            )
        )
