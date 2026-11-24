# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install, inspect and read back Prometheus, Grafana and the host exporter on declared lab hosts.

The settings (an overlay) declare the targets, the images, the addresses, the ports, the thresholds and the
retention. The ``lab_hosts`` table says which hosts exist. This handler renders the files, writes them on the
observer host and installs each container through the container unit installer node (run as a command, never
imported), so no container is started by hand. ``readback`` asks Prometheus for every ``lab_hosts`` row.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostsTable
from omnimarket.nodes.node_host_observability_install_effect.handlers.render_observability import (
    PROMETHEUS_CONFIG,
    PROMETHEUS_RULES,
    render_container_units_overlay,
    render_files,
    units_for_host,
)
from omnimarket.nodes.node_host_observability_install_effect.models.model_observability import (
    ModelObservabilityInstallRequest,
    ModelObservabilityInstallResult,
    ModelObservabilityOverlay,
)

NODE_NAME = "node_host_observability_install_effect"
OVERLAY_ENV = "ONEX_HOST_OBSERVABILITY_OVERLAY"
CONTAINER_UNITS_MODULE = "omnimarket.nodes.node_host_installer_container_units"
UNITS_OVERLAY_NAME = "container-units.overlay.yaml"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
HttpGet = Callable[[str], str]
HostsReader = Callable[[str], ModelLabHostsTable]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a host command; a missing program reads as exit status 127."""
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return str(response.read().decode("utf-8"))


def read_hosts(hosts_file: str) -> ModelLabHostsTable:
    """The host table: a pinned file when given, else the table the settings overlay declares."""
    if hosts_file:
        document = yaml.safe_load(Path(hosts_file).read_text(encoding="utf-8"))
        return ModelLabHostsTable.model_validate(document)
    return resolve_lab_hosts_table()


class HandlerObservabilityInstall:
    """One request, one result; the runner, the HTTP read, the host table reader and the interpreter are injected."""

    def __init__(
        self,
        *,
        run: Runner = run_command,
        get: HttpGet = http_get,
        hosts: HostsReader = read_hosts,
        python: str = sys.executable,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.run = run
        self.get = get
        self.hosts = hosts
        self.python = python
        self.environ = os.environ if environ is None else environ

    def handle(
        self, request: ModelObservabilityInstallRequest
    ) -> ModelObservabilityInstallResult:
        try:
            overlay = self._overlay(request.overlay_file)
            table = self.hosts(request.hosts_file)
        except (NodeOverlayError, ValueError, OSError, yaml.YAMLError) as exc:
            return ModelObservabilityInstallResult(
                exit_code=3, stderr=f"INDETERMINATE: {type(exc).__name__}: {exc}\n"
            )
        names = [row.name for row in table.hosts]
        unknown = sorted(set(overlay.targets) - set(names))
        if unknown:
            return ModelObservabilityInstallResult(
                exit_code=1,
                stderr=f"targets {unknown} are not rows of the lab_hosts table; nothing done\n",
            )
        uncovered = tuple(name for name in names if name not in overlay.targets)
        if request.mode == "readback":
            return self._readback(overlay, names, uncovered)
        units = units_for_host(overlay, request.host_name)
        if not units:
            return ModelObservabilityInstallResult(
                exit_code=1,
                stderr=f"{request.host_name} takes no unit of the observability overlay\n",
                uncovered_hosts=uncovered,
            )
        if request.mode == "dry_run":
            return self._dry_run(overlay, request.host_name, uncovered)
        if request.mode == "status":
            return self._each_unit(
                overlay, request.host_name, units, "status", uncovered
            )
        return self._install(overlay, request.host_name, units, uncovered)

    def _overlay(self, overlay_file: str) -> ModelObservabilityOverlay:
        if overlay_file:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
            return load_structured_overlay(
                NODE_NAME, ModelObservabilityOverlay, explicit=document
            )
        return load_structured_overlay(
            NODE_NAME,
            ModelObservabilityOverlay,
            self.environ,
            file_env=OVERLAY_ENV,
        )

    def _dry_run(
        self, overlay: ModelObservabilityOverlay, host: str, uncovered: tuple[str, ...]
    ) -> ModelObservabilityInstallResult:
        out = ""
        if host == overlay.observer_host:
            for relative, text in render_files(overlay).items():
                out += f"--- {overlay.config_root}/{relative}\n{text}"
        out += f"--- {UNITS_OVERLAY_NAME}\n" + yaml.safe_dump(
            render_container_units_overlay(overlay, host), sort_keys=False
        )
        return ModelObservabilityInstallResult(
            exit_code=0, stdout=out, uncovered_hosts=uncovered
        )

    def _install(
        self,
        overlay: ModelObservabilityOverlay,
        host: str,
        units: list[str],
        uncovered: tuple[str, ...],
    ) -> ModelObservabilityInstallResult:
        root = Path(overlay.config_root)
        out = ""
        changed_prometheus = False
        target = overlay.targets.get(host)
        try:
            self._mkdir(root)
            if target is not None and target.exporter == "install":
                self._mkdir(Path(overlay.exporter.textfile_directory))
            if host == overlay.observer_host:
                for relative, text in render_files(overlay).items():
                    wrote = self._write(root / relative, text)
                    out += f"{'wrote' if wrote else 'unchanged'} {root / relative}\n"
                    if wrote and relative in (PROMETHEUS_CONFIG, PROMETHEUS_RULES):
                        changed_prometheus = True
            units_overlay = root / UNITS_OVERLAY_NAME
            self._write(
                units_overlay,
                yaml.safe_dump(
                    render_container_units_overlay(overlay, host), sort_keys=False
                ),
            )
        except OSError as exc:
            return ModelObservabilityInstallResult(
                exit_code=1,
                stdout=out,
                stderr=f"cannot write under {root}: {type(exc).__name__}\n",
                uncovered_hosts=uncovered,
            )
        result = self._each_unit(overlay, host, units, "install", uncovered, prefix=out)
        if result.exit_code == 0 and changed_prometheus:
            reload_ = self.run(
                [
                    overlay.docker_binary,
                    "kill",
                    "--signal=HUP",
                    overlay.prometheus.container,
                ]
            )
            note = (
                f"reloaded {overlay.prometheus.container}\n"
                if reload_.returncode == 0
                else f"reload of {overlay.prometheus.container} failed (exit {reload_.returncode}): {reload_.stderr}\n"
            )
            return ModelObservabilityInstallResult(
                exit_code=result.exit_code or reload_.returncode,
                stdout=result.stdout + note,
                stderr=result.stderr,
                uncovered_hosts=uncovered,
            )
        return result

    def _each_unit(
        self,
        overlay: ModelObservabilityOverlay,
        host: str,
        units: list[str],
        verb: str,
        uncovered: tuple[str, ...],
        prefix: str = "",
    ) -> ModelObservabilityInstallResult:
        units_overlay = Path(overlay.config_root) / UNITS_OVERLAY_NAME
        out, err, code = prefix, "", 0
        for unit in units:
            done = self.run(
                [
                    self.python,
                    "-m",
                    CONTAINER_UNITS_MODULE,
                    verb,
                    "--host",
                    host,
                    "--unit",
                    unit,
                    "--overlay",
                    str(units_overlay),
                ]
            )
            out += f"== {verb} {unit} exit {done.returncode}\n{done.stdout}"
            err += done.stderr
            code = code or done.returncode
            if done.returncode and verb == "install":
                break
        return ModelObservabilityInstallResult(
            exit_code=code, stdout=out, stderr=err, uncovered_hosts=uncovered
        )

    def _readback(
        self,
        overlay: ModelObservabilityOverlay,
        names: list[str],
        uncovered: tuple[str, ...],
    ) -> ModelObservabilityInstallResult:
        prom = overlay.prometheus
        base = f"http://{prom.address}:{prom.port}/api/v1/query"
        window = f"{overlay.grafana.reading_window_minutes}m"
        try:
            up = self._series(base, f'up{{job="{prom.job_name}"}}')
            age = self._series(
                base, f"time() - last_over_time(node_time_seconds[{window}])"
            )
        except (OSError, ValueError, KeyError) as exc:
            return ModelObservabilityInstallResult(
                exit_code=3,
                stderr=f"INDETERMINATE: Prometheus at {prom.address}:{prom.port} unread ({type(exc).__name__})\n",
                uncovered_hosts=uncovered,
            )
        lines: list[str] = []
        good = 0
        for name in names:
            if name in uncovered:
                lines.append(f"NO TARGET {name}")
                continue
            is_up, seconds = up.get(name), age.get(name)
            if is_up != 1.0 or seconds is None:
                lines.append(f"DOWN {name} up={is_up} age={seconds}")
            elif seconds > overlay.max_reading_age_seconds:
                lines.append(f"STALE {name} age={seconds:.0f}s")
            else:
                good += 1
                lines.append(f"READ {name} up=1 age={seconds:.0f}s")
        lines.append(
            f"COVERED {good} of {len(names)} lab_hosts rows "
            f"(reading at most {overlay.max_reading_age_seconds} s old)"
        )
        return ModelObservabilityInstallResult(
            exit_code=0 if good == len(names) else 1,
            stdout="\n".join(lines) + "\n",
            uncovered_hosts=uncovered,
        )

    def _series(self, base: str, query: str) -> dict[str, float]:
        document = json.loads(
            self.get(f"{base}?{urllib.parse.urlencode({'query': query})}")
        )
        if document["status"] != "success":
            raise ValueError("query did not succeed")
        return {
            row["metric"]["host"]: float(row["value"][1])
            for row in document["data"]["result"]
            if "host" in row["metric"]
        }

    @staticmethod
    def _mkdir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)

    def _write(self, path: Path, text: str) -> bool:
        """Write ``text`` atomically when it differs; True when the file changed."""
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
        self._mkdir(path.parent)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(path)
        return True
