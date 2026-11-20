"""Tests for the lab collector.

Run: uv run pytest tests/capability_catalog/test_collect_lab.py -q

Every test replays a recorded ``docker ps -a`` line, a HOLD-bearing or HOLD-free ledger read, and
an ssh timeout through the same :data:`Runner`
seam a live run uses (module docstring), so none of this needs a lab host.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_capability_catalog_effect.handlers import (
    handler_capability_catalog_collect_lab as collect_lab,
)
from omnimarket.nodes.node_capability_catalog_effect.models import (
    model_capability_catalog_sources as sources_model,
)
from omnimarket.nodes.node_capability_catalog_effect.models.model_capability_catalog import (
    LaneStatus,
    ProbeState,
)

pytestmark = pytest.mark.unit

HostSource = sources_model.HostSource
LaneSource = sources_model.LaneSource
RemoteResult = sources_model.RemoteResult
Sources = sources_model.Sources


# --------------------------------------------------------------------------
# Fixtures: recorded docker ps -a rows


def _docker_row(name: str, project: str, *, running: bool) -> str:
    row = {
        "Names": name,
        "Image": "omninode/omnibase-infra:latest",
        "State": "running" if running else "exited",
        "Status": "Up 2 hours" if running else "Exited (0) 3 hours ago",
        "Labels": {"com.docker.compose.project": project},
    }
    return json.dumps(row)


def _lab_200_ps_lines() -> str:
    """28 dev-200 containers (23 up, 5 exited) + 10 dogfood (all up) -- P1, verified 2026-09-27."""
    rows = [
        _docker_row(f"dev200-svc-{i}", "omnibase-infra-dev-200", running=True)
        for i in range(23)
    ]
    rows += [
        _docker_row(f"dev200-oneshot-{i}", "omnibase-infra-dev-200", running=False)
        for i in range(5)
    ]
    rows += [
        _docker_row(f"dogfood-svc-{i}", "omnibase-infra-dogfood", running=True)
        for i in range(10)
    ]
    return "\n".join(rows) + "\n"


def _lab_201_ps_lines_dev_up() -> str:
    return (
        _docker_row("omnibase-infra-runtime-1", "omnibase-infra", running=True) + "\n"
    )


# --------------------------------------------------------------------------
# Fake Runner: dispatches on the actual command text, whether wrapped in ssh or bare


def _inner_argv(argv: Sequence[str]) -> list[str]:
    """Unwrap ssh (and the .101/.105 `zsh -lc` login-shell quirk) back to plain tokens."""
    tokens = shlex.split(argv[-1]) if argv and argv[0] == "ssh" else list(argv)
    if len(tokens) == 3 and tokens[:2] == ["zsh", "-lc"]:
        tokens = shlex.split(tokens[2])
    return tokens


def _inner_command(argv: Sequence[str]) -> str:
    """The actual command as plain, unquoted text -- for prefix matching in fixtures."""
    return " ".join(_inner_argv(argv))


def make_fake_runner(
    responses: Mapping[str, tuple[str, int]],
    *,
    timeout_prefixes: frozenset[str] = frozenset(),
) -> collect_lab.Runner:
    def runner(argv: Sequence[str], timeout: float) -> RemoteResult:
        inner = _inner_command(argv)
        for prefix in timeout_prefixes:
            if inner.startswith(prefix):
                return RemoteResult(stdout="", exit_code=124, timed_out=True)
        for prefix, (stdout, code) in responses.items():
            if inner.startswith(prefix):
                return RemoteResult(stdout=stdout, exit_code=code, timed_out=False)
        raise AssertionError(f"no fake response recorded for command: {inner!r}")

    return runner


def fake_holds_runner(hold_surfaces: frozenset[str]) -> collect_lab.LedgerHoldsRunner:
    def holds(surface: str) -> RemoteResult:
        if surface in hold_surfaces:
            return RemoteResult(
                stdout=f"HOLD h1 by=someone scope=<surface={surface}> until=open L1\n",
                exit_code=0,
                timed_out=False,
            )
        return RemoteResult(
            stdout="HOLD none checked=1 released=0 expired=0 legacy-no-id=0\n",
            exit_code=0,
            timed_out=False,
        )

    return holds


def no_holds_runner(surface: str) -> RemoteResult:
    return RemoteResult(
        stdout="HOLD none checked=0 released=0 expired=0 legacy-no-id=0\n",
        exit_code=0,
        timed_out=False,
    )


def no_planner(
    envelope: Mapping[str, Any], planner_path: Path, manifest_path: Path
) -> dict[str, Any] | None:
    return None


def _lab_200_host() -> HostSource:
    return HostSource(
        host_id="lab-200",
        display_name=".200",
        local=True,
        ssh_target=None,
        login_shell_quirk=False,
        lanes=(
            LaneSource(lane_id="dev-200", compose_project="omnibase-infra-dev-200"),
            LaneSource(lane_id="dogfood", compose_project="omnibase-infra-dogfood"),
        ),
        model_servers=(),
    )


def _lab_201_host() -> HostSource:
    return HostSource(
        host_id="lab-201",
        display_name=".201",
        local=False,
        ssh_target="192.0.2.127",
        login_shell_quirk=False,
        lanes=(LaneSource(lane_id="dev", compose_project="omnibase-infra"),),
        model_servers=(),
    )


def _lab_105_host() -> HostSource:
    return HostSource(
        host_id="lab-105",
        display_name=".105",
        local=False,
        ssh_target="192.0.2.122",
        login_shell_quirk=True,
        lanes=(
            LaneSource(lane_id="local", compose_project="omnibase-infra-local"),
            LaneSource(lane_id="dogfood", compose_project="omnibase-infra-dogfood"),
        ),
        model_servers=(),
    )


def _sources(hosts: Mapping[str, HostSource]) -> Sources:
    return Sources(
        orchestrator_host="lab-200",
        lane_manifest_path="omnibase_infra/deploy/lane-census/lane-manifest.yaml",
        census_planner_path="omnibase_infra/scripts/lane_census_plan.py",
        ssh_timeout_s=8,
        curl_timeout_s=5,
        hosts=hosts,
    )


# --------------------------------------------------------------------------
# read_only_allowlist


@pytest.mark.parametrize(
    "argv",
    [
        ["hostname"],
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        ["docker", "ps"],
        ["docker", "network", "ls", "--format", "{{json .}}"],
        ["cat", "~/.omnibase/state/lane-locks/omnibase-infra.lock.holder"],
        ["kill", "-0", "12345"],
        ["curl", "-s", "-m", "5", "http://192.0.2.127:8000/v1/models"],
        ["zsh", "-lc", "docker ps -a --format '{{json .}}'"],
    ],
)
def test_read_only_allowlist_allows(argv: list[str]) -> None:
    assert collect_lab.is_read_only_command(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "compose", "up", "-d"],
        ["docker", "rm", "-f", "some-container"],
        ["docker", "restart", "some-container"],
        ["docker", "kill", "some-container"],
        ["docker", "network", "rm", "some-network"],
        ["docker", "stop", "some-container"],
        ["kill", "-9", "12345"],
        ["kill", "12345"],
        ["curl", "-X", "POST", "http://x/y"],
        ["curl", "-d", "payload", "http://x/y"],
        ["cat", "/etc/passwd"],
        ["cat", "ssh-dir"],
        ["rm", "-rf", "/"],
    ],
)
def test_read_only_allowlist_refuses(argv: list[str]) -> None:
    assert not collect_lab.is_read_only_command(argv)


def test_run_readonly_never_invokes_a_disallowed_runner() -> None:
    host = _lab_200_host()

    def boom(argv: Sequence[str], timeout: float) -> RemoteResult:
        raise AssertionError("a disallowed command must never reach the runner")

    with pytest.raises(collect_lab.NotReadOnlyError):
        collect_lab.run_readonly(host, ["docker", "rm", "-f", "x"], 5, runner=boom)


# --------------------------------------------------------------------------
# free_requires_all_three


@pytest.mark.parametrize(
    ("running", "hold", "holder", "expected"),
    [
        (True, False, False, LaneStatus.FREE),
        (True, True, False, LaneStatus.BUSY),
        (True, False, True, LaneStatus.BUSY),
        (True, True, True, LaneStatus.BUSY),
        (False, False, False, LaneStatus.BUSY),
    ],
)
def test_free_requires_all_three(
    running: bool, hold: bool, holder: bool, expected: LaneStatus
) -> None:
    status = collect_lab.determine_lane_status(
        containers_running=running,
        hold_in_force_value=hold,
        lock_holder_alive_value=holder,
        docker_unknown=False,
    )
    assert status is expected


def test_unreadable_hold_or_holder_never_renders_free() -> None:
    """None (unreadable) must not collapse into FREE ."""
    assert (
        collect_lab.determine_lane_status(
            containers_running=True,
            hold_in_force_value=None,
            lock_holder_alive_value=False,
            docker_unknown=False,
        )
        is LaneStatus.BUSY
    )
    assert (
        collect_lab.determine_lane_status(
            containers_running=True,
            hold_in_force_value=False,
            lock_holder_alive_value=None,
            docker_unknown=False,
        )
        is LaneStatus.BUSY
    )


def test_docker_unknown_overrides_everything_else() -> None:
    assert (
        collect_lab.determine_lane_status(
            containers_running=True,
            hold_in_force_value=False,
            lock_holder_alive_value=False,
            docker_unknown=True,
        )
        is LaneStatus.UNKNOWN
    )


# --------------------------------------------------------------------------
# dev-200 container count feeds the planner a lab-200 envelope


def test_dev_200_container_count_and_planner_envelope(tmp_path: Path) -> None:
    host = _lab_200_host()
    sources = _sources({"lab-200": host})
    runner = make_fake_runner(
        {
            "docker ps -a": (_lab_200_ps_lines(), 0),
            "docker network ls": ("", 0),
            "cat ~/.omnibase/state/lane-locks/": ("", 1),
        }
    )
    captured: list[dict[str, Any]] = []

    def capturing_planner(
        envelope: Mapping[str, Any], planner_path: Path, manifest_path: Path
    ) -> dict[str, Any] | None:
        captured.append(dict(envelope))
        return {"host": envelope["host"], "findings": [], "has_drift": False}

    host_record, lanes, _model_servers = collect_lab.collect_host(
        host,
        sources,
        registry_root=tmp_path,
        now="2026-09-28T13:00:00Z",
        out_of_service_reason=None,
        runner=runner,
        holds_runner=no_holds_runner,
        planner_run=capturing_planner,
    )

    assert host_record.state is ProbeState.OK
    dev_200 = next(lane for lane in lanes if lane.lane_id == "dev-200")
    assert dev_200.note == "28 containers, 23 up"
    assert dev_200.status is LaneStatus.FREE

    dogfood = next(lane for lane in lanes if lane.lane_id == "dogfood")
    assert dogfood.note == "10 containers, 10 up"

    assert len(captured) == 1
    assert captured[0]["host"] == "lab-200"
    assert len(captured[0]["containers"]) == 38


# --------------------------------------------------------------------------
# ssh exit 124 -> every lane on that host is UNKNOWN


def test_ssh_timeout_makes_every_lane_on_the_host_unknown(tmp_path: Path) -> None:
    host = _lab_105_host()
    sources = _sources({"lab-105": host})
    runner = make_fake_runner({}, timeout_prefixes=frozenset({"docker ps -a"}))

    host_record, lanes, _model_servers = collect_lab.collect_host(
        host,
        sources,
        registry_root=tmp_path,
        now="2026-09-28T13:00:00Z",
        out_of_service_reason=None,
        runner=runner,
        holds_runner=no_holds_runner,
        planner_run=no_planner,
    )

    assert host_record.state is ProbeState.UNKNOWN
    assert len(lanes) == 2
    assert all(lane.status is LaneStatus.UNKNOWN for lane in lanes)
    assert all("124" in (lane.note or "") for lane in lanes)


# --------------------------------------------------------------------------
# a HOLD row in force naming dev on lab-201 -> dev is BUSY


def test_a_holds_reader_timeout_leaves_the_lane_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default reader that does not answer in time is an unreadable ledger, not an
    exception that fails the collection of every lane."""

    def slow(command: Sequence[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", slow)

    assert collect_lab.hold_in_force("omnibase-infra-local") is None


def test_hold_in_force_makes_the_lane_busy(tmp_path: Path) -> None:
    host = _lab_201_host()
    sources = _sources({"lab-201": host})
    runner = make_fake_runner(
        {
            "docker ps -a": (_lab_201_ps_lines_dev_up(), 0),
            "docker network ls": ("", 0),
            "cat ~/.omnibase/state/lane-locks/": ("", 1),
        }
    )

    host_record, lanes, _model_servers = collect_lab.collect_host(
        host,
        sources,
        registry_root=tmp_path,
        now="2026-09-28T13:00:00Z",
        out_of_service_reason=None,
        runner=runner,
        holds_runner=fake_holds_runner(frozenset({"omnibase-infra"})),
        planner_run=no_planner,
    )

    assert host_record.state is ProbeState.OK
    dev = next(lane for lane in lanes if lane.lane_id == "dev")
    assert dev.status is LaneStatus.BUSY
    assert dev.note == "1 containers, 1 up"


# --------------------------------------------------------------------------
# out-of-service hosts are never probed


def test_out_of_service_host_is_never_probed_and_lanes_render_out_of_service(
    tmp_path: Path,
) -> None:
    host = _lab_200_host()
    sources = _sources({"lab-200": host})

    def boom(argv: Sequence[str], timeout: float) -> RemoteResult:
        raise AssertionError("an out-of-service host must not be probed")

    host_record, lanes, _model_servers = collect_lab.collect_host(
        host,
        sources,
        registry_root=tmp_path,
        now="2026-09-28T13:00:00Z",
        out_of_service_reason="RULING 2026-01-01T00:00:00Z: test",
        runner=boom,
        holds_runner=no_holds_runner,
        planner_run=no_planner,
    )

    assert host_record.out_of_service_reason == "RULING 2026-01-01T00:00:00Z: test"
    assert all(lane.status is LaneStatus.OUT_OF_SERVICE for lane in lanes)


# --------------------------------------------------------------------------
# sources.yaml loads and the checked-in file never names a person or a
# hardcoded ssh user


def test_sources_yaml_loads_and_declares_no_ssh_user() -> None:
    sources = collect_lab.load_sources()
    assert sources.orchestrator_host == "lab-200"
    assert "lab-200" not in sources.hosts, "the orchestrator host runs no Docker engine"
    assert not any(host.local for host in sources.hosts.values()), (
        "no source reads a local docker"
    )
    for host_id, host in sources.hosts.items():
        if host.ssh_target:
            assert "@" not in host.ssh_target, (
                f"{host_id} embeds an ssh user in sources.yaml"
            )


def test_static_facts_out_of_service_hosts_is_a_list() -> None:
    from omnimarket.nodes.node_capability_catalog_effect.handlers import (
        handler_capability_catalog_render as render,
    )

    facts = render.load_static_facts()
    assert isinstance(facts.get("out_of_service_hosts"), list)
