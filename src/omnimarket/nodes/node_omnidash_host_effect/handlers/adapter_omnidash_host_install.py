# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render, check and install the omnidash host: a pinned node, one service unit, an updater and its timer.

``python -m omnimarket.nodes.node_omnidash_host_effect <verb>``: ``check`` fails for a duplicate or
wildcard-bound host row (and, with ``--against-lab-table``, a host the lab host table does not carry),
``install`` writes everything for the named host's overlay row and a second run changes nothing,
``update`` is the updater tick the timer runs, ``run-now`` starts the updater once and ``status``
prints the status file.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import re
import subprocess
import tarfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.model_omnidash_host import (
    ModelOmnidashHostInstallResult,
    ModelOmnidashHostOverlay,
    ModelOmnidashHostRow,
    ModelOmnidashHostSchedule,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable

from .adapter_omnidash_host_system import node_archive, node_bin, serve_hostname

NODE_NAME = "node_omnidash_host_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
OVERLAY_FILE_ENV = "ONEX_OMNIDASH_HOST_OVERLAY_FILE"
WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "[::]", "*", "localhost"})
_SAFE = re.compile(r"^[A-Za-z0-9_@+=:,./~-]+$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Fetcher = Callable[[str], bytes]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return bytes(response.read())


def load_schedule(path: Path = CONTRACT_PATH) -> ModelOmnidashHostSchedule:
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelOmnidashHostSchedule.model_validate(contract["schedule"])


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelOmnidashHostOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelOmnidashHostOverlay, environ, file_env=OVERLAY_FILE_ENV
    )


def overlay_file(environ: Mapping[str, str]) -> Path | None:
    pinned = environ.get(OVERLAY_FILE_ENV, "").strip()
    if pinned:
        return Path(pinned)
    return find_node_overlay(NODE_NAME, environ)


def interval_of(row: ModelOmnidashHostRow, schedule: ModelOmnidashHostSchedule) -> int:
    """The poll interval: the host row's value, else the contract's."""
    return row.poll_interval_seconds or schedule.poll_interval_seconds


def host_gaps(
    overlay: ModelOmnidashHostOverlay, lab_table: ModelLabHostsTable | None = None
) -> tuple[str, ...]:
    gaps: list[str] = []
    seen: set[str] = set()
    for row in overlay.hosts:
        if row.name in seen:
            gaps.append(f"{row.name}: declared more than once")
        seen.add(row.name)
        if row.bind_address in WILDCARD_BINDS:
            gaps.append(
                f"{row.name}: bind_address {row.bind_address!r} is not a single interface address"
            )
        if row.port == row.scratch_port:
            gaps.append(f"{row.name}: port and scratch_port are the same")
    if lab_table is not None:
        rows = {host.name: host for host in lab_table.hosts}
        for name in sorted(seen):
            entry = rows.get(name)
            if entry is None:
                gaps.append(f"{name}: names a host the lab host table does not have")
            elif not entry.available:
                gaps.append(f"{name}: the lab host table marks the host unavailable")
    return tuple(gaps)


def _safe(value: str, what: str) -> str:
    if not _SAFE.match(value):
        raise ValueError(f"{what} {value!r} holds a character a unit line cannot carry")
    return value


def _env(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"\n'


def render_units(
    schedule: ModelOmnidashHostSchedule,
    row: ModelOmnidashHostRow,
    *,
    overlay_path: Path,
    home: Path,
) -> tuple[str, str, str]:
    """The service, updater service and timer text for one host row."""
    root = _safe(row.build_root, "build_root")
    bin_dir = _safe(str(node_bin(row)), "node bin")
    python = _safe(row.python, "python")
    path = f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"
    hostname = serve_hostname(row)
    interval = interval_of(row, schedule)
    updater_timeout = (
        schedule.build_timeout_seconds * 2 + schedule.health_timeout_seconds
    )
    service = (
        "# Rendered by the omnidash host node (install); the node overlay owns every value.\n"
        "[Unit]\nDescription=OmniDash from the omnidash repository's own dev script, newest-branch build\n"
        f"ConditionPathExists={root}/{schedule.current_link}\n"
        "\n[Service]\nType=simple\n"
        f"WorkingDirectory={root}/{schedule.current_link}\n"
        + _env("HOME", str(home))
        + _env("PATH", path)
        + _env("__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS", hostname)
        + f"ExecStart={bin_dir}/npm run dev -- --host {_safe(row.bind_address, 'bind_address')} "
        f"--port {row.port} --strictPort\n"
        "Restart=on-failure\nRestartSec=10\n"
        f"SyslogIdentifier={schedule.service_unit}\n"
        "\n[Install]\nWantedBy=default.target\n"
    )
    updater_env = [
        _env("HOME", str(home)),
        _env("PATH", path),
        _env(OVERLAY_FILE_ENV, _safe(str(overlay_path), "overlay path")),
    ]
    if row.omnimarket_src:
        updater_env.append(
            _env("PYTHONPATH", _safe(row.omnimarket_src, "omnimarket_src"))
        )
    updater = (
        "# Rendered by the omnidash host node (install): one poll of the omnidash origin.\n"
        "[Unit]\nDescription=OmniDash updater: build the newest branch, switch only when healthy\n"
        "\n[Service]\nType=oneshot\n"
        f"TimeoutStartSec={updater_timeout}\n"
        + "".join(updater_env)
        + f"ExecStart={python} -m {NODE_MODULE} update --host {_safe(row.name, 'host name')}\n"
        f"SyslogIdentifier={schedule.updater_unit}\n"
    )
    timer = (
        f"# poll interval {interval}s\n"
        "[Unit]\nDescription=OmniDash updater poll\n"
        f"\n[Timer]\nOnActiveSec={schedule.first_run_delay_seconds}s\n"
        f"OnUnitActiveSec={interval}s\n"
        f"Unit={schedule.updater_unit}.service\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )
    return service, updater, timer


def provision_node(
    row: ModelOmnidashHostRow,
    schedule: ModelOmnidashHostSchedule,
    fetch: Fetcher = fetch_bytes,
) -> bool:
    """Unpack the pinned node under the tools root. Returns True when it installed something."""
    name = node_archive(row)
    target = Path(row.tools_root) / name
    if (target / "bin" / "node").is_file():
        return False
    base = f"{row.node_dist_base.rstrip('/')}/v{row.node_version}"
    archive = f"{name}.tar.xz"
    sums = fetch(f"{base}/SHASUMS256.txt").decode("utf-8")
    expected = next(
        (line.split()[0] for line in sums.splitlines() if line.endswith(f" {archive}")),
        "",
    )
    if not expected:
        raise ValueError(f"{archive} is not in the published checksums")
    blob = fetch(f"{base}/{archive}")
    if hashlib.sha256(blob).hexdigest() != expected:
        raise ValueError(f"{archive} does not match its published checksum")
    root = Path(row.tools_root)
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz") as tar:
        tar.extractall(root, filter="data")
    return True


class OmnidashHostInstaller:
    """Install the omnidash host on the machine that runs this process."""

    def __init__(
        self,
        *,
        schedule: ModelOmnidashHostSchedule,
        overlay: ModelOmnidashHostOverlay,
        home: Path,
        overlay_path: Path,
        run: Runner = run_command,
        fetch: Fetcher = fetch_bytes,
        user: str | None = None,
    ) -> None:
        self.schedule = schedule
        self.overlay = overlay
        self.home = home
        self.overlay_path = overlay_path
        self.run = run
        self.fetch = fetch
        self.user = user or getpass.getuser()
        self.unit_dir = home / ".config/systemd/user"

    def check(
        self, lab_table: ModelLabHostsTable | None = None
    ) -> ModelOmnidashHostInstallResult:
        gaps = host_gaps(self.overlay, lab_table)
        if gaps:
            return ModelOmnidashHostInstallResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        return ModelOmnidashHostInstallResult(
            exit_code=0, stdout=f"OK {len(self.overlay.hosts)} hosts\n"
        )

    def row(self, host: str) -> ModelOmnidashHostRow | ModelOmnidashHostInstallResult:
        gaps = host_gaps(self.overlay)
        if gaps:
            return ModelOmnidashHostInstallResult(
                exit_code=1, stderr="".join(f"GAP {gap}\n" for gap in gaps)
            )
        for candidate in self.overlay.hosts:
            if candidate.name == host:
                return candidate
        return ModelOmnidashHostInstallResult(
            exit_code=1,
            stderr=f"{host}: not in the {NODE_NAME} overlay, so omnidash is not declared for it\n",
        )

    def install(
        self, host: str, *, dry_run: bool = False
    ) -> ModelOmnidashHostInstallResult:
        found = self.row(host)
        if isinstance(found, ModelOmnidashHostInstallResult):
            return found
        row = found
        try:
            service, updater, timer = render_units(
                self.schedule, row, overlay_path=self.overlay_path, home=self.home
            )
        except ValueError as exc:
            return ModelOmnidashHostInstallResult(exit_code=1, stderr=f"{exc}\n")
        if dry_run:
            return ModelOmnidashHostInstallResult(
                exit_code=0, stdout=f"{service}\n{updater}\n{timer}"
            )
        linger = self.run(["loginctl", "show-user", self.user, "--property=Linger"])
        if "Linger=yes" not in linger.stdout:
            return ModelOmnidashHostInstallResult(
                exit_code=1,
                stderr=f"user {self.user} has no linger, so the units would stop at logout; "
                f"run: loginctl enable-linger {self.user}\n",
            )
        output = ""
        changed = False
        try:
            if provision_node(row, self.schedule, self.fetch):
                changed = True
                output += f"installed node {row.node_version} under {row.tools_root}\n"
            else:
                output += f"node {row.node_version} unchanged\n"
        except (ValueError, OSError) as exc:
            return ModelOmnidashHostInstallResult(
                exit_code=1, stderr=f"node provisioning failed: {exc}\n"
            )
        Path(row.build_root).mkdir(parents=True, exist_ok=True)
        Path(row.status_path).parent.mkdir(parents=True, exist_ok=True)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        units = (
            (f"{self.schedule.service_unit}.service", service),
            (f"{self.schedule.updater_unit}.service", updater),
            (f"{self.schedule.updater_unit}.timer", timer),
        )
        wrote = False
        for name, text in units:
            target = self.unit_dir / name
            if target.exists() and target.read_text(encoding="utf-8") == text:
                output += f"{name} unchanged\n"
            else:
                target.write_text(text, encoding="utf-8")
                output += f"wrote {target}\n"
                wrote = True
        if wrote:
            changed = True
            reloaded = self.run(["systemctl", "--user", "daemon-reload"])
            if reloaded.returncode:
                return ModelOmnidashHostInstallResult(
                    exit_code=reloaded.returncode,
                    stdout=output,
                    stderr=reloaded.stderr,
                    changed=True,
                )
        for unit, command in (
            (f"{self.schedule.service_unit}.service", ["enable"]),
            (f"{self.schedule.updater_unit}.timer", ["enable", "--now"]),
        ):
            enabled = self.run(["systemctl", "--user", "is-enabled", unit])
            active = (
                self.run(["systemctl", "--user", "is-active", unit]).stdout.strip()
                if unit.endswith(".timer")
                else "active"
            )
            if enabled.stdout.strip() == "enabled" and active == "active":
                output += f"{unit} enabled\n"
                continue
            done = self.run(["systemctl", "--user", *command, unit])
            changed = True
            if done.returncode:
                return ModelOmnidashHostInstallResult(
                    exit_code=done.returncode,
                    stdout=output,
                    stderr=done.stderr,
                    changed=True,
                )
            output += f"enabled {unit}\n"
        summary = "changed" if changed else "no change"
        return ModelOmnidashHostInstallResult(
            exit_code=0,
            stdout=output + f"install {host}: {summary}\n",
            changed=changed,
        )

    def run_now(self, host: str) -> ModelOmnidashHostInstallResult:
        found = self.row(host)
        if isinstance(found, ModelOmnidashHostInstallResult):
            return found
        done = self.run(
            ["systemctl", "--user", "start", f"{self.schedule.updater_unit}.service"]
        )
        return ModelOmnidashHostInstallResult(
            exit_code=done.returncode,
            stdout=done.stdout or f"ran {self.schedule.updater_unit}.service once\n",
            stderr=done.stderr,
        )


__all__ = [
    "OVERLAY_ROOTS_ENV",
    "OmnidashHostInstaller",
    "host_gaps",
    "interval_of",
    "load_overlay",
    "load_schedule",
    "node_archive",
    "overlay_file",
    "provision_node",
    "render_units",
]
