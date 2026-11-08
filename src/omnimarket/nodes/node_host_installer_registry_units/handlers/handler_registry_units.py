# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render and install launchd or systemd registry_module_tick units, with a project path override.

A registry_module_tick unit runs ``uv run --frozen --python <brew python> --project <project> python -m <module>`` on a timer. The project is the
canonical registry clone unless the unit contract declares ``project_path_override`` and the request names a checkout:
then the rendered unit runs that checkout, so a fix installs from its branch without waiting for a merge. The checkout
must be clean (the installed code equals its revision) and carry the unit's entry file. The revision the unit runs is
read from the checkout and written to the unit's install record. A unit whose contract says ``platform: systemd``
renders a user service and timer for a Linux pool host instead of a plist (OMN-22878).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelCopiedUnitPayload,
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)

NODE_DIR = Path(__file__).resolve().parents[1]
STALE_INTERVALS = 3
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# The modes that change what the host runs. A dry-run, a status read and a kickstart change nothing, so they never take the lock.
LOCKED_MODES = frozenset({"install", "uninstall"})
EXIT_INSTALL_IN_PROGRESS = 75


class InstallInProgressError(RuntimeError):
    """Another install or uninstall of the same unit holds the unit's lock."""


def interpreter_present(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, env=env, check=False, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


# launchd's ProcessType of a tick unit that declares none: Background throttles its CPU and disk reads.
DEFAULT_PROCESS_TYPE = "Background"
PROCESS_TYPES = frozenset({"Background", "Standard", "Adaptive", "Interactive"})
# launchd has no run limit for a StartInterval job, so a hung run holds the job and no tick follows (the overlay store
# refresh hung 40 minutes at 0% CPU on 2026-11-06 and the alarm opened a missed-interval episode). A unit that declares
# run_timeout_s runs under this wrapper: it puts the command in its own process group, ends the whole group with TERM
# and then KILL when the limit passes, and exits 124 so the job-exit alarm reads a failed exit instead of silence.
TIMEOUT_PROGRAM = (
    "my $limit = shift; my $pid = fork(); defined $pid or exit 71; "
    "if (!$pid) { setpgrp(0, 0); exec @ARGV; exit 127 } "
    "$SIG{ALRM} = sub { kill 'TERM', -$pid; sleep 5; kill 'KILL', -$pid; exit 124 }; "
    "alarm $limit; waitpid($pid, 0); "
    "exit($? & 127 ? 128 + ($? & 127) : $? >> 8)"
)
TIMEOUT_INTERPRETER = "/usr/bin/perl"


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
            with self.install_lock(request, unit):
                if unit.get("kind") == "copied_tree":
                    return self.copied_unit(request, unit)
                if unit.get("platform") == "systemd":
                    return self.dispatch_systemd(request, unit)
                return self.dispatch(request, unit)
        except InstallInProgressError as exc:
            return self.result(request, EXIT_INSTALL_IN_PROGRESS, error=f"{exc}\n")
        except (OSError, ValueError) as exc:
            return self.result(request, 1, error=f"{exc}\n")

    @contextmanager
    def install_lock(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> Iterator[None]:
        """Hold the unit's lock for an install or uninstall; a second one is refused, never queued.

        The lock is an advisory ``flock`` on a file in the unit's state directory, so the kernel drops it when the
        holder ends, fails or dies and a stale lock cannot block the next install. The file names the holder.
        """
        if request.mode not in LOCKED_MODES:
            yield
            return
        state = Path(request.registry_root) / str(unit["state_directory"])
        state.mkdir(parents=True, exist_ok=True)
        path = state / f"{request.unit}.install.lock"
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.seek(0)
                holder = handle.read().strip() or "a holder that wrote no record"
                raise InstallInProgressError(
                    f"INSTALL-IN-PROGRESS: another {request.unit} install holds {path} ({holder}); "
                    "this request changed nothing, ask the lane that owns the unit's installs"
                ) from None
            handle.seek(0)
            handle.truncate()
            handle.write(
                f"pid={os.getpid()} mode={request.mode} project={request.project_path or 'canonical'} "
                f"since={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            )
            handle.flush()
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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

    def config_target(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> Path | None:
        """Where the unit's declared configuration file is written: the value of the environment variable the
        declaration names, which is the path the installed unit reads (a path override included)."""
        declared = unit.get("config_file")
        if not declared:
            return None
        name = str(declared["environment"])
        value = self.environment(request, unit).get(name)
        if not value:
            raise ValueError(
                f"{request.unit} declares a configuration file at ${name}, which the unit does not set"
            )
        return Path(value)

    def config_text(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> str:
        """The unit's declared configuration document with every placeholder filled.

        Built-in placeholders are {home}, {registry_root} and {ledger_path} (the request's, else the registry's own
        ledger file). The others are the declaration's ``inputs``: each names a request field and an environment
        variable of the installing shell, and falls back to the value the unit's last install recorded, so a
        reinstall with no inputs reproduces the file. A placeholder nothing fills is refused, so a configuration is
        never written with a hole in it.
        """
        declared = unit.get("config_file")
        if not declared:
            return ""
        root = Path(request.registry_root)
        values = {
            "home": request.home,
            "registry_root": request.registry_root,
            "ledger_path": request.ledger_path
            or str(root / "docs/tracking/ROLLING_WORK_LEDGER.md"),
        }
        values.update(self.config_inputs(request, unit))

        def fill(node: Any) -> Any:
            if isinstance(node, str):
                for key, value in values.items():
                    node = node.replace("{" + key + "}", value)
                left = PLACEHOLDER.search(node)
                if left is not None:
                    raise ValueError(
                        f"the {request.unit} configuration names {left.group(0)}, which this request does not set "
                        "(name it as a request input, export its environment variable, or reinstall once with it)"
                    )
                return node
            if isinstance(node, list):
                return [fill(item) for item in node]
            if isinstance(node, dict):
                return {str(key): fill(item) for key, item in node.items()}
            return node

        return json.dumps(fill(declared["document"]), indent=1) + "\n"

    def config_inputs(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> dict[str, str]:
        """The values of the configuration's declared inputs: the request, else the installing shell, else the last install."""
        declared = (unit.get("config_file") or {}).get("inputs") or {}
        if not declared:
            return {}
        root = Path(request.registry_root)
        record = root / str(unit["state_directory"]) / f"{request.unit}.installed.json"
        try:
            recorded = dict(
                json.loads(record.read_text(encoding="utf-8")).get("config_inputs")
                or {}
            )
        except (OSError, ValueError, AttributeError, TypeError):
            recorded = {}
        found: dict[str, str] = {}
        for name, source in declared.items():
            value = (
                str(getattr(request, str(source["field"]), "")).strip()
                or os.environ.get(str(source.get("env") or ""), "").strip()
                or str(recorded.get(str(name)) or "")
            )
            if value:
                found[str(name)] = value
        return found

    @staticmethod
    def write_config(target: Path, text: str, mode: int) -> str:
        """Write ``text`` at ``target`` with ``mode`` and keep a differing file it replaces as ``<name>.previous``.

        Returns the path of the kept copy, or '' when nothing was replaced or the content was the same.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        kept = ""
        if target.is_file() and target.read_text(encoding="utf-8") != text:
            previous = target.with_name(target.name + ".previous")
            shutil.copyfile(target, previous)
            previous.chmod(mode)
            kept = str(previous)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        try:
            temporary.chmod(mode)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return kept

    @staticmethod
    def timeout_args(unit: dict[str, Any]) -> str:
        """The wrapper's ProgramArguments entries for a unit that declares run_timeout_s, else nothing."""
        limit = unit.get("run_timeout_s")
        if limit is None:
            return ""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(
                f"{unit['label']} declares run_timeout_s {limit!r}; it must be a whole number of seconds, at least 1"
            )
        return "".join(
            f"<string>{escape(part)}</string>\n    "
            for part in (TIMEOUT_INTERPRETER, "-e", TIMEOUT_PROGRAM, str(limit))
        )

    def render(
        self,
        request: ModelRegistryUnitsRequest,
        unit: dict[str, Any],
        project: Path,
        uv: str,
        python: str,
    ) -> str:
        text = (NODE_DIR / str(unit["template"])).read_text(encoding="utf-8")
        process_type = str(unit.get("process_type") or DEFAULT_PROCESS_TYPE)
        if process_type not in PROCESS_TYPES:
            raise ValueError(
                f"{unit['label']} declares process_type {process_type!r}; launchd knows {sorted(PROCESS_TYPES)}"
            )
        timeout_args = self.timeout_args(unit)
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
            "PROCESS_TYPE": process_type,
        }
        for key, value in values.items():
            text = text.replace(f"__{key}__", escape(value))
        text = text.replace(
            "__RUN_AT_LOAD__",
            "<true/>" if unit["schedule"]["run_at_load"] else "<false/>",
        )
        text = text.replace("__ENVIRONMENT__", environment)
        text = text.replace("__TIMEOUT_ARGS__", timeout_args)
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
        config_path = self.config_target(request, unit)
        config = self.config_text(request, unit)
        if request.mode == "dry-run":
            return self.result(
                request,
                0,
                out=text
                + (f"\nconfiguration {config_path}:\n{config}" if config else ""),
                rendered=text,
                project=project,
                revision=revision,
                canonical=canonical,
                config=config,
                config_path=str(config_path or ""),
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
        kept_config = ""
        if config_path is not None:
            kept_config = self.write_config(
                config_path,
                config,
                int(str(unit["config_file"].get("mode", "0600")), 8),
            )
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
                    "config_inputs": self.config_inputs(request, unit),
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
            f"heartbeat {evidence}, logs in {state}\n"
            + (
                f"wrote configuration {config_path}"
                + (
                    f", kept the file it replaced as {kept_config}"
                    if kept_config
                    else ""
                )
                + "\n"
                if config_path is not None
                else ""
            ),
            rendered=text,
            project=project,
            revision=revision,
            canonical=canonical,
            record=str(record),
            config=config,
            config_path=str(config_path or ""),
        )

    # ------------------------------------------------------------------ systemd

    @staticmethod
    def systemd_value(value: str) -> str:
        """A value placed inside a double-quoted systemd word; one systemd cannot hold unchanged is refused."""
        if any(c in value for c in '"\\\n'):
            raise ValueError(
                f"{value!r} holds a quote, backslash or newline a systemd unit cannot carry"
            )
        return value.replace("%", "%%")

    def render_systemd(
        self,
        request: ModelRegistryUnitsRequest,
        unit: dict[str, Any],
        project: Path,
        uv: str,
        python: str,
    ) -> tuple[str, str]:
        """The service and the timer text of a systemd registry_module_tick unit."""
        home, root = Path(request.home), Path(request.registry_root)
        evidence = home / str(unit["evidence_file"])
        state = root / str(unit["state_directory"])
        interval = int(unit["schedule"]["start_interval"])
        environment = " ".join(
            f'"{self.systemd_value(name)}={self.systemd_value(value)}"'
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
            "RUN_TIMEOUT": str(int(unit["schedule"].get("run_timeout_s") or interval)),
            "START_INTERVAL": str(interval),
            "DESCRIPTION": str(unit["schedule"]["description"]),
        }
        service = (NODE_DIR / str(unit["template"])).read_text(encoding="utf-8")
        timer = (NODE_DIR / str(unit["timer_template"])).read_text(encoding="utf-8")
        for key, value in values.items():
            service = service.replace(f"__{key}__", self.systemd_value(value))
            timer = timer.replace(f"__{key}__", self.systemd_value(value))
        service = service.replace(
            "__ENVIRONMENT__", f"Environment={environment}" if environment else ""
        )
        service = service.replace(
            "__TICK_SYNC_ARGS__", '"--no-sync"' if unit.get("tick_no_sync") else ""
        )
        timer = timer.replace(
            "__RUN_AT_LOAD__",
            "OnActiveSec=1s"
            if unit["schedule"]["run_at_load"]
            else f"OnActiveSec={interval}",
        )
        left = re.search(r"__[A-Z_]+__", service + timer)
        if (
            left is not None
        ):  # a template placeholder this renderer does not fill is never written
            raise ValueError(
                f"{unit['label']} template leaves {left.group(0)} unfilled"
            )
        return service, timer

    def dispatch_systemd(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> ModelRegistryUnitsResult:
        label = str(unit["label"])
        home, root = Path(request.home), Path(request.registry_root)
        state = root / str(unit["state_directory"])
        evidence = home / str(unit["evidence_file"])
        unit_dir = home / ".config/systemd/user"
        service_path = unit_dir / f"{label}.service"
        timer_path = unit_dir / f"{label}.timer"
        record = state / f"{request.unit}.installed.json"
        systemctl = ["systemctl", "--user"]

        if request.mode == "kickstart":
            done = self.run([*systemctl, "start", "--no-block", f"{label}.service"])
            return self.result(
                request,
                done.returncode,
                out=done.stdout + f"started {label}.service once\n",
                error=done.stderr,
            )
        if request.mode == "uninstall":
            stopped = self.run([*systemctl, "disable", "--now", f"{label}.timer"])
            service_path.unlink(missing_ok=True)
            timer_path.unlink(missing_ok=True)
            self.run([*systemctl, "daemon-reload"])
            return self.result(
                request,
                0,
                out=stopped.stdout
                + f"removed {label} (state under {state} is left in place)\n",
            )
        if request.mode == "status":
            return self.status_systemd(request, unit, evidence, record)

        project, canonical = self.project_for(request, unit)
        if canonical and not request.expected_revision:
            revision = self.head_of(project)
        else:
            revision = self.checkout_revision(project, unit, request.expected_revision)
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError(f"uv is not on PATH; it runs {unit['module']} for {label}")
        python = self.interpreter_for(unit)
        service, timer = self.render_systemd(request, unit, project, uv, python)
        text = service + "\n" + timer
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
        unit_dir.mkdir(parents=True, exist_ok=True)
        previous = {
            path: (path.read_bytes() if path.exists() else None)
            for path in (service_path, timer_path)
        }
        for path, body in ((service_path, service), (timer_path, timer)):
            staged = path.with_name(f".{path.name}.staged")
            staged.write_text(body, encoding="utf-8")
            staged.chmod(0o644)
            staged.replace(path)
        steps = (
            [*systemctl, "daemon-reload"],
            [*systemctl, "enable", f"{label}.timer"],
            [*systemctl, "restart", f"{label}.timer"],
        )
        for step in steps:
            done = self.run(step)
            if done.returncode:
                for (
                    path,
                    old,
                ) in (
                    previous.items()
                ):  # put the previous unit back, never leave a half install
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(old)
                self.run([*systemctl, "daemon-reload"])
                if previous[timer_path] is not None:
                    self.run([*systemctl, "restart", f"{label}.timer"])
                return self.result(
                    request,
                    done.returncode,
                    out=done.stdout,
                    error=f"{' '.join(step)} failed, previous unit restored:\n{done.stderr}",
                    project=project,
                    revision=revision,
                    canonical=canonical,
                )
        record.write_text(
            json.dumps(
                {
                    "unit": request.unit,
                    "label": label,
                    "platform": "systemd",
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
            out=f"installed {label}.timer: {unit['schedule']['description']}, runs {unit['module']} from {project} at "
            f"{revision}, heartbeat {evidence}, logs in {state}\n",
            rendered=text,
            project=project,
            revision=revision,
            canonical=canonical,
            record=str(record),
        )

    def status_systemd(
        self,
        request: ModelRegistryUnitsRequest,
        unit: dict[str, Any],
        evidence: Path,
        record: Path,
    ) -> ModelRegistryUnitsResult:
        label = str(unit["label"])
        out, failed = "", 0
        active = self.run(["systemctl", "--user", "is-active", f"{label}.timer"])
        if active.returncode == 0:
            out += f"LOADED {label}.timer\n"
        else:
            out += f"NOT LOADED {label}\n{label} NOT RUNNING: timer is {active.stdout.strip() or 'absent'}\n"
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
        config: str = "",
        config_path: str = "",
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
            config=config,
            config_path=config_path,
        )

    def copied_unit(
        self, request: ModelRegistryUnitsRequest, unit: dict[str, Any]
    ) -> ModelRegistryUnitsResult:
        """Stage and smoke a copied bundle before changing a loaded agent."""
        label = str(unit["label"])
        home = Path(request.home)
        install = home / str(unit["install_directory"])
        state = Path(request.registry_root) / str(unit["state_directory"])
        plist = home / "Library/LaunchAgents" / f"{label}.plist"
        rendered = ""
        digest = ""

        def result(
            code: int = 0, out: str = "", err: str = ""
        ) -> ModelRegistryUnitsResult:
            return ModelRegistryUnitsResult(
                unit=request.unit,
                mode=request.mode,
                status="failure" if code else "success",
                exit_code=code,
                stdout=out,
                stderr=err,
                rendered=rendered,
                project=request.source_root,
                bundle_sha256=digest,
                project_is_canonical=False,
            )

        def checked(argv: list[str]) -> subprocess.CompletedProcess[str]:
            done = self.run(argv)
            if done.returncode:
                raise ValueError(f"{argv[0]} exited {done.returncode}: {done.stderr}")
            return done

        try:
            domain = f"gui/{checked(['id', '-u']).stdout.strip()}"
            target = f"{domain}/{label}"
            if request.mode == "kickstart":
                checked(["launchctl", "kickstart", target])
                return result(out=f"kickstarted {label}\n")
            if request.mode == "uninstall":
                self.run(["launchctl", "bootout", target])
                plist.unlink(missing_ok=True)
                return result(
                    out=f"removed {label}; copied payload and state retained\n"
                )
            if request.mode == "status":
                checked(["launchctl", "print", target])
                log = state / "runs.jsonl"
                rows = log.read_text(encoding="utf-8").splitlines()
                if not rows or time.time() - log.stat().st_mtime > 1800:
                    raise ValueError("runs.jsonl is empty or older than 30 minutes")
                return result(out=f"LOADED {label}\n{rows[-1]}\n")
            overlay_path = Path(__file__).resolve().parents[1] / self.relative(
                str(unit["payload_overlay"])
            )
            payload = ModelCopiedUnitPayload.model_validate(
                yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            )
            source = Path(request.source_root)
            if not request.source_root or not source.is_absolute():
                raise ValueError(
                    "source_root must name the absolute plugin root, with skills and src"
                )
            python = next(
                (
                    str(p)
                    for p in unit["interpreter_candidates"]
                    if os.access(p, os.X_OK)
                ),
                "",
            )
            if not python:
                raise ValueError("no declared executable interpreter exists")
            with tempfile.TemporaryDirectory(prefix="copied-unit-") as scratch:
                staged = Path(scratch) / "payload"
                staged.mkdir()
                for name in payload.copied_files:
                    relative = self.relative(str(name))
                    origin = source / relative
                    destination = staged / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(origin, destination)
                for name in payload.copied_trees:
                    relative = self.relative(str(name))
                    shutil.copytree(
                        source / relative,
                        staged / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
                    )
                script = self.relative(str(unit["script"]))
                # The same layout is tested here and installed below. Clean PYTHONPATH
                # in the command adapter; never point it at another checkout.
                checked([python, "-B", str(staged / script), "--help"])
                sha = hashlib.sha256()
                for path in sorted(p for p in staged.rglob("*") if p.is_file()):
                    sha.update(path.relative_to(staged).as_posix().encode() + b"\0")
                    sha.update(hashlib.sha256(path.read_bytes()).digest())
                digest = sha.hexdigest()
                schedule = unit["schedule"]
                ledger = request.ledger_path or str(
                    Path(request.registry_root) / "docs/tracking/ROLLING_WORK_LEDGER.md"
                )
                arguments = [
                    str(arg).format(state=str(state), ledger=ledger)
                    for arg in payload.arguments
                ]
                user = checked(["id", "-un"]).stdout.strip()
                rendered = plistlib.dumps(
                    {
                        "Label": label,
                        "ProgramArguments": [
                            python,
                            "-B",
                            str(install / script),
                            *arguments,
                        ],
                        "WorkingDirectory": request.registry_root,
                        "ProcessType": "Background",
                        "StartInterval": schedule["start_interval"],
                        "RunAtLoad": schedule["run_at_load"],
                        "EnvironmentVariables": {
                            "HOME": str(home),
                            "OMNI_HOME": request.registry_root,
                            "USER": user,
                            "LOGNAME": user,
                            "ONEX_LEDGER_PATH": ledger,
                            "ONEX_STATE_DIR": str(
                                Path(request.registry_root) / ".onex_state"
                            ),
                            "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                        },
                        "StandardOutPath": str(state / f"{request.unit}.out.log"),
                        "StandardErrorPath": str(state / f"{request.unit}.err.log"),
                    },
                    sort_keys=False,
                ).decode()
                candidate = Path(scratch) / "agent.plist"
                candidate.write_text(rendered, encoding="utf-8")
                checked(["plutil", "-lint", str(candidate)])
                if request.mode == "dry-run":
                    return result(out=rendered)
                self.activate(
                    staged, install, candidate, plist, state, domain, target, checked
                )
                (state / "install.json").write_text(
                    json.dumps(
                        {"source_root": request.source_root, "bundle_sha256": digest}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return result(out=f"installed {label}; bundle_sha256={digest}\n")
        except (OSError, ValueError, shutil.Error) as exc:
            return result(1, err=f"{exc}\n")

    @staticmethod
    def relative(name: str) -> Path:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"payload path must stay relative: {name}")
        return path

    def activate(
        self,
        staged: Path,
        install: Path,
        candidate: Path,
        plist: Path,
        state: Path,
        domain: str,
        target: str,
        checked: Runner,
    ) -> None:
        """Keep the old payload and plist for rollback if bootstrap refuses."""
        install.parent.mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".copied-unit-", dir=install.parent
        ) as scratch:
            swap = Path(scratch)
            ready = swap / "ready"
            shutil.copytree(staged, ready)
            previous = swap / "previous"
            old_plist = plist.read_bytes() if plist.exists() else None
            loaded = self.run(["launchctl", "print", target]).returncode == 0
            if loaded:
                checked(["launchctl", "bootout", target])
            replaced = False
            try:
                if install.exists():
                    install.rename(previous)
                ready.rename(install)
                replaced = True
                shutil.copy2(candidate, plist)
                checked(["launchctl", "bootstrap", domain, str(plist)])
            except (OSError, ValueError):
                if replaced:
                    shutil.rmtree(install)
                if previous.exists():
                    previous.rename(install)
                if old_plist is None:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_bytes(old_plist)
                    if loaded:
                        checked(["launchctl", "bootstrap", domain, str(plist)])
                raise
