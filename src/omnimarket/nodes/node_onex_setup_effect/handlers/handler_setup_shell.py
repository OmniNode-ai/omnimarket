# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Survey shell conflicts statically and recover only with invocation consent."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml
from omnibase_core.cli.cli_user_config import user_config_path

from omnimarket.nodes.node_onex_setup_effect import setup_shell_profiles
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    _previous_state,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_shell import (
    ModelSetupProfileSnapshot,
    ModelSetupShellConflict,
    ModelSetupShellPolicy,
)

type ProfileEdit = tuple[ModelSetupProfileSnapshot, tuple[int, ...], str]


class HandlerSetupShell:
    name = "shell-safety"

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        home: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._home = home or Path.home()
        self._onex = onex_home or user_config_path().parent
        self._env = dict(os.environ if environment is None else environment)
        self._policy = ModelSetupShellPolicy.model_validate(
            yaml.safe_load(
                (Path(__file__).parents[1] / "shell_policy.yaml").read_text()
            )
        )
        _, previous = _previous_state(self._onex / "setup.json")
        self._backup_evidence = (
            next(
                (
                    item.evidence.get("recovery_backups", "")
                    for item in previous.observations
                    if item.step == self.name
                ),
                "",
            )
            if previous
            else ""
        )

    def _legacy(self, name: str) -> bool:
        return any(
            re.fullmatch(pattern, name) for pattern in self._policy.legacy_variables
        )

    def _profile_conflicts(
        self, snapshot: ModelSetupProfileSnapshot
    ) -> tuple[list[ModelSetupShellConflict], tuple[int, ...]]:
        conflicts: list[ModelSetupShellConflict] = []
        lines: list[int] = []
        safe = True
        for number, line in enumerate(snapshot.content.decode().splitlines(), 1):
            if (
                not line.strip()
                or line.lstrip().startswith("#")
                or line.startswith(self._policy.set_aside_marker)
            ):
                continue
            try:
                words = shlex.split(line, comments=True)
            except ValueError:
                words = []
                safe = False
            if line.rstrip().endswith("\\") or "<<" in line or re.search(r"[{}]", line):
                safe = False
            assignment = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z_0-9]*)=", line)
            alias = re.match(r"^\s*alias\s+onex=", line)
            function = re.match(r"^\s*(?:function\s+onex\b|onex\s*\(\))", line)
            source = (
                words
                and words[0] in ("source", ".")
                and self._policy.legacy_source_fragment in line
            )
            if (
                function
                or alias
                or source
                or (assignment and self._legacy(assignment[1]))
            ):
                name = (
                    assignment[1]
                    if assignment
                    else "onex"
                    if alias or function
                    else "legacy environment source"
                )
                standalone = (
                    bool(words)
                    and not function
                    and not any(char in line for char in (";", "&", "|", "`", "$("))
                )
                standalone = standalone and len(words) == (
                    2 if words and words[0] in ("export", "alias", "source", ".") else 1
                )
                safe = safe and standalone
                lines.append(number)
                conflicts.append(
                    ModelSetupShellConflict(
                        kind="profile",
                        location=snapshot.path.name,
                        line=number,
                        name=name,
                        recoverable=standalone,
                    )
                )
        if not safe:
            conflicts = [
                item.model_copy(update={"recoverable": False}) for item in conflicts
            ]
        return conflicts, tuple(lines)

    def _survey(
        self, plan: ModelSetupPlan
    ) -> tuple[list[ModelSetupShellConflict], list[ProfileEdit]]:
        conflicts: list[ModelSetupShellConflict] = []
        edits: list[ProfileEdit] = []
        for name, value in self._env.items():
            if self._legacy(name) and value:
                if (
                    name in self._policy.workspace_variables
                    and Path(value).expanduser().absolute() == plan.workspace
                ):
                    continue
                conflicts.append(
                    ModelSetupShellConflict(
                        kind="environment", location="current environment", name=name
                    )
                )
        selected = shutil.which("onex", path=self._env.get("PATH", ""))
        expected = Path(sys.executable).parent / "onex"
        if selected and Path(selected).resolve() != expected.resolve():
            conflicts.append(
                ModelSetupShellConflict(kind="executable", location="PATH", name="onex")
            )
        for filename, shell in self._policy.profiles.items():
            try:
                snapshot = setup_shell_profiles.read_profile(self._home / filename)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                conflicts.append(
                    ModelSetupShellConflict(
                        kind="unsafe-profile",
                        location=filename,
                        name="regular readable profile required",
                    )
                )
                continue
            found, lines = self._profile_conflicts(snapshot)
            conflicts.extend(found)
            if lines:
                edits.append((snapshot, lines, shell))
        return conflicts, edits

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        try:
            conflicts, _ = self._survey(plan)
            if conflicts:
                recoverable = all(item.recoverable for item in conflicts)
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable"
                    if plan.set_aside and recoverable
                    else "action_required",
                    evidence={
                        "conflicts": json.dumps(
                            [item.model_dump() for item in conflicts], sort_keys=True
                        )
                    },
                    fix="Review named shell conflicts. Use --set-aside to preserve and disable safe standalone profile lines; resolve executable, inherited-environment or ambiguous profile conflicts manually. No profile was executed.",
                )
            evidence = {"profile_inspection": "static; no profiles executed"}
            if self._backup_evidence:
                evidence["recovery_backups"] = self._backup_evidence
            return ModelSetupObservation(
                step=self.name, status="success", evidence=evidence
            )
        except (OSError, ValueError):
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Shell inspection could not safely read the selected profile files; repair their permissions or syntax manually.",
            )

    def apply(self, plan: ModelSetupPlan) -> ModelSetupObservation | None:
        before = self.observe(plan)
        if before.status != "unavailable":
            return before
        try:
            conflicts, edits = self._survey(plan)
            if (
                not plan.set_aside
                or not conflicts
                or not all(item.recoverable for item in conflicts)
            ):
                return self.observe(plan)
            backups = setup_shell_profiles.recover_profiles(
                edits, home=self._onex, marker=self._policy.set_aside_marker
            )
            self._backup_evidence = json.dumps(backups)
            return None
        except (OSError, ValueError, subprocess.SubprocessError):
            return ModelSetupObservation(
                step=self.name,
                status="action_required",
                fix="Profile recovery refused: syntax, permissions or content changed. Existing backups remain private; review the profile manually and retry.",
            )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if plan.action != "verify":
            result = self.apply(plan)
            if result is not None:
                return result
        return self.observe(plan)
