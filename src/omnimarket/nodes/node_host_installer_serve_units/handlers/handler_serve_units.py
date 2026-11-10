# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the serve and clone-refresh serve user units of a Linux pool host (OMN-23365).

Moved from the private host installer's ``lab-work-serve-systemd`` unit. The unit structure is declared in this node's
contract and its private names in this node's overlay; the host's values (bus lane, rank penalty, unit slots, declared
clones) are its row of the lab_hosts overlay, and ``ONEX_SKILL_OVERLAY_ROOTS`` for the serve comes from this node's
overlay, so a serve started from a registry root reads the overlays (the host_capacity overlay among them) a lane reads.
"""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_host_installer_serve_units.models.model_serve_units import (
    ModelServeUnitsOverlay,
    ModelServeUnitsRequest,
    ModelServeUnitsResult,
    ModelUnitDeclaration,
)

NODE_DIR = Path(__file__).resolve().parents[1]
REGISTRY_ROOT_PREFIX = "$OMNI_HOME/"
SKILL_OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
LOG_TAIL_LINES = 20

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
HostsLoader = Callable[[], ModelLabHostsTable]
OverlayLoader = Callable[[], ModelServeUnitsOverlay]


def run_command(
    argv: Sequence[str], cwd: Path | None = None, unset: Sequence[str] = ()
) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if k not in unset}
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            cwd=cwd,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def load_node_overlay() -> ModelServeUnitsOverlay:
    """This node's overlay, found under ``ONEX_SKILL_OVERLAY_ROOTS``; there is no packaged fallback."""
    return load_structured_overlay(
        "node_host_installer_serve_units", ModelServeUnitsOverlay
    )


class RefusalError(Exception):
    """A request this node declines, with the exit status and the typed reason the result carries."""

    def __init__(self, code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


class FailureError(Exception):
    """A host command that failed after the node began to write; the result carries its status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    return dict(
        yaml.safe_load((contract_path or NODE_DIR / "contract.yaml").read_text("utf-8"))
    )


def expand(value: str, registry_root: str) -> str:
    """Expand the declared registry-root prefix; a systemd environment file has no shell."""
    if value.startswith(REGISTRY_ROOT_PREFIX):
        return f"{registry_root}/{value[len(REGISTRY_ROOT_PREFIX) :]}"
    return value


def _quoted_env_value(argv: str) -> str:
    return '"' + argv.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tail(path: Path, lines: int) -> str:
    return "".join(
        path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-lines:]
    )


class HandlerServeUnits:
    """Render and install the serve units; every host command goes through the injected runner."""

    def __init__(
        self,
        *,
        run: Runner = run_command,
        hosts: HostsLoader = resolve_lab_hosts_table,
        overlay: OverlayLoader = load_node_overlay,
        user: Callable[[], str] = getpass.getuser,
        contract_path: Path | None = None,
    ) -> None:
        self._run = run
        self._hosts = hosts
        self._overlay = overlay
        self._user = user
        self._contract = load_contract(contract_path)

    # -- entry --------------------------------------------------------------------------------

    def handle(self, request: ModelServeUnitsRequest) -> ModelServeUnitsResult:
        try:
            declared = self._overlay()
            if request.mode == "status":
                return self._status(request, declared)
            if request.mode == "uninstall":
                return self._uninstall(request, declared)
            return self._install(request, declared)
        except RefusalError as exc:
            return ModelServeUnitsResult(
                unit=request.unit,
                mode=request.mode,
                status="refused",
                exit_code=exc.code,
                reason=exc.reason,
                stderr=f"{exc}\n",
            )
        except FailureError as exc:
            return ModelServeUnitsResult(
                unit=request.unit,
                mode=request.mode,
                status="failure",
                exit_code=exc.code,
                reason="host-command-failed",
                stderr=f"{exc}\n",
                changed=True,
            )

    # -- declared pieces ----------------------------------------------------------------------

    @property
    def _unit(self) -> dict[str, Any]:
        return dict(self._contract["unit"])

    @staticmethod
    def _declared_units(
        declared: ModelServeUnitsOverlay,
    ) -> list[tuple[str, ModelUnitDeclaration]]:
        """The units in install order, each with the role that names its template."""
        return [("serve", declared.serve), ("clone_refresh", declared.clone_refresh)]

    def _template(self, role: str, entry: ModelUnitDeclaration) -> bytes:
        text = (NODE_DIR / str(self._unit["templates"][role])).read_text("utf-8")
        for placeholder, value in (
            ("__ENV_FILE__", entry.env_file),
            ("__LOG_DIRECTORY__", entry.log_directory),
            ("__SYSLOG_IDENTIFIER__", entry.name.removesuffix(".service")),
        ):
            text = text.replace(placeholder, value)
        return text.encode("utf-8")

    def _checked(self, argv: list[str]) -> str:
        done = self._run(argv)
        if done.returncode:
            raise FailureError(
                done.returncode,
                f"{' '.join(argv)} exited {done.returncode}: {done.stderr}",
            )
        return done.stdout or ""

    # -- host values --------------------------------------------------------------------------

    def _row(self, request: ModelServeUnitsRequest) -> tuple[ModelLabHostRow, str]:
        table = self._hosts()
        row = next((h for h in table.hosts if h.name == request.host_name), None)
        if row is None:
            raise RefusalError(
                2,
                "unknown-host",
                f"unknown host {request.host_name!r}; known hosts: "
                f"{', '.join(h.name for h in table.hosts)}",
            )
        if table.bus_lane is None:
            raise RefusalError(
                2, "no-bus-lane", "the lab_hosts overlay declares no bus_lane"
            )
        if row.local:
            raise RefusalError(
                64,
                "local-host",
                f"{row.name} is the launching host (local: true) and is served by launchd, not systemd",
            )
        return row, table.bus_lane

    def _roots(
        self, request: ModelServeUnitsRequest, declared: ModelServeUnitsOverlay
    ) -> str:
        roots = tuple(request.skill_overlay_roots) or tuple(
            expand(root, request.registry_root)
            for root in declared.skill_overlay_roots.get(request.host_name, ())
        )
        if not roots:
            raise RefusalError(
                66,
                "no-skill-overlay-roots",
                f"the node overlay declares no skill_overlay_roots for host {request.host_name}; "
                "add them there or name them in the request",
            )
        for root in roots:
            if os.pathsep in root:
                raise RefusalError(
                    65,
                    "skill-overlay-root-invalid",
                    f"{SKILL_OVERLAY_ROOTS_ENV} root {root} holds a path separator",
                )
            if not Path(root).is_dir():
                raise RefusalError(
                    66,
                    "skill-overlay-root-missing",
                    f"{SKILL_OVERLAY_ROOTS_ENV} root {root} is not a directory",
                )
        return os.pathsep.join(roots)

    def _lane_values(
        self,
        row: ModelLabHostRow,
        registry_root: str,
        declared: ModelServeUnitsOverlay,
    ) -> dict[str, str]:
        clones = {clone.repo: clone.path for clone in row.clones}
        wanted = declared.lane_environment
        lane_repos = {spec.repo for spec in wanted.values()}
        if not (row.available and lane_repos & clones.keys()):
            return {}
        gaps = [name for name, spec in wanted.items() if spec.repo not in clones]
        if gaps:
            raise RefusalError(
                2,
                "lane-clone-missing",
                f"lab host {row.name!r} takes lanes but declares no clone to derive "
                f"{', '.join(gaps)} from",
            )
        values: dict[str, str] = {}
        for name, spec in wanted.items():
            base = expand(clones[spec.repo], registry_root)
            values[name] = f"{base}/{spec.inside}" if spec.inside else base
        return values

    def _knowledge_base(
        self,
        request: ModelServeUnitsRequest,
        row: ModelLabHostRow,
        declared: ModelServeUnitsOverlay,
    ) -> str:
        path = request.knowledge_base_path or ""
        if not path:
            repo = declared.knowledge_base.repo
            path = next(
                (
                    expand(c.path, request.registry_root)
                    for c in row.clones
                    if c.repo == repo
                ),
                "",
            )
        if path and not (Path(path) / ".git").is_dir():
            raise RefusalError(
                66,
                "knowledge-base-not-a-clone",
                f"{declared.knowledge_base_variable}={path} is not a clone (no .git directory)",
            )
        return path

    # -- install ------------------------------------------------------------------------------

    def _install(
        self, request: ModelServeUnitsRequest, declared: ModelServeUnitsOverlay
    ) -> ModelServeUnitsResult:
        unit = self._unit
        python = f"{request.serve_root}/{unit['serve_python']}"
        if not os.access(python, os.X_OK):
            raise RefusalError(
                66, "serve-python-missing", f"{python} is not executable"
            )
        row, bus_lane = self._row(request)
        overlay_roots = self._roots(request, declared)
        for _role, entry in self._declared_units(declared):
            probe = self._run(
                [python, "-c", f"import {entry.import_probe}"],
                cwd=Path(request.serve_root) / str(unit["serve_cwd"]),
                unset=("PYTHONPATH",),
            )
            if probe.returncode:
                raise RefusalError(
                    66,
                    "import-probe-failed",
                    f"{python} cannot import {entry.import_probe} (update the serve root's omnimarket)",
                )
        kb_path = self._knowledge_base(request, row, declared)
        lane_values = self._lane_values(row, request.registry_root, declared)
        texts = self._render(
            request, declared, row, bus_lane, overlay_roots, kb_path, lane_values
        )
        if request.mode == "dry-run":
            return ModelServeUnitsResult(
                unit=request.unit,
                mode=request.mode,
                status="success",
                stdout="".join(texts.values()),
                environments=texts,
            )
        user = self._user()
        linger = self._run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
        if (linger.stdout or "").rstrip("\n") != "yes":
            raise RefusalError(
                69,
                "linger-off",
                f"linger is off; run: sudo loginctl enable-linger {user}",
            )
        return self._write(request, declared, texts)

    def _render(
        self,
        request: ModelServeUnitsRequest,
        declared: ModelServeUnitsOverlay,
        row: ModelLabHostRow,
        bus_lane: str,
        overlay_roots: str,
        kb_path: str,
        lane_values: dict[str, str],
    ) -> dict[str, str]:
        unit = self._unit
        path = str(unit["path"]).format(home=request.home)
        root = request.registry_root

        def header(entry: ModelUnitDeclaration) -> str:
            return str(unit["header"]).format(ticket=entry.ticket)

        lab = [
            f"OMNIBASE_PATH={request.serve_root}",
            f"LAB_WORK_HOST_NAME={row.name}",
            f"LAB_WORK_BUS_LANE={bus_lane}",
            f"LAB_WORK_RANK_PENALTY={row.penalty:g}",
        ]
        if row.unit_slots is not None:
            lab.append(f"LAB_WORK_MAX_UNITS={row.unit_slots}")
        if lane_values:
            lab.append(f"OMNI_HOME={root}")
            lab.extend(f"{key}={value}" for key, value in lane_values.items())
        lab.append(f"{SKILL_OVERLAY_ROOTS_ENV}={overlay_roots}")
        lab.append(f"LAB_WORK_ROOT={request.home}/{unit['work_directory']}")
        lab.append(f"PATH={path}")

        sync = _quoted_env_value(
            shlex.join(["python3", f"{root}/{unit['sync_engine']}", "sync", "--repo"])
            + " {repo}"
        )
        clone = [
            f"OMNIBASE_PATH={request.serve_root}",
            f"CLONE_REFRESH_HOST_NAME={row.name}",
            f"CLONE_REFRESH_BUS_LANE={bus_lane}",
            f"CLONE_REFRESH_SYNC_COMMAND={sync}",
            f"OMNI_HOME={root}",
            f"ONEX_STATE_DIR={root}/.onex_state",
        ]
        if kb_path:
            clone.append(f"{declared.knowledge_base_variable}={kb_path}")
        if "ONEX_WORKSPACE_CONFIG_ROOT" in lane_values:
            clone.append(
                f"ONEX_WORKSPACE_CONFIG_ROOT={lane_values['ONEX_WORKSPACE_CONFIG_ROOT']}"
            )
        clone.append(f"PATH={path}")
        return {
            Path(declared.serve.env_file).name: header(declared.serve)
            + "".join(f"{line}\n" for line in lab),
            Path(declared.clone_refresh.env_file).name: header(declared.clone_refresh)
            + "".join(f"{line}\n" for line in clone),
        }

    def _write(
        self,
        request: ModelServeUnitsRequest,
        declared: ModelServeUnitsOverlay,
        texts: dict[str, str],
    ) -> ModelServeUnitsResult:
        unit = self._unit
        home = Path(request.home)
        unit_dir = home / str(unit["unit_directory"])
        unit_dir.mkdir(parents=True, exist_ok=True)
        (home / str(unit["work_directory"])).mkdir(parents=True, exist_ok=True)
        changed = False
        for role, entry in self._declared_units(declared):
            env_file = home / entry.env_file
            env_file.parent.mkdir(parents=True, exist_ok=True)
            (home / entry.log_directory).mkdir(parents=True, exist_ok=True)
            text = texts[env_file.name]
            if not env_file.is_file() or env_file.read_text("utf-8") != text:
                env_file.write_text(text, encoding="utf-8")
                changed = True
            template = self._template(role, entry)
            destination = unit_dir / entry.name
            if not destination.is_file() or destination.read_bytes() != template:
                destination.write_bytes(template)
                destination.chmod(0o644)
                changed = True
        out = ""
        if changed:
            self._checked(["systemctl", "--user", "daemon-reload"])
        for _role, entry in self._declared_units(declared):
            self._checked(["systemctl", "--user", "enable", entry.name])
            active = self._run(["systemctl", "--user", "is-active", entry.name])
            if changed or (active.stdout or "").strip() != "active":
                self._checked(["systemctl", "--user", "restart", entry.name])
                changed = True
            out += self._checked(
                ["systemctl", "--user", "status", entry.name, "--no-pager"]
            )
        if not changed:
            out = "no change\n" + out
        return ModelServeUnitsResult(
            unit=request.unit,
            mode=request.mode,
            status="success",
            stdout=out,
            changed=changed,
            environments=texts,
        )

    # -- status and uninstall -----------------------------------------------------------------

    def _status(
        self, request: ModelServeUnitsRequest, declared: ModelServeUnitsOverlay
    ) -> ModelServeUnitsResult:
        home = Path(request.home)
        code, out = 0, ""
        for _role, entry in self._declared_units(declared):
            done = self._run(
                ["systemctl", "--user", "status", entry.name, "--no-pager"]
            )
            out += done.stdout or ""
            code = done.returncode or code
            log = home / entry.log_directory / "serve.log"
            if log.is_file():
                out += _tail(log, LOG_TAIL_LINES)
        return ModelServeUnitsResult(
            unit=request.unit,
            mode=request.mode,
            status="success" if not code else "failure",
            exit_code=code,
            stdout=out,
        )

    def _uninstall(
        self, request: ModelServeUnitsRequest, declared: ModelServeUnitsOverlay
    ) -> ModelServeUnitsResult:
        unit = self._unit
        home = Path(request.home)
        for _role, entry in self._declared_units(declared):
            self._run(["systemctl", "--user", "disable", "--now", entry.name])
        for _role, entry in self._declared_units(declared):
            (home / str(unit["unit_directory"]) / entry.name).unlink(missing_ok=True)
            (home / entry.env_file).unlink(missing_ok=True)
        self._checked(["systemctl", "--user", "daemon-reload"])
        return ModelServeUnitsResult(
            unit=request.unit, mode=request.mode, status="success", changed=True
        )
