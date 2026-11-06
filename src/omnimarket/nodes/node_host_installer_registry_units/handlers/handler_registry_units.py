# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render and install launchd registry_module_tick units, with a project path override.

A registry_module_tick unit runs ``uv run --frozen --python <brew python> --project <project> python -m <module>`` on a timer. The project is the
canonical registry clone unless the unit contract declares ``project_path_override`` and the request names a checkout:
then the rendered unit runs that checkout, so a fix installs from its branch without waiting for a merge. The checkout
must be clean (the installed code equals its revision) and carry the unit's entry file. The revision the unit runs is
read from the checkout and written to the unit's install record.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
STALE_INTERVALS = 3
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def interpreter_present(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(LC_ALL="C", TZ="UTC")
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, env=env, check=False, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def load_units(contract_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ``units`` table of this node's contract."""
    document = yaml.safe_load(
        (contract_path or NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return {
        str(name): dict(unit) for name, unit in (document.get("units") or {}).items()
    }


class HandlerRegistryUnits:
    """``handle(request) -> result``. ``run`` runs one command; tests inject it."""

    def __init__(
        self, run: Runner = run_command, units: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.run = run
        self.units = units if units is not None else load_units()

    def handle(self, request: ModelRegistryUnitsRequest) -> ModelRegistryUnitsResult:
        unit = self.units.get(request.unit)
        if unit is None:
            return self.result(
                request, 2, error=f"{request.unit} is not a unit of this node\n"
            )
        try:
            return self.dispatch(request, unit)
        except (OSError, ValueError) as exc:
            return self.result(request, 1, error=f"{exc}\n")

    # ------------------------------------------------------------------ plan

    def interpreter_for(self, unit: dict[str, Any]) -> str:
        """The first declared interpreter that exists on this host; the unit is refused when none does."""
        candidates = [str(c) for c in unit.get("interpreter_candidates") or []]
        if not candidates:
            raise ValueError(f"{unit['label']} declares no interpreter_candidates")
        for candidate in candidates:
            if interpreter_present(candidate):
                return candidate
        raise ValueError(
            f"none of the interpreters {candidates} exists on this host; {unit['label']} needs the brew python "
            "(a uv-managed one never gets the macOS Local Network grant)"
        )

    def project_for(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> tuple[Path, bool]:
        """The project the unit runs and whether it is the canonical clone; the override is refused when undeclared."""
        canonical = Path(request.registry_root) / str(unit["registry_project"])
        if not request.project_path:
            return canonical, True
        if not unit.get("project_path_override"):
            raise ValueError(f"{request.unit} declares no project path override")
        project = Path(request.project_path)
        if not project.is_absolute():
            raise ValueError(
                f"project_path {request.project_path!r} is not an absolute path"
            )
        return project, project.resolve() == canonical.resolve()

    def checkout_revision(
        self, project: Path, unit: dict[str, Any], expected: str
    ) -> str:
        """The commit ``project`` is at; it must be a clean checkout that carries the entry file and is at ``expected``."""
        head = self.run(["git", "-C", str(project), "rev-parse", "HEAD"])
        if head.returncode:
            raise ValueError(f"{project} is not a git checkout: {head.stderr.strip()}")
        revision = head.stdout.strip()
        dirty = self.run(["git", "-C", str(project), "status", "--porcelain"])
        if dirty.returncode or dirty.stdout.strip():
            first = dirty.stdout.strip().splitlines()[:1] or [dirty.stderr.strip()]
            raise ValueError(
                f"{project} is not clean ({first[0]}); an installed checkout must equal its revision"
            )
        if expected and not revision.startswith(expected):
            raise ValueError(
                f"{project} is at {revision}, not the expected revision {expected}"
            )
        entry = project / str(unit["entry_file"])
        if not entry.is_file():
            raise ValueError(
                f"{entry} is absent: {project} does not carry {unit['module']}"
            )
        return revision

    def environment(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> dict[str, str]:
        declared = dict(unit.get("environment") or {})
        values = {"home": request.home, "registry_root": request.registry_root}
        if request.workspace_config_root:
            values["workspace_config_root"] = request.workspace_config_root
        for key in ("plans_path", "marketplace_root"):
            if getattr(request, key):
                values[key] = getattr(request, key)
        allowed = {str(n) for n in unit.get("environment_path_overrides") or []}
        for name in request.path_overrides:
            if name not in allowed:
                raise ValueError(
                    f"{name} is not a path override {request.unit} declares"
                )
        filled: dict[str, str] = {}
        for name, template in declared.items():
            if not NAME.match(str(name)):
                raise ValueError(f"invalid unit environment name {name!r}")
            text = str(template)
            for key, value in values.items():
                text = text.replace("{" + key + "}", value)
            left = PLACEHOLDER.search(text)
            if left is not None and name not in request.path_overrides:
                raise ValueError(
                    f"{name} names {left.group(0)}, which this request does not set"
                )
            filled[str(name)] = request.path_overrides.get(str(name), text)
        for name, field in (unit.get("environment_inputs") or {}).items():
            if not NAME.fullmatch(str(name)):
                raise ValueError(f"invalid unit environment name {name!r}")
            supplied = (
                str(getattr(request, str(field))).strip()
                or os.environ.get(str(name), "").strip()
            )
            if supplied:
                filled[str(name)] = supplied
        if request.mode == "install":
            for name in unit.get("required_environment_inputs") or []:
                if not filled.get(str(name)):
                    raise ValueError(
                        f"{name} is unset: supply the unit's declared request input or export it before installing"
                    )
        return filled

    def render(
        self,
        request: ModelRegistryUnitsRequest,
        unit: dict[str, Any],
        project: Path,
        uv: str,
        python: str,
    ) -> str:
        text = (NODE_DIR / str(unit["template"])).read_text(encoding="utf-8")
        home, root = Path(request.home), Path(request.registry_root)
        evidence = home / str(unit["evidence_file"])
        state = root / str(unit["state_directory"])
        environment = "".join(
            f"\n    <key>{escape(name)}</key><string>{escape(value)}</string>"
            for name, value in self.environment(request, unit).items()
        )
        user = self.run(["id", "-un"]).stdout.strip() or os.environ.get("USER", "")
        values = {
            "LABEL": str(unit["label"]),
            "UNIT": request.unit,
            "UV": uv,
            "PYTHON": python,
            "PROJECT": str(project),
            "MODULE": str(unit["module"]),
            "OMNI_HOME": str(root),
            "EVIDENCE_DIR": str(evidence.parent),
            "HOME": str(home),
            "USER": user,
            "STATE_DIR": str(state),
            "START_INTERVAL": str(unit["schedule"]["start_interval"]),
        }
        for key, value in values.items():
            text = text.replace(f"__{key}__", escape(value))
        text = text.replace(
            "__RUN_AT_LOAD__",
            "<true/>" if unit["schedule"]["run_at_load"] else "<false/>",
        )
        text = text.replace("__ENVIRONMENT__", environment)
        text = text.replace(
            "__TICK_SYNC_ARGS__",
            "<string>--no-sync</string>" if unit.get("tick_no_sync") else "",
        )
        plistlib.loads(
            text.encode("utf-8")
        )  # a plist that does not parse is never written
        return text

    # ------------------------------------------------------------------ modes

    def dispatch(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> ModelRegistryUnitsResult:
        if unit.get("platform") == "systemd":
            return HandlerSystemdRegistryTick(self).handle(request)
        label = str(unit["label"])
        home, root = Path(request.home), Path(request.registry_root)
        state = root / str(unit["state_directory"])
        evidence = home / str(unit["evidence_file"])
        plist = home / "Library/LaunchAgents" / f"{label}.plist"
        record = state / f"{request.unit}.installed.json"
        domain = "gui/" + self.run(["id", "-u"]).stdout.strip()
        target = f"{domain}/{label}"

        if request.mode == "kickstart":
            done = self.run(["launchctl", "kickstart", target])
            return self.result(
                request,
                done.returncode,
                out=done.stdout + f"kickstarted {label} once\n",
                error=done.stderr,
            )
        if request.mode == "uninstall":
            stopped = self.run(["launchctl", "bootout", target])
            plist.unlink(missing_ok=True)
            return self.result(
                request,
                0,
                out=stopped.stdout
                + f"removed {label} (state under {state} is left in place)\n",
            )
        if request.mode == "status":
            return self.status(request, unit, target, evidence, state, record)

        project, canonical = self.project_for(request, unit)
        if canonical and not request.expected_revision:
            revision = self.head_of(project)
        else:
            revision = self.checkout_revision(project, unit, request.expected_revision)
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError(f"uv is not on PATH; it runs {unit['module']} for {label}")
        python = self.interpreter_for(unit)
        text = self.render(request, unit, project, uv, python)
        if request.mode == "dry-run":
            return self.result(
                request,
                0,
                out=text,
                rendered=text,
                project=project,
                revision=revision,
                canonical=canonical,
            )
        proof = self.run(
            [
                uv,
                "run",
                "--frozen",
                "--python",
                python,
                "--project",
                str(project),
                "python",
                "-m",
                str(unit["module"]),
                "--help",
            ]
        )
        if proof.returncode:
            raise ValueError(
                f"{unit['module']} does not start from {project}; {label} was not loaded:\n{proof.stdout}{proof.stderr}"
            )
        state.mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="registry-units-") as scratch:
            rendered = Path(scratch) / "agent.plist"
            rendered.write_text(text, encoding="utf-8")
            rendered.chmod(0o600)
            self.run(["launchctl", "bootout", target])
            shutil.move(str(rendered), str(plist))
        started = self.run(["launchctl", "bootstrap", domain, str(plist)])
        if started.returncode:
            return self.result(
                request,
                started.returncode,
                out=started.stdout,
                error=started.stderr,
                project=project,
                revision=revision,
                canonical=canonical,
            )
        record.write_text(
            json.dumps(
                {
                    "unit": request.unit,
                    "label": label,
                    "project": str(project),
                    "project_is_canonical": canonical,
                    "revision": revision,
                    "installed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return self.result(
            request,
            0,
            out=f"installed {label}: {unit['schedule']['description']}, runs {unit['module']} from {project} at {revision}, "
            f"heartbeat {evidence}, logs in {state}\n",
            rendered=text,
            project=project,
            revision=revision,
            canonical=canonical,
            record=str(record),
        )

    def head_of(self, project: Path) -> str:
        head = self.run(["git", "-C", str(project), "rev-parse", "HEAD"])
        return head.stdout.strip() if head.returncode == 0 else ""

    def status(
        self,
        request: ModelRegistryUnitsRequest,
        unit: dict[str, Any],
        target: str,
        evidence: Path,
        state: Path,
        record: Path,
    ) -> ModelRegistryUnitsResult:
        label = str(unit["label"])
        out, failed = "", 0
        loaded = self.run(["launchctl", "print", target])
        if loaded.returncode == 0:
            out += f"LOADED {label}\n" + "".join(
                line + "\n"
                for line in loaded.stdout.splitlines()
                if re.match(r"^\s+(runs|last exit code|state) ", line)
            )
        else:
            out += f"NOT LOADED {label}\n{label} NOT RUNNING: agent is not loaded\n"
            failed = 1
        interval = int(unit["schedule"]["start_interval"])
        try:
            age = max(0.0, time.time() - evidence.stat().st_mtime)
            out += f"{evidence}: {int(age)} s old\n"
            if age > STALE_INTERVALS * interval:
                out += f"{label} NOT RUNNING: {evidence} is older than {STALE_INTERVALS * interval} s\n"
                failed = 1
        except OSError as exc:
            out += f"{label} NOT RUNNING: no readable {evidence} ({exc})\n"
            failed = 1
        try:
            recorded = json.loads(record.read_text(encoding="utf-8"))
            out += f"installed revision {recorded.get('revision')} of {recorded.get('project')}\n"
        except (OSError, ValueError):
            out += f"no install record at {record}\n"
        return self.result(request, failed, out=out)

    @staticmethod
    def result(
        request: ModelRegistryUnitsRequest,
        code: int,
        *,
        out: str = "",
        error: str = "",
        rendered: str = "",
        project: Path | str = "",
        revision: str = "",
        canonical: bool = True,
        record: str = "",
    ) -> ModelRegistryUnitsResult:
        return ModelRegistryUnitsResult(
            unit=request.unit,
            mode=request.mode,
            status="failure" if code else "success",
            exit_code=code,
            stdout=out,
            stderr=error,
            rendered=rendered,
            project=str(project),
            revision=revision,
            project_is_canonical=canonical,
            record=record,
        )


def _quoted(value: str) -> str:
    """One systemd directive argument; escape specifiers as well as quotes."""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("systemd directive contains a control character")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


class HandlerSystemdRegistryTick:
    def __init__(
        self,
        installer: HandlerRegistryUnits,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.installer = installer
        self.clock = clock

    def lingering(self) -> bool:
        user = self.installer.run(["id", "-un"])
        if user.returncode or not user.stdout.strip():
            return False
        result = self.installer.run(
            [
                "loginctl",
                "show-user",
                user.stdout.strip(),
                "--property=Linger",
                "--value",
            ]
        )
        return result.returncode == 0 and result.stdout.strip() == "yes"

    def command(self, *args: str) -> str:
        done = self.installer.run(["systemctl", "--user", *args])
        if done.returncode:
            raise ValueError(f"systemctl {' '.join(args)}: {done.stderr.strip()}")
        return done.stdout

    def handle(self, request: ModelRegistryUnitsRequest) -> ModelRegistryUnitsResult:
        unit = self.installer.units[request.unit]
        label = str(unit["label"])
        state = Path(request.registry_root) / str(unit["state_directory"])
        directory = Path(request.home) / str(unit["unit_directory"])
        timer, service = f"{label}.timer", f"{label}.service"
        record = state / f"{request.unit}.installed.json"
        if request.mode == "status":
            return self.status(request, unit, timer, service, record)
        if request.mode == "kickstart":
            out = self.command("start", service)
            return self.installer.result(request, 0, out=out)
        if request.mode == "uninstall":
            self.command("disable", "--now", timer)
            self.command("stop", service)
            for name in (timer, service):
                (directory / name).unlink(missing_ok=True)
            self.command("daemon-reload")
            return self.installer.result(request, 0, out=f"removed {timer}\n")

        project, canonical = self.installer.project_for(request, unit)
        # A timer always records and runs a clean revision, including the canonical checkout.
        revision = self.installer.checkout_revision(
            project, unit, request.expected_revision
        )
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError("uv is not on PATH")
        python = self.installer.interpreter_for(unit)
        environment = self.installer.environment(request, unit)
        argv = [
            uv,
            "run",
            "--frozen",
            "--python",
            python,
            "--project",
            str(project),
            "python",
            "-m",
            str(unit["module"]),
        ]
        replacements = {
            "LABEL": label,
            "COMMAND": " ".join(_quoted(arg) for arg in argv),
            "PROJECT": str(project).replace("%", "%%"),
            "ENVIRONMENT": "\n".join(
                f"Environment={_quoted(key + '=' + value)}"
                for key, value in environment.items()
            ),
            "CALENDAR": str(unit["schedule"]["calendar"]),
            "PERSISTENT": str(bool(unit["schedule"]["persistent"])).lower(),
            "TIMEOUT": str(unit["timeout_seconds"]),
            "ACCURACY": str(unit["schedule"]["accuracy_seconds"]),
        }
        rendered: dict[str, str] = {}
        for name, template in (
            (service, unit["template"]),
            (timer, unit["timer_template"]),
        ):
            text = (NODE_DIR / str(template)).read_text(encoding="utf-8")
            for key, value in replacements.items():
                text = text.replace(f"__{key}__", value)
            rendered[name] = text
        combined = "\n".join(rendered.values())
        if request.mode == "dry-run":
            return self.installer.result(
                request,
                0,
                out=combined,
                rendered=combined,
                project=project,
                revision=revision,
                canonical=canonical,
            )

        if unit["require_linger"] and not self.lingering():
            raise ValueError(
                "LINGER-REQUIRED: the user manager must survive logout; provision linger through the host installer"
            )
        # The probe reads the configuration the timer will read, so it carries the
        # unit's rendered environment, not whatever the installer's shell exports.
        proof = self.installer.run(
            [
                "env",
                *(f"{key}={value}" for key, value in environment.items()),
                *argv,
                "--check-config",
            ]
        )
        if proof.returncode:
            raise ValueError(
                f"tick configuration refused before install: {proof.stdout}{proof.stderr}"
            )
        directory.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        for name, text in rendered.items():
            path = directory / name
            temporary = path.with_suffix(path.suffix + ".new")
            temporary.write_text(text, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        self.command("daemon-reload")
        self.command("enable", "--now", timer)
        self.command("restart", timer)
        self.command("start", service)
        record.write_text(
            json.dumps(
                {
                    "revision": revision,
                    "project": str(project),
                    "installed_at": datetime.now(UTC).isoformat(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return self.installer.result(
            request,
            0,
            out=f"installed {timer} at {revision}; journalctl --user -u {service}\n",
            rendered=combined,
            project=project,
            revision=revision,
            canonical=canonical,
            record=str(record),
        )

    def status(
        self,
        request: ModelRegistryUnitsRequest,
        unit: dict[str, Any],
        timer: str,
        service: str,
        record: Path,
    ) -> ModelRegistryUnitsResult:
        timer_state = self.command(
            "show", timer, "--property=LoadState,ActiveState,UnitFileState"
        )
        properties = dict(
            line.split("=", 1) for line in timer_state.splitlines() if "=" in line
        )
        failed = (
            properties.get("LoadState") != "loaded"
            or properties.get("ActiveState") != "active"
            or properties.get("UnitFileState") != "enabled"
        )
        # A timer can be active while its service never ran. Read the service's actual start.
        stamp = self.command(
            "show", service, "--property=ExecMainStartTimestamp", "--value"
        ).strip()
        try:
            started = datetime.strptime(stamp, "%a %Y-%m-%d %H:%M:%S %Z").replace(
                tzinfo=UTC
            )
        except ValueError:
            started = None
        age = (self.clock() - started).total_seconds() if started else None
        threshold = int(unit["schedule"]["start_interval"]) * int(
            unit["status_intervals"]
        )
        failed |= age is None or age < 0 or age > threshold
        exit_status = self.command(
            "show", service, "--property=ExecMainStatus", "--value"
        ).strip()
        failed |= exit_status != "0"
        out = f"{timer}\n{timer_state}last_start={stamp or 'NEVER'} age_seconds={age} exit={exit_status}\n"
        if unit["require_linger"]:
            linger = self.lingering()
            failed |= not linger
            out += f"linger={linger}\n"
        try:
            installed = json.loads(record.read_text(encoding="utf-8"))
            out += f"installed revision {installed['revision']} of {installed['project']}\n"
        except (OSError, ValueError, KeyError):
            failed = True
            out += "MISSING-INSTALL-RECORD\n"
        return self.installer.result(request, int(failed), out=out)
