# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for planted overlays, declarations and registry trees (OMN-20804)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.handlers.handler_automation_liveness_overlay_check_cli import (
    collect_files,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.handlers import (
    HandlerAutomationLivenessOverlayCheck,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    ModelAutomationLivenessOverlayCheckRequest,
    ModelAutomationLivenessOverlayCheckResult,
    ModelAutomationLivenessScannedFile,
)

REPOSITORY = "omnibase_infra"


def entry_yaml(
    name: str = "onex-planted",
    *,
    native_id: str | None = None,
    kind: str = "systemd-timer",
    state: str = "active",
    drop: tuple[str, ...] = (),
    **overrides: str,
) -> str:
    """A valid systemd-timer entry as overlay YAML; ``drop`` removes keys, ``overrides`` replace."""
    fields = {
        "process_id": f"h201/{name}",
        "owner_repo": REPOSITORY,
        "host": "h201",
        "trigger": f"{{kind: {kind}, native_id: {native_id or name + '.timer'}}}",
        "expected_interval_seconds": "600",
        "max_runtime_seconds": "300",
        "alarm_deadline_seconds": "1620",
        "emitter": "observer",
        "run_record": "per-run",
        "evidence": f"{{source: systemd_journal, locator: {name}.service}}",
        "real_work": "units converged",
        "demand": "none",
        "positive_control": f'{{kind: class_drill, ref: "class-drill/{kind}"}}',
        "state": state,
    }
    fields.update(overrides)
    lines = [f"  {key}: {value}" for key, value in fields.items() if key not in drop]
    return "- " + "\n".join(lines).lstrip()


def overlay_yaml(*entries: str) -> str:
    body = "\n".join(entries)
    return f"schema_version: automation-liveness-overlay/v1\nprocesses:\n{body}\n"


def sources_yaml(
    *sources: tuple[str, str, tuple[str, ...]],
    repositories: tuple[str, ...] = (REPOSITORY,),
) -> str:
    """A declaration naming ``repositories`` and, per source, (repository, path, installs)."""
    lines = ["schema_version: automation-liveness-sources/v1", "repositories:"]
    lines += [f"  - {name}" for name in repositories]
    if not sources:
        lines.append("sources: []")
    else:
        lines.append("sources:")
        for repository, path, installs in sources:
            lines += [
                f"  - repository: {repository}",
                f"    path: {path}",
                "    installs:",
            ]
            lines += [f"      - {name}" for name in installs]
    return "\n".join(lines) + "\n"


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_check(
    overlay: str,
    sources: str,
    registry: Path,
    repositories: tuple[str, ...] = (REPOSITORY,),
    *,
    complete: bool = True,
) -> ModelAutomationLivenessOverlayCheckResult:
    """Scan ``registry/<repository>`` for each repository that exists and check."""
    scanned: list[str] = []
    files: list[ModelAutomationLivenessScannedFile] = []
    for repository in repositories:
        root = registry / repository
        if root.is_dir():
            scanned.append(repository)
            files.extend(collect_files(repository, root))
    return HandlerAutomationLivenessOverlayCheck().handle(
        ModelAutomationLivenessOverlayCheckRequest(
            overlay_text=overlay,
            sources_text=sources,
            scanned_repositories=tuple(scanned),
            files=tuple(files),
            complete=complete,
        )
    )


#: A shell installer that writes a launchd agent: the shape a new installer takes.
PLANTED_LAUNCHD_INSTALLER = """#!/bin/bash
set -euo pipefail
cp "$HERE/ai.omninode.planted.plist" "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/ai.omninode.planted.plist"
"""
