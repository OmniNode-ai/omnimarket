# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check supported client platforms; capability owners check their own effects."""

from __future__ import annotations

import platform
import shutil
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
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


def collect_machine_facts(workspace: Path) -> ModelSetupMachineFacts:
    """Read bounded OS facts without starting services or probing stack ports."""
    ancestor = workspace
    while not ancestor.exists():
        ancestor = ancestor.parent
    system = platform.system()
    observations: dict[str, bool | None] = {}
    if system == "Darwin":
        command = HandlerSetupCommand()
        for name, key in (
            ("rosetta", "sysctl.proc_translated"),
            ("virtual_machine", "kern.hv_vmm_present"),
        ):
            result = command.handle(
                ModelSetupCommandRequest(
                    argv=("/usr/sbin/sysctl", "-n", key), timeout_seconds=5
                )
            )
            observations[name] = (
                {"0": False, "1": True}.get(result.stdout.strip())
                if result.returncode == 0
                else None
            )
    return ModelSetupMachineFacts(
        platform=system,
        architecture=platform.machine(),
        macos_version=platform.mac_ver()[0],
        free_disk_bytes=shutil.disk_usage(ancestor).free,
        rosetta=observations.get("rosetta"),
        virtual_machine=observations.get("virtual_machine"),
    )


class HandlerSetupPreflight:
    """Shared platform check; no component name enables hidden resource floors."""

    name = "preflight"

    def __init__(
        self,
        *,
        collect: Callable[[Path], ModelSetupMachineFacts] | None = None,
    ) -> None:
        self._collect = collect or collect_machine_facts

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            facts = self._collect(plan.workspace)
            evidence = {
                "platform": facts.platform,
                "architecture": facts.architecture,
                "macos_version": facts.macos_version,
                "rosetta": "unknown"
                if facts.rosetta is None
                else str(facts.rosetta).lower(),
                "virtual_machine": "unknown"
                if facts.virtual_machine is None
                else str(facts.virtual_machine).lower(),
            }
            reasons: list[str] = []
            if facts.platform != "Darwin":
                reasons.append("Run onboarding on macOS 14 or newer.")
            else:
                if int(facts.macos_version.split(".")[0]) < 14:
                    reasons.append("Upgrade to macOS 14 or newer.")
                if facts.architecture not in ("arm64", "x86_64"):
                    reasons.append("Use a supported arm64 or x86_64 Mac architecture.")
                if facts.rosetta:
                    reasons.append("Open a native arm64 terminal instead of Rosetta.")
            if reasons:
                return ModelSetupObservation(
                    step=self.name,
                    status="action_required" if facts.rosetta else "unavailable",
                    evidence=evidence,
                    fix=" ".join(reasons),
                )
            return ModelSetupObservation(
                step=self.name, status="success", evidence=evidence
            )
        except (OSError, ValueError):
            return ModelSetupObservation(
                step=self.name,
                status="failure",
                fix="A platform observation could not complete. Run onex setup --preflight-only from a supported native macOS terminal.",
            )

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        return self.handle(plan)

    def apply(self, plan: ModelSetupPlan) -> None:
        raise ValueError("Resolve the preflight observation before provisioning")
