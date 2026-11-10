# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provision the legacy Mac base tools through bounded vendor commands.

The setup engine owns progress. Vendor success is followed by tool observation;
no phase marker stands in for an installed binary. Administrative credentials
stay with sudo, never in a setup request or credential store.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
    collect_machine_facts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

_TOOLS = ("git", "brew", "gh", "jq", "python3.13", "uv")


def _locate(name: str) -> str | None:
    selected = shutil.which(name)
    if selected:
        return selected
    for directory in (
        Path(sys.executable).parent,
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path.home() / ".local" / "bin",
        Path("/usr/bin"),
    ):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class HandlerSetupBaseTools:
    """Native prerequisites; verify reads, installation uses the reviewed plan."""

    name = "base-tools"

    def __init__(
        self,
        *,
        collect: Callable[[Path], ModelSetupMachineFacts] | None = None,
        locate: Callable[[str], str | None] | None = None,
        run: Callable[[ModelSetupCommandRequest], ModelSetupCommandResult]
        | None = None,
        clt_marker: Path | None = None,
        secrets: tuple[SecretStr, ...] = (),
    ) -> None:
        self._collect = collect or collect_machine_facts
        self._locate = locate or _locate
        self._run = run or HandlerSetupCommand(secrets=secrets).handle
        self._marker = clt_marker or Path(
            "/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
        )

    def _result(
        self, status: str, fix: str, *, changed: bool = False
    ) -> ModelSetupObservation:
        return ModelSetupObservation.model_validate(
            {"step": self.name, "status": status, "fix": fix, "changed": changed}
        )

    def _missing(self, facts: ModelSetupMachineFacts) -> list[str]:
        missing = [tool for tool in _TOOLS if self._locate(tool) is None]
        if "xcode-command-line-tools" in facts.missing_tools:
            missing.insert(0, "xcode-command-line-tools")
        return missing

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            facts = self._collect(plan.workspace)
            if facts.platform != "Darwin":
                return self._result(
                    "unavailable", "Base-tool provisioning supports macOS only."
                )
            missing = self._missing(facts)
            if missing:
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable",
                    evidence={"missing_tools": ",".join(missing)},
                    fix="Run onex setup --action repair to install the missing base tools.",
                )
            return ModelSetupObservation(
                step=self.name,
                status="success",
                evidence={tool: self._locate(tool) or "unavailable" for tool in _TOOLS},
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return self._result(
                "failure",
                "Base tools could not be observed. Check native Terminal and system-tool access.",
            )

    def _command(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = 300,
        environment: dict[str, str] | None = None,
    ) -> ModelSetupCommandResult:
        path = os.pathsep.join(
            (
                str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                os.environ.get("PATH", "/usr/bin:/bin"),
            )
        )
        return self._run(
            ModelSetupCommandRequest(
                argv=argv,
                cwd=Path.home(),
                timeout_seconds=timeout,
                environment={"PATH": path, **(environment or {})},
            )
        )

    def _checked(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = 1200,
        environment: dict[str, str] | None = None,
    ) -> None:
        if self._command(argv, timeout=timeout, environment=environment).returncode:
            raise RuntimeError("The selected vendor command failed")

    def _download_install(
        self, url: str, shell: str, environment: dict[str, str]
    ) -> None:
        curl = self._locate("curl") or "/usr/bin/curl"
        with tempfile.TemporaryDirectory(prefix="onex-base-tools-") as directory:
            script = Path(directory) / "install.sh"
            self._checked(
                (
                    curl,
                    "-fsSL",
                    "--retry",
                    "3",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "300",
                    url,
                    "-o",
                    str(script),
                ),
                timeout=1000,
            )
            self._checked((shell, str(script)), environment=environment)

    def _install_clt(self) -> ModelSetupObservation | None:
        try:
            fd = os.open(
                self._marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            return self._result(
                "action_required",
                "Another Command Line Tools installation owns the existing marker. Finish it before retrying.",
            )
        owned = os.fstat(fd)
        os.close(fd)
        try:
            listed = self._command(("/usr/sbin/softwareupdate", "-l"), timeout=300)
            labels = re.findall(
                r"^\s*\*\s+Label:\s*(Command Line Tools[^\r\n]*)",
                listed.stdout,
                re.MULTILINE,
            )
            if listed.returncode or not labels:
                return self._result(
                    "action_required",
                    "No Command Line Tools update was offered. Run xcode-select --install, finish its installer, then repair setup.",
                )
            label = max(
                labels,
                key=lambda value: tuple(
                    (1, int(part)) if part.isdigit() else (0, part)
                    for part in re.split(r"(\d+)", value)
                ),
            )
            self._checked(
                (
                    "/usr/bin/sudo",
                    "-n",
                    "/usr/sbin/softwareupdate",
                    "-i",
                    label,
                    "--verbose",
                ),
                timeout=7200,
            )
            return None
        finally:
            try:
                current = self._marker.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == (
                owned.st_dev,
                owned.st_ino,
            ):
                self._marker.unlink()

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        mutated = False
        try:
            suitability = HandlerSetupPreflight(
                collect=self._collect, require_tools=False
            ).handle(plan)
            if suitability.status != "success":
                return suitability.model_copy(update={"step": self.name})
            missing = self._missing(self._collect(plan.workspace))
            if not missing:
                return None
            if "xcode-command-line-tools" in missing and self._marker.exists():
                return self._result(
                    "action_required",
                    "Another Command Line Tools installation owns the existing marker. Finish it before retrying.",
                )
            if (
                "brew" in missing or "xcode-command-line-tools" in missing
            ) and self._command(("/usr/bin/sudo", "-n", "true"), timeout=10).returncode:
                return self._result(
                    "action_required",
                    "Authorize the Mac administrator step yourself with sudo -v in this Terminal, then repeat onex setup --action repair. Setup never reads your password.",
                )
            if "xcode-command-line-tools" in missing:
                mutated = True
                pending = self._install_clt()
                if pending is not None:
                    return pending.model_copy(update={"changed": True})
            if self._locate("brew") is None:
                mutated = True
                self._download_install(
                    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh",
                    "/bin/bash",
                    {"NONINTERACTIVE": "1"},
                )
            brew = self._locate("brew")
            if brew is None:
                return self._result(
                    "failure",
                    "Homebrew did not become available. Review its official installer and repeat setup repair.",
                    changed=mutated,
                )
            for tool, formula in (
                ("git", "git"),
                ("gh", "gh"),
                ("jq", "jq"),
                ("python3.13", "python@3.13"),
            ):
                if self._locate(tool) is None:
                    mutated = True
                    self._checked(
                        (brew, "install", formula),
                        environment={
                            "HOMEBREW_NO_ENV_HINTS": "1",
                            "HOMEBREW_NO_AUTO_UPDATE": "1",
                        },
                    )
            if self._locate("uv") is None:
                mutated = True
                self._download_install(
                    "https://astral.sh/uv/install.sh",
                    "/bin/sh",
                    {"UV_NO_MODIFY_PATH": "1"},
                )
            return None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return self._result(
                "failure",
                "A base-tool installer could not complete. Check Xcode tools, Homebrew and the selected formula or Astral uv installer, then repeat setup repair.",
                changed=mutated,
            )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        observed = self.observe(plan)
        if plan.action == "verify" or observed.status != "unavailable":
            return observed
        outcome = self.apply(plan)
        if outcome is not None:
            return outcome
        return self.observe(plan).model_copy(update={"changed": True})
