# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The omnidash host node: newest-branch choice, build and health refusals, status file, idempotent install.

The in-memory profile: every side effect of a tick sits behind the handler's ports, so the decision
logic runs against fakes. The node publishes and subscribes to no topic, so there is no bus profile.
The hosts, addresses and ports here are synthetic fixtures; a grep test holds the package to naming none.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_omnidash_host import (
    ModelOmnidashBranchTip,
    ModelOmnidashHostOverlay,
    ModelOmnidashHostRow,
    ModelOmnidashHostSchedule,
    ModelOmnidashHostUpdateCommand,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_omnidash_host_effect.handlers import (
    adapter_omnidash_host_install as install_module,
)
from omnimarket.nodes.node_omnidash_host_effect.handlers.adapter_omnidash_host_install import (
    OmnidashHostInstaller,
    host_gaps,
    interval_of,
    load_schedule,
    provision_node,
    render_units,
)
from omnimarket.nodes.node_omnidash_host_effect.handlers.adapter_omnidash_host_system import (
    SystemOmnidashHostPorts,
)
from omnimarket.nodes.node_omnidash_host_effect.handlers.handler_omnidash_host_effect import (
    HandlerOmnidashHostEffect,
    OmnidashPollError,
    newest_branch,
    read_status,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(install_module.__file__).resolve().parents[1]
MODEL_FILE = Path(__file__).resolve().parents[1] / (
    "src/omnimarket/models/model_omnidash_host.py"
)
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def row(tmp_path: Path, **overrides: object) -> ModelOmnidashHostRow:
    values: dict[str, object] = {
        "name": "host-a",
        "ssh_target": "host-a",
        "bind_address": "192.0.2.10",
        "port": 18080,
        "scratch_port": 18081,
        "serve_url": "http://host-a.example.test:18080/",
        "repo_url": "https://example.test/org/dashboard.git",
        "build_root": str(tmp_path / "root"),
        "node_version": "22.13.0",
        "tools_root": str(tmp_path / "tools"),
        "node_dist_base": "https://example.test/node-dist",
        "status_path": str(tmp_path / "status.json"),
        "bus_lane": "dev",
        "projection_api_url": "http://192.0.2.20:3002",
        "projection_tenant_id": "00000000-0000-0000-0000-000000000000",
        "python": "/opt/venv/bin/python",
    }
    values.update(overrides)
    return ModelOmnidashHostRow.model_validate(values)


def tip(branch: str, sha: str, at: int) -> ModelOmnidashBranchTip:
    return ModelOmnidashBranchTip(branch=branch, sha=sha, committed_at=at)


class FakePorts:
    def __init__(
        self,
        tips: Sequence[ModelOmnidashBranchTip],
        *,
        build_fails: set[str] | None = None,
        health_fails: set[str] | None = None,
        switch_fails: set[str] | None = None,
        poll_error: str | None = None,
    ) -> None:
        self.tips = list(tips)
        self.build_fails = build_fails or set()
        self.health_fails = health_fails or set()
        self.switch_fails = switch_fails or set()
        self.poll_error = poll_error
        self.calls: list[tuple[str, str]] = []
        self.serving: str | None = None
        self.pruned: frozenset[str] = frozenset()

    def list_branches(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule
    ) -> list[ModelOmnidashBranchTip]:
        if self.poll_error:
            raise OmnidashPollError(self.poll_error)
        return list(self.tips)

    def resolve_commit(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        sha: str,
    ) -> ModelOmnidashBranchTip | None:
        return next((t for t in self.tips if t.sha == sha), None)

    def build(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule, sha: str
    ) -> str | None:
        self.calls.append(("build", sha))
        return "npm run build failed (exit 2)" if sha in self.build_fails else None

    def check_health(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule, sha: str
    ) -> str | None:
        self.calls.append(("health", sha))
        return (
            "health check answered 500 within 90s" if sha in self.health_fails else None
        )

    def switch(
        self, host: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule, sha: str
    ) -> str | None:
        self.calls.append(("switch", sha))
        if sha in self.switch_fails:
            return "switch: restart failed"
        self.serving = sha
        return None

    def prune(
        self,
        host: ModelOmnidashHostRow,
        schedule: ModelOmnidashHostSchedule,
        keep: frozenset[str],
    ) -> None:
        self.pruned = keep


def moment(minute: int) -> datetime:
    return datetime(2026, 11, 2, 12, minute, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.minute = 0

    def __call__(self) -> datetime:
        self.minute += 1
        return moment(self.minute)


def tick(
    tmp_path: Path, ports: FakePorts, clock: Clock, *, pin: str | None = None
) -> tuple[str, Path]:
    host = row(tmp_path)
    result = HandlerOmnidashHostEffect(ports, clock=clock).handle(
        ModelOmnidashHostUpdateCommand(host=host, schedule=load_schedule(), pin_sha=pin)
    )
    return result.outcome, Path(host.status_path)


# -- newest branch -----------------------------------------------------------------------------
def test_omnidash_host_newest_branch_is_by_committer_date_not_name_or_order() -> None:
    tips = [tip("zeta", SHA_A, 100), tip("alpha", SHA_B, 300), tip("dev", SHA_C, 200)]
    chosen = newest_branch(tips)
    assert chosen is not None
    assert chosen.branch == "alpha"
    assert newest_branch([]) is None


def test_omnidash_host_result_status_equals_the_status_file_record(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100), tip("feature", SHA_B, 200)])
    host = row(tmp_path)
    result = HandlerOmnidashHostEffect(ports, clock=Clock()).handle(
        ModelOmnidashHostUpdateCommand(host=host, schedule=load_schedule())
    )
    assert result.status == read_status(Path(host.status_path))
    assert result.status.served_sha == SHA_B


def test_omnidash_host_tick_serves_the_newest_branch_and_writes_status(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100), tip("feature", SHA_B, 200)])
    outcome, status_path = tick(tmp_path, ports, Clock())
    assert outcome == "switched"
    assert ports.calls == [("build", SHA_B), ("health", SHA_B), ("switch", SHA_B)]
    status = read_status(status_path)
    assert status.served_branch == "feature"
    assert status.served_sha == SHA_B
    assert status.served_committed_at == 200
    assert status.switched_at is not None
    assert status.last_poll_at is not None
    assert status.last_result == "switched"
    assert status.refusal_reason is None
    document = yaml.safe_load(status_path.read_text(encoding="utf-8"))
    assert {
        "served_branch",
        "served_sha",
        "switched_at",
        "last_poll_at",
        "last_result",
        "refusal_reason",
    } <= set(document)


def test_omnidash_host_unchanged_tip_builds_nothing(tmp_path: Path) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100)])
    clock = Clock()
    tick(tmp_path, ports, clock)
    ports.calls.clear()
    outcome, status_path = tick(tmp_path, ports, clock)
    assert outcome == "unchanged"
    assert ports.calls == []
    assert read_status(status_path).served_sha == SHA_A


def test_omnidash_host_newer_commit_after_serving_switches_and_moves_the_times(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100)])
    clock = Clock()
    tick(tmp_path, ports, clock)
    first = read_status(tmp_path / "status.json")
    ports.tips = [tip("dev", SHA_A, 100), tip("dev2", SHA_B, 400)]
    outcome, status_path = tick(tmp_path, ports, clock)
    second = read_status(status_path)
    assert outcome == "switched"
    assert (first.served_sha, second.served_sha) == (SHA_A, SHA_B)
    assert second.switched_at is not None
    assert first.switched_at is not None
    assert second.switched_at > first.switched_at
    assert ports.pruned == frozenset({SHA_A, SHA_B})


# -- refusals keep the last good build ---------------------------------------------------------
def test_omnidash_host_failed_build_keeps_the_old_build_serving(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100)])
    clock = Clock()
    tick(tmp_path, ports, clock)
    ports.tips = [tip("dev", SHA_A, 100), tip("bad", SHA_B, 500)]
    ports.build_fails = {SHA_B}
    outcome, status_path = tick(tmp_path, ports, clock)
    status = read_status(status_path)
    assert outcome == "refused-build"
    assert ports.serving == SHA_A
    assert ("switch", SHA_B) not in ports.calls
    assert ("health", SHA_B) not in ports.calls
    assert status.served_sha == SHA_A
    assert status.newest_branch == "bad"
    assert status.newest_sha == SHA_B
    assert status.refusal_reason == "npm run build failed (exit 2)"
    assert status.last_result == "refused-build"


def test_omnidash_host_failed_health_keeps_the_old_build_serving(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100)])
    clock = Clock()
    tick(tmp_path, ports, clock)
    ports.tips = [tip("dev", SHA_A, 100), tip("sick", SHA_B, 500)]
    ports.health_fails = {SHA_B}
    outcome, status_path = tick(tmp_path, ports, clock)
    status = read_status(status_path)
    assert outcome == "refused-health"
    assert ports.serving == SHA_A
    assert ("switch", SHA_B) not in ports.calls
    assert status.served_sha == SHA_A
    assert status.refusal_reason is not None
    assert "500" in status.refusal_reason


def test_omnidash_host_failed_switch_keeps_the_old_build_in_the_status(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100)])
    clock = Clock()
    tick(tmp_path, ports, clock)
    ports.tips = [tip("new", SHA_B, 500)]
    ports.switch_fails = {SHA_B}
    outcome, status_path = tick(tmp_path, ports, clock)
    assert outcome == "refused-health"
    assert read_status(status_path).served_sha == SHA_A


def test_omnidash_host_a_refused_sha_is_not_rebuilt_until_the_tip_moves(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("bad", SHA_B, 500)], build_fails={SHA_B})
    clock = Clock()
    tick(tmp_path, ports, clock)
    ports.calls.clear()
    outcome, status_path = tick(tmp_path, ports, clock)
    assert outcome == "refused-already"
    assert ports.calls == []
    assert read_status(status_path).refusal_reason == "npm run build failed (exit 2)"
    ports.tips = [tip("bad", SHA_C, 600)]
    ports.build_fails = set()
    outcome, status_path = tick(tmp_path, ports, clock)
    assert outcome == "switched"
    status = read_status(status_path)
    assert status.refusal_reason is None
    assert status.refused_sha is None


def test_omnidash_host_poll_failure_keeps_what_is_served(tmp_path: Path) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100)])
    clock = Clock()
    tick(tmp_path, ports, clock)
    ports.poll_error = "fetch failed: network is unreachable"
    outcome, status_path = tick(tmp_path, ports, clock)
    status = read_status(status_path)
    assert outcome == "poll-failed"
    assert status.served_sha == SHA_A
    assert status.refusal_reason == "fetch failed: network is unreachable"


def test_omnidash_host_no_branches_changes_nothing_served(tmp_path: Path) -> None:
    outcome, status_path = tick(tmp_path, FakePorts([]), Clock())
    assert outcome == "no-branches"
    assert read_status(status_path).served_sha is None


def test_omnidash_host_pinned_commit_is_served_then_the_tip_takes_over(
    tmp_path: Path,
) -> None:
    ports = FakePorts([tip("dev", SHA_A, 100), tip("dev", SHA_B, 200)])
    clock = Clock()
    outcome, status_path = tick(tmp_path, ports, clock, pin=SHA_A)
    assert outcome == "switched"
    assert read_status(status_path).served_sha == SHA_A
    outcome, status_path = tick(tmp_path, ports, clock)
    assert outcome == "switched"
    assert read_status(status_path).served_sha == SHA_B


# -- units and idempotent install --------------------------------------------------------------
def test_omnidash_host_units_bind_one_address_and_read_the_interval(
    tmp_path: Path,
) -> None:
    schedule = load_schedule()
    host = row(tmp_path)
    service, updater, timer = render_units(
        schedule, host, overlay_path=tmp_path / "overlay.yaml", home=tmp_path
    )
    assert "--host 192.0.2.10 --port 18080 --strictPort" in service
    assert "0.0.0.0" not in service
    assert f"OnUnitActiveSec={schedule.poll_interval_seconds}s" in timer
    assert "update --host host-a" in updater
    faster = row(tmp_path, poll_interval_seconds=7)
    assert interval_of(faster, schedule) == 7
    assert (
        "OnUnitActiveSec=7s"
        in render_units(
            schedule, faster, overlay_path=tmp_path / "overlay.yaml", home=tmp_path
        )[2]
    )


def test_omnidash_host_wildcard_bind_and_duplicate_rows_are_gaps(
    tmp_path: Path,
) -> None:
    overlay = ModelOmnidashHostOverlay(
        hosts=(row(tmp_path, bind_address="0.0.0.0"), row(tmp_path))
    )
    gaps = host_gaps(overlay)
    assert any("not a single interface address" in gap for gap in gaps)
    assert any("declared more than once" in gap for gap in gaps)
    table = ModelLabHostsTable(hosts=(ModelLabHostRow(name="other", target="other"),))
    assert any(
        "lab host table does not have" in gap
        for gap in host_gaps(ModelOmnidashHostOverlay(hosts=(row(tmp_path),)), table)
    )


def node_tarball(version: str) -> tuple[bytes, str]:
    import hashlib
    import platform

    arch = {"x86_64": "x64", "aarch64": "arm64"}[platform.machine()]
    name = f"node-v{version}-linux-{arch}"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        payload = b"#!/bin/sh\n"
        info = tarfile.TarInfo(f"{name}/bin/node")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    blob = buffer.getvalue()
    sums = f"{hashlib.sha256(blob).hexdigest()}  {name}.tar.xz\n"
    return blob, sums


class FakeSystemd:
    """Remembers enabled units, as systemd would across two runs."""

    def __init__(self) -> None:
        self.enabled: set[str] = set()
        self.active: set[str] = set()
        self.reloads = 0
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        self.commands.append(argv)
        if argv[0] == "loginctl":
            return subprocess.CompletedProcess(argv, 0, "Linger=yes\n", "")
        verb, unit = argv[2], argv[-1]
        if verb == "daemon-reload":
            self.reloads += 1
        elif verb == "is-enabled":
            return subprocess.CompletedProcess(
                argv,
                0 if unit in self.enabled else 1,
                "enabled\n" if unit in self.enabled else "disabled\n",
                "",
            )
        elif verb == "is-active":
            return subprocess.CompletedProcess(
                argv, 0, "active\n" if unit in self.active else "inactive\n", ""
            )
        elif verb == "enable":
            self.enabled.add(unit)
            if "--now" in argv:
                self.active.add(unit)
        return subprocess.CompletedProcess(argv, 0, "", "")


def installer(tmp_path: Path, systemd: FakeSystemd) -> OmnidashHostInstaller:
    blob, sums = node_tarball("22.13.0")

    def fetch(url: str) -> bytes:
        return sums.encode() if url.endswith("SHASUMS256.txt") else blob

    return OmnidashHostInstaller(
        schedule=load_schedule(),
        overlay=ModelOmnidashHostOverlay(hosts=(row(tmp_path),)),
        home=tmp_path / "home",
        overlay_path=tmp_path / "overlay.yaml",
        run=systemd,
        fetch=fetch,
        user="tester",
    )


def test_omnidash_host_install_twice_the_second_run_changes_nothing(
    tmp_path: Path,
) -> None:
    systemd = FakeSystemd()
    first = installer(tmp_path, systemd).install("host-a")
    assert first.exit_code == 0
    assert first.changed
    assert "install host-a: changed" in first.stdout
    units = tmp_path / "home/.config/systemd/user"
    assert sorted(p.name for p in units.iterdir()) == [
        "omnidash-host-update.service",
        "omnidash-host-update.timer",
        "omnidash-host.service",
    ]
    reloads = systemd.reloads
    before = len(systemd.commands)
    second = installer(tmp_path, systemd).install("host-a")
    assert second.exit_code == 0
    assert not second.changed
    assert "install host-a: no change" in second.stdout
    assert systemd.reloads == reloads
    assert not any(c[2:3] == ["enable"] for c in systemd.commands[before:])


def test_omnidash_host_install_refuses_an_undeclared_host_and_missing_linger(
    tmp_path: Path,
) -> None:
    systemd = FakeSystemd()
    result = installer(tmp_path, systemd).install("elsewhere")
    assert result.exit_code == 1
    assert "not in the node_omnidash_host_effect overlay" in result.stderr

    def no_linger(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 0, "Linger=no\n", "")

    target = installer(tmp_path, systemd)
    target.run = no_linger
    refused = target.install("host-a")
    assert refused.exit_code == 1
    assert "enable-linger" in refused.stderr


def test_omnidash_host_node_provision_checks_the_published_checksum(
    tmp_path: Path,
) -> None:
    blob, sums = node_tarball("22.13.0")
    host = row(tmp_path)

    def tampered(url: str) -> bytes:
        return sums.encode() if url.endswith("SHASUMS256.txt") else blob + b"x"

    with pytest.raises(ValueError, match="checksum"):
        provision_node(host, load_schedule(), tampered)
    assert not (Path(host.tools_root)).exists()


# -- real git, no network ----------------------------------------------------------------------
def git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**scrub_git_location_env(os.environ), **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_omnidash_host_real_git_lists_tips_newest_committer_date_first(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet", "-b", "dev")
    (origin / "f").write_text("1")
    git(origin, "add", "f")
    git(origin, "commit", "--quiet", "-m", "one", date="2026-10-01T00:00:00Z")
    git(origin, "checkout", "--quiet", "-b", "zzz-old")
    git(origin, "checkout", "--quiet", "-b", "aaa-new")
    (origin / "f").write_text("2")
    git(origin, "commit", "--quiet", "-am", "two", date="2026-11-01T00:00:00Z")
    newest_sha = git(origin, "rev-parse", "HEAD")
    host = row(tmp_path, repo_url=str(origin))
    tips = SystemOmnidashHostPorts().list_branches(host, load_schedule())
    chosen = newest_branch(tips)
    assert chosen is not None
    assert (chosen.branch, chosen.sha) == ("aaa-new", newest_sha)
    assert {t.branch for t in tips} == {"dev", "zzz-old", "aaa-new"}
    pinned = SystemOmnidashHostPorts().resolve_commit(host, load_schedule(), newest_sha)
    assert pinned is not None
    assert pinned.branch == "aaa-new"
    missing = host.model_copy(update={"repo_url": str(tmp_path / "nowhere")})
    with pytest.raises(OmnidashPollError):
        SystemOmnidashHostPorts().list_branches(missing, load_schedule())


# -- the package names no host and holds no interval literal -------------------------------------
HOST_FACT_PATTERNS = (
    r"\bh0\d\d\b",
    r"hp-gen\d",
    r"\.ts\.net",
    r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    r"\b8088\b",
)


def literal_offenders(files: Sequence[Path], patterns: Sequence[str]) -> list[str]:
    found = []
    for file in files:
        for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in patterns:
                if re.search(pattern, line):
                    found.append(f"{file.name}:{number}: {pattern}")
    return found


def package_code() -> list[Path]:
    return [*NODE_DIR.rglob("*.py"), MODEL_FILE, *NODE_DIR.glob("metadata.yaml")]


def test_omnidash_host_package_names_no_host_address_port_or_interval() -> None:
    interval = load_schedule().poll_interval_seconds
    patterns = (*HOST_FACT_PATTERNS, rf"\b{interval}\b")
    assert literal_offenders(package_code(), patterns) == []


def test_omnidash_host_literal_scan_catches_a_planted_literal(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(
        f"BIND = '{'.'.join(('100', '64', '0', '9'))}'\nINTERVAL = 120\nHOST = 'hp-gen{8}'\n",
        encoding="utf-8",
    )
    found = literal_offenders([planted], (*HOST_FACT_PATTERNS, r"\b120\b"))
    assert len(found) == 3


def test_omnidash_host_example_overlay_validates_and_uses_documentation_addresses() -> (
    None
):
    document = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    overlay = ModelOmnidashHostOverlay.model_validate(document)
    assert overlay.hosts[0].name == "host-a"
    assert host_gaps(overlay) == ()
    assert overlay.hosts[0].bind_address.startswith("192.0.2.")


def test_omnidash_host_contract_owns_the_interval_default() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["schedule"]["poll_interval_seconds"] > 0
    assert list(contract["schedule"]["schedulers"]) == ["systemd-user"]
