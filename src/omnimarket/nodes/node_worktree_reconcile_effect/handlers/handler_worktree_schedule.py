# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the existing host reconciliation node's own systemd user timer."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRunResult
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_schedule import (
    ModelWorktreeSchedule,
    ModelWorktreeScheduleOverlay,
    ModelWorktreeScheduleRequest,
    ModelWorktreeScheduleResult,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
MODULE = "omnimarket.nodes.node_worktree_reconcile_effect"
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


def load_overlay(path: str, host: str) -> ModelWorktreeScheduleOverlay:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host):
        raise ValueError("host must be a literal host identifier")
    overlay = ModelWorktreeScheduleOverlay.model_validate(
        yaml.safe_load(absolute_path(path).read_text(encoding="utf-8"))
    )
    if host not in overlay.hosts:
        raise ValueError(f"{host}: no worktree scheduler declared in the overlay")
    command = overlay.hosts[host]
    if command.host != host or not command.roots:
        raise ValueError(
            f"{host}: command must name this host and at least one worktree root"
        )
    for value in (
        overlay.python,
        overlay.project,
        overlay.state_directory,
        overlay.environment_file,
        *command.roots,
        *command.extra_clone_roots,
        *command.exclude_paths,
    ):
        absolute_path(value)
    if command.ledger_path:
        absolute_path(command.ledger_path)
    return overlay


class HandlerWorktreeSchedule:
    def __init__(
        self, *, unit_directory: Path | None = None, runner: Runner = run_command
    ) -> None:
        self.unit_directory = unit_directory or Path.home() / ".config/systemd/user"
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
            (state / "installed.json").write_text(
                json.dumps(
                    {
                        "revision": head,
                        "project": str(project),
                        "host": request.host,
                        "installed_at": datetime.now(UTC).isoformat(),
                    }
                )
                + "\n"
            )
        else:
            receipt = ModelWorktreeReconcileRunResult.model_validate_json(
                (state / "latest.json").read_text()
            )
            completed = receipt.completed_event
            age = (datetime.now(UTC) - completed.finished_at).total_seconds()
            installed = json.loads((state / "installed.json").read_text())
            if (
                not 0 <= age <= schedule.interval_seconds * 2
                or completed.failures
                or completed.host != request.host
                or installed["revision"] != head
                or installed["host"] != request.host
                or installed["project"] != str(project)
            ):
                return result.model_copy(
                    update={
                        "exit_code": 1,
                        "stderr": "stale or failed tick, or installed revision differs\n",
                    }
                )
        return result.model_copy(
            update={
                "stdout": f"{schedule.unit_name}.timer {request.mode} revision={head}\n"
            }
        )
