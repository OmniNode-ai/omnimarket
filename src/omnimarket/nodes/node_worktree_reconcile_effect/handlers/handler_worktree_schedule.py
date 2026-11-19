# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the existing host reconciliation node's own systemd user timer or launchd agent."""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.rules_unit_roots import (
    refuse_worktree_plists,
    refuse_worktree_roots,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_reconcile_report import (
    ModelWorktreeReconcileRunReport,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_schedule import (
    ModelWorktreeSchedule,
    ModelWorktreeScheduleOverlay,
    ModelWorktreeScheduleRequest,
    ModelWorktreeScheduleResult,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
MODULE = "omnimarket.nodes.node_worktree_reconcile_effect"
SHELL = "/bin/sh"
DROPIN_NAME = "10-low-priority.conf"
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)


def absolute_path(value: str) -> Path:
    """Only literal absolute paths can be rendered into a systemd unit."""
    if not re.fullmatch(r"/[A-Za-z0-9_./@+=,:-]+", value) or ".." in Path(value).parts:
        raise ValueError(
            "schedule paths must be absolute and contain only literal path characters"
        )
    return Path(value)


def expand_omni_home(value: str) -> str:
    """Resolve a literal ``$OMNI_HOME`` prefix from the installing process's environment.

    A Mac host's registry root is a home directory path an overlay file may never carry,
    so the overlay writes ``$OMNI_HOME/...`` and the loader expands it (OMN-22799). Any
    other value is returned unchanged: a ``$`` elsewhere stays and is refused later by
    :func:`absolute_path`.
    """
    if value != "$OMNI_HOME" and not value.startswith("$OMNI_HOME/"):
        return value
    home = os.environ.get("OMNI_HOME", "")
    if not home:
        raise ValueError("OMNI_HOME is not set")
    if home.endswith("/"):
        home = home[:-1]
    return home + value[len("$OMNI_HOME") :]


#: The overlay's path fields, top level and per host; exactly these expand $OMNI_HOME.
_PATH_FIELDS = ("python", "project", "state_directory", "environment_file")
_LIST_FIELDS = ("roots", "extra_clone_roots", "exclude_paths")


def _expand_document_omni_home(document: dict[str, Any]) -> None:
    """Expand $OMNI_HOME in the raw document's path fields in place, before validation.

    ``merged_pr_command`` and the like keep their ``$OMNI_HOME``: the shell expands
    those at run time.
    """
    for field in _PATH_FIELDS:
        if isinstance(document.get(field), str):
            document[field] = expand_omni_home(document[field])
    # A malformed hosts or host_paths is left alone: model validation refuses it.
    hosts = document.get("hosts")
    if isinstance(hosts, dict):
        for command in hosts.values():
            if not isinstance(command, dict):
                continue
            for field in _LIST_FIELDS:
                items = command.get(field)
                if isinstance(items, list):
                    command[field] = [
                        expand_omni_home(item) if isinstance(item, str) else item
                        for item in items
                    ]
            if isinstance(command.get("ledger_path"), str):
                command["ledger_path"] = expand_omni_home(command["ledger_path"])
    host_paths = document.get("host_paths")
    if isinstance(host_paths, dict):
        for paths in host_paths.values():
            if not isinstance(paths, dict):
                continue
            for field in _PATH_FIELDS:
                if isinstance(paths.get(field), str):
                    paths[field] = expand_omni_home(paths[field])


def load_overlay(path: str, host: str) -> ModelWorktreeScheduleOverlay:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise ValueError("host must be a literal host identifier")
    document = yaml.safe_load(absolute_path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict):
        _expand_document_omni_home(document)
    overlay = ModelWorktreeScheduleOverlay.model_validate(document)
    if host not in overlay.hosts:
        raise ValueError(f"{host}: no worktree scheduler declared in the overlay")
    command = overlay.hosts[host]
    if command.host != host or not command.roots:
        raise ValueError(
            f"{host}: command must name this host and at least one worktree root"
        )
    resolved = overlay.for_host(host)
    for value in (
        resolved.python,
        resolved.project,
        resolved.state_directory,
        resolved.environment_file,
        *command.roots,
        *command.extra_clone_roots,
        *command.exclude_paths,
    ):
        absolute_path(value)
    if command.ledger_path:
        absolute_path(command.ledger_path)
    return resolved


class HandlerWorktreeSchedule:
    def __init__(
        self,
        *,
        unit_directory: Path | None = None,
        launchd_directory: Path | None = None,
        uid: int | None = None,
        runner: Runner = run_command,
    ) -> None:
        self.unit_directory = unit_directory or Path.home() / ".config/systemd/user"
        self.launchd_directory = (
            launchd_directory or Path.home() / "Library/LaunchAgents"
        )
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner

    def handle(
        self, request: ModelWorktreeScheduleRequest
    ) -> ModelWorktreeScheduleResult:
        try:
            return self._handle(request)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            yaml.YAMLError,
            subprocess.SubprocessError,
        ) as exc:
            return ModelWorktreeScheduleResult(exit_code=1, stderr=f"{exc}\n")

    def _handle(
        self, request: ModelWorktreeScheduleRequest
    ) -> ModelWorktreeScheduleResult:
        overlay = load_overlay(request.overlay_path, request.host)
        schedule = ModelWorktreeSchedule.model_validate(
            yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["schedule"]
        )
        project = Path(overlay.project)
        entry = project / "src" / Path(*MODULE.split(".")) / "__main__.py"
        if not entry.is_file():
            raise ValueError("project does not carry the worktree reconciliation node")
        # Git location inherited from a dispatch lane must not select another checkout.
        git_env = dict(os.environ)
        selected_tree = git_env.get("GIT_WORK_TREE")
        if not selected_tree or Path(selected_tree).resolve() != project.resolve():
            git_env = {k: v for k, v in git_env.items() if not k.startswith("GIT_")}
        head = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            env=git_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        if request.expected_revision is not None and head != request.expected_revision:
            raise ValueError("project revision differs from expected_revision")
        if request.mode == "install":
            dirty = subprocess.run(
                ["git", "-C", str(project), "status", "--porcelain"],
                env=git_env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            if dirty.stdout.strip():
                raise ValueError("install requires a clean project checkout")
        state = Path(overlay.state_directory)
        if overlay.schedulers.get(request.host, "systemd") == "launchd":
            return self._launchd(request, overlay, schedule, project, head, state)
        service = (
            "[Unit]\nDescription=ONEX host worktree reconciliation\n\n[Service]\n"
            f"Type=oneshot\nTimeoutStartSec={schedule.timeout_seconds}\n"
            f'WorkingDirectory={project}\nEnvironment="PYTHONPATH={project}/src"\n'
            f"EnvironmentFile={overlay.environment_file}\n"
            "UnsetEnvironment=GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\n"
            f'ExecStart="{overlay.python}" -m {MODULE} tick '
            f'--host "{request.host}" --overlay "{absolute_path(request.overlay_path)}"\n'
            "StandardOutput=journal\nStandardError=journal\n"
        )
        timer = (
            "[Unit]\nDescription=ONEX recurring worktree reconciliation\n\n[Timer]\n"
            f"OnBootSec={schedule.on_boot_seconds}s\n"
            f"OnUnitActiveSec={schedule.interval_seconds}s\nPersistent=true\n"
            f"Unit={schedule.unit_name}.service\n\n[Install]\nWantedBy=timers.target\n"
        )
        resources = schedule.resources
        dropin = (
            "[Service]\n"
            f"Nice={resources.nice}\n"
            f"IOSchedulingClass={resources.io_scheduling_class}\n"
            f"IOSchedulingPriority={resources.io_scheduling_priority}\n"
            f"CPUWeight={resources.cpu_weight}\n"
        )
        if request.mode != "status":
            # A unit that runs from a worktree dies when this node removes the tree: refuse before
            # anything is written or returned, naming the path (OMN-22783).
            refuse_worktree_roots(
                {
                    f"{schedule.unit_name}.service": service,
                    f"{schedule.unit_name}.timer": timer,
                    f"{schedule.unit_name}.service.d/{DROPIN_NAME}": dropin,
                }
            )
        result = ModelWorktreeScheduleResult(
            exit_code=0,
            service=service,
            timer=timer,
            dropin=dropin,
            revision=head,
        )
        if request.mode == "render":
            return result
        if request.mode == "install":
            if (
                not Path(overlay.python).is_file()
                or not Path(overlay.environment_file).is_file()
            ):
                raise ValueError(
                    "interpreter and host environment file must exist before install"
                )
            self.unit_directory.mkdir(parents=True, exist_ok=True)
            state.mkdir(parents=True, exist_ok=True)
            (self.unit_directory / f"{schedule.unit_name}.service").write_text(service)
            (self.unit_directory / f"{schedule.unit_name}.timer").write_text(timer)
            # The one drop-in the unit has: a hand-made override of the same name is replaced.
            dropin_directory = self.unit_directory / f"{schedule.unit_name}.service.d"
            dropin_directory.mkdir(exist_ok=True)
            (dropin_directory / DROPIN_NAME).write_text(dropin)
            for argv in (
                ["systemctl", "--user", "daemon-reload"],
                [
                    "systemctl",
                    "--user",
                    "enable",
                    "--now",
                    f"{schedule.unit_name}.timer",
                ],
            ):
                done = self.runner(argv)
                if done.returncode:
                    return result.model_copy(
                        update={"exit_code": done.returncode, "stderr": done.stderr}
                    )
        for argv in (
            ["systemctl", "--user", "is-enabled", f"{schedule.unit_name}.timer"],
            ["systemctl", "--user", "is-active", f"{schedule.unit_name}.timer"],
            [
                "systemctl",
                "--user",
                "list-timers",
                "--all",
                "--no-pager",
                f"{schedule.unit_name}.timer",
            ],
        ):
            done = self.runner(argv)
            if done.returncode or (
                "list-timers" in argv
                and f"{schedule.unit_name}.timer" not in done.stdout
            ):
                return result.model_copy(
                    update={
                        "exit_code": done.returncode or 1,
                        "stderr": done.stderr or "no scheduled timer\n",
                    }
                )
        if request.mode == "install":
            self._record_install(state, project, request.host, head)
        else:
            stale = self._stale_receipt(state, project, request.host, head, schedule)
            if stale:
                return result.model_copy(update={"exit_code": 1, "stderr": stale})
        return result.model_copy(
            update={
                "stdout": f"{schedule.unit_name}.timer {request.mode} revision={head}\n"
            }
        )

    def _record_install(self, state: Path, project: Path, host: str, head: str) -> None:
        """The install receipt both managers write, so a status names what runs and at what revision."""
        (state / "installed.json").write_text(
            json.dumps(
                {
                    "revision": head,
                    "project": str(project),
                    "host": host,
                    "installed_at": datetime.now(UTC).isoformat(),
                }
            )
            + "\n"
        )

    def _stale_receipt(
        self,
        state: Path,
        project: Path,
        host: str,
        head: str,
        schedule: ModelWorktreeSchedule,
    ) -> str:
        """Why the tick and install receipts are stale, failed or another host's; empty when current."""
        receipt = ModelWorktreeReconcileRunReport.model_validate_json(
            (state / "latest.json").read_text()
        )
        completed = receipt.completed_event
        age = (datetime.now(UTC) - completed.finished_at).total_seconds()
        installed = json.loads((state / "installed.json").read_text())
        if (
            not 0 <= age <= schedule.interval_seconds * 2
            or completed.failures
            or completed.host != host
            or installed["revision"] != head
            or installed["host"] != host
            or installed["project"] != str(project)
        ):
            return "stale or failed tick, or installed revision differs\n"
        return ""

    def _launchd(
        self,
        request: ModelWorktreeScheduleRequest,
        overlay: ModelWorktreeScheduleOverlay,
        schedule: ModelWorktreeSchedule,
        project: Path,
        head: str,
        state: Path,
    ) -> ModelWorktreeScheduleResult:
        """The launchd path of the same installer, for a Mac the overlay declares (OMN-22799)."""
        label = schedule.launchd_label
        domain = f"gui/{self.uid}"
        target = f"{domain}/{label}"
        plist_path = self.launchd_directory / f"{label}.plist"
        if request.mode == "render":
            return ModelWorktreeScheduleResult(
                exit_code=0,
                plist=self._render_launchd(request, overlay, schedule, project, state),
                revision=head,
            )
        if request.mode == "install":
            plist = self._render_launchd(request, overlay, schedule, project, state)
            if (
                not Path(overlay.python).is_file()
                or not Path(overlay.environment_file).is_file()
            ):
                raise ValueError(
                    "interpreter and host environment file must exist before install"
                )
            self.launchd_directory.mkdir(parents=True, exist_ok=True)
            state.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist)
            for argv, unload in (
                (["launchctl", "bootout", target], True),
                (["launchctl", "bootstrap", domain, str(plist_path)], False),
                (["launchctl", "enable", target], False),
                (["launchctl", "kickstart", target], False),
            ):
                done = self.runner(argv)
                # Only the bootout of a job that is not loaded yet fails without failing the install.
                if done.returncode and not unload:
                    return ModelWorktreeScheduleResult(
                        exit_code=done.returncode,
                        stderr=done.stderr,
                        plist=plist,
                        revision=head,
                    )
            self._record_install(state, project, request.host, head)
            return ModelWorktreeScheduleResult(
                exit_code=0,
                stdout=f"{label}.plist install revision={head}\n",
                plist=plist,
                revision=head,
            )
        loaded = self.runner(["launchctl", "print", target])
        if loaded.returncode:
            return ModelWorktreeScheduleResult(
                exit_code=loaded.returncode or 1,
                stderr=loaded.stderr or f"{label} is not loaded\n",
                revision=head,
            )
        stale = self._stale_receipt(state, project, request.host, head, schedule)
        if stale:
            return ModelWorktreeScheduleResult(exit_code=1, stderr=stale, revision=head)
        return ModelWorktreeScheduleResult(
            exit_code=0,
            stdout=f"{label}.plist status revision={head}\n",
            revision=head,
        )

    def _render_launchd(
        self,
        request: ModelWorktreeScheduleRequest,
        overlay: ModelWorktreeScheduleOverlay,
        schedule: ModelWorktreeSchedule,
        project: Path,
        state: Path,
    ) -> str:
        """The one launchd agent plist of the host timer, refused when it would run from a worktree."""
        resources = schedule.resources
        overlay_path = str(absolute_path(request.overlay_path))
        document = {
            "Label": schedule.launchd_label,
            "ProgramArguments": [
                SHELL,
                "-c",
                'set -a; . "$1"; shift; exec "$@"',
                "onex-worktree-reconcile",
                overlay.environment_file,
                overlay.python,
                "-m",
                MODULE,
                "tick",
                "--host",
                request.host,
                "--overlay",
                overlay_path,
            ],
            "WorkingDirectory": str(project),
            "EnvironmentVariables": {
                "PYTHONPATH": f"{project}/src",
            },
            "StartInterval": schedule.interval_seconds,
            "RunAtLoad": True,
            # cpu_weight has no launchd equivalent: launchd throttles through ProcessType alone,
            # so the contract's resources.cpu_weight is not rendered into the agent.
            "Nice": resources.nice,
            "ProcessType": "Background",
            "LowPriorityIO": resources.io_scheduling_class == "idle",
            "StandardOutPath": str(state / "tick.out.log"),
            "StandardErrorPath": str(state / "tick.err.log"),
        }
        plist = plistlib.dumps(document, sort_keys=False).decode()
        # A unit that runs from a worktree dies when this node removes the tree: refuse before
        # anything is written or returned, naming the path (OMN-22783).
        refuse_worktree_plists({f"{schedule.launchd_label}.plist": plist.encode()})
        return plist
