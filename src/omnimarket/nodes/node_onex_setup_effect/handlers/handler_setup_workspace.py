# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile the legacy seven-repository proven stack without discarding work.

The infrastructure anchor and its sibling pins are the source of truth. Claude
uses the default-branch commit at the anchor time, as in the audited installer.
Every existing clone is checked before fetching; every clone is checked again
before the first checkout. No reset, clean, forced checkout or deletion is used.
"""

from __future__ import annotations

import re
import stat
from collections.abc import Callable
from pathlib import Path

import yaml

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

WORKSPACE_REPOSITORIES = (
    "omnibase_compat",
    "omnibase_core",
    "omnibase_spi",
    "omnibase_infra",
    "omnimarket",
    "omniclaude",
    "omnidash",
)
PROVEN_INFRA_ANCHOR = "bf256f9660f2520dddb56328d2983f00bafdc910"


def _sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("The proven workspace requires immutable commit pins")
    return value


class HandlerSetupWorkspace:
    """A bounded Git effect; verification only reads the actual repositories."""

    name = "workspace-repositories"

    def __init__(
        self,
        *,
        anchor: str = PROVEN_INFRA_ANCHOR,
        run: Callable[[ModelSetupCommandRequest], ModelSetupCommandResult]
        | None = None,
        remote_url: Callable[[str], str] | None = None,
    ) -> None:
        self._anchor = _sha(anchor)
        self._run = run or HandlerSetupCommand().handle
        self._remote = remote_url or (
            lambda name: f"https://github.com/OmniNode-ai/{name}.git"
        )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        """Contract invocation uses the same adapter as the ordered workflow."""
        if plan.action != "verify":
            self.apply(plan)
        return self.observe(plan)

    def _git(self, root: Path, *args: str, optional: bool = False) -> str:
        result = self._run(
            ModelSetupCommandRequest(
                argv=("git", "-C", str(root), *args),
                timeout_seconds=600,
                environment={"GIT_OPTIONAL_LOCKS": "0"},
            )
        )
        if result.returncode != 0:
            if optional:
                return ""
            raise ValueError(
                "Workspace Git operation failed; existing user work was preserved"
            )
        return result.stdout.strip()

    def _exists(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                "A foreign repository path was preserved; choose a clean workspace"
            )
        return True

    def _repository(self, root: Path, name: str) -> bool:
        if not self._exists(root):
            return False
        if not any(root.iterdir()):
            return False
        top = self._git(root, "rev-parse", "--show-toplevel", optional=True)
        if not top or Path(top).resolve() != root.resolve():
            raise ValueError(
                "A foreign workspace folder is not a repository and was preserved"
            )
        expected = self._remote(name)
        actual = self._git(root, "remote", "get-url", "origin")
        if actual.rstrip("/").removesuffix(".git") != expected.rstrip("/").removesuffix(
            ".git"
        ):
            raise ValueError(
                "The workspace repository origin is foreign and was preserved"
            )
        return True

    def _clean(self, root: Path) -> None:
        if self._git(root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(
                "Workspace has local work; preserve or commit it before repair"
            )
        references = self._git(
            root, "for-each-ref", "--format=%(refname)", "refs/heads"
        ).splitlines()
        for reference in ("HEAD", *references):
            count = self._git(
                root, "rev-list", "--count", reference, "--not", "--remotes=origin"
            )
            if count != "0":
                raise ValueError(
                    "Workspace has local-only commits; preserve them before repair"
                )

    def _default_branch(self, root: Path) -> str:
        reference = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            optional=True,
        )
        if reference.startswith("origin/"):
            return reference
        for branch in ("dev", "main", "master"):
            reference = f"origin/{branch}"
            if self._git(root, "rev-parse", "--verify", reference, optional=True):
                return reference
        raise ValueError("The proven workspace has no observed origin default branch")

    def _targets(self, workspace: Path) -> dict[str, str]:
        infra = workspace / "omnibase_infra"
        _sha(self._git(infra, "rev-parse", "--verify", f"{self._anchor}^{{commit}}"))
        timestamp = self._git(infra, "show", "-s", "--format=%cI", self._anchor)
        raw = self._git(infra, "show", f"{self._anchor}:.github/sibling-pins.yaml")
        if len(raw) > 1024 * 1024:
            raise ValueError("The workspace sibling-pin manifest is too large")
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            raise ValueError("The workspace sibling-pin manifest is invalid") from None
        pins = data.get("pins") if isinstance(data, dict) else None
        if not isinstance(pins, dict):
            raise ValueError("The workspace sibling-pin manifest requires pins")
        targets: dict[str, str] = {"omnibase_infra": self._anchor}
        for name in WORKSPACE_REPOSITORIES:
            if name == "omnibase_infra":
                continue
            root = workspace / name
            # OMN-22477 excludes the CI-only Claude pin from onboarding.
            pin = pins.get(name) if name != "omniclaude" else None
            if pin is not None:
                if not isinstance(pin, str):
                    raise ValueError("The workspace sibling pin is invalid")
                target = _sha(pin)
            else:
                target = _sha(
                    self._git(
                        root,
                        "rev-list",
                        "-1",
                        "--first-parent",
                        f"--before={timestamp}",
                        self._default_branch(root),
                    )
                )
            resolved = _sha(
                self._git(root, "rev-parse", "--verify", f"{target}^{{commit}}")
            )
            if resolved != target:
                raise ValueError(
                    "The workspace pin did not resolve to its immutable commit"
                )
            targets[name] = target
        return targets

    def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
        if not plan.developer:
            return ModelSetupObservation(
                step=self.name,
                status="skipped",
                evidence={"installation": "package", "source_clones": "not selected"},
            )
        try:
            if not plan.workspace.is_dir() or not all(
                self._repository(plan.workspace / name, name)
                for name in WORKSPACE_REPOSITORIES
            ):
                return ModelSetupObservation(
                    step=self.name,
                    status="unavailable",
                    fix="Run onex setup --action repair to acquire the proven workspace repositories.",
                )
            for name in WORKSPACE_REPOSITORIES:
                self._clean(plan.workspace / name)
            targets = self._targets(plan.workspace)
            actual = {
                name: self._git(plan.workspace / name, "rev-parse", "HEAD")
                for name in WORKSPACE_REPOSITORIES
            }
            return ModelSetupObservation(
                step=self.name,
                status="success" if actual == targets else "unavailable",
                evidence={"infra_anchor": self._anchor, **actual},
                fix=None
                if actual == targets
                else "Run onex setup --action repair to reconcile the changed proven pins.",
            )
        except ValueError as exc:
            return ModelSetupObservation(
                step=self.name, status="action_required", fix=str(exc)
            )

    def apply(self, plan: ModelSetupPlan) -> None:
        if not plan.developer:
            return
        if not plan.workspace.is_dir():
            raise ValueError(
                "Create the managed workspace before acquiring repositories"
            )
        existing: list[tuple[str, Path]] = []
        missing: list[tuple[str, Path]] = []
        # Validate every existing folder before cloning/fetching even the first.
        for name in WORKSPACE_REPOSITORIES:
            root = plan.workspace / name
            (existing if self._repository(root, name) else missing).append((name, root))
        for _, root in existing:
            self._clean(root)
        for name, root in missing:
            self._git(
                plan.workspace,
                "clone",
                "--no-checkout",
                "--",
                self._remote(name),
                str(root),
            )
        for name in WORKSPACE_REPOSITORIES:
            self._git(plan.workspace / name, "fetch", "origin")
        infra = plan.workspace / "omnibase_infra"
        if not self._git(
            infra, "rev-parse", "--verify", f"{self._anchor}^{{commit}}", optional=True
        ):
            self._git(infra, "fetch", "origin", self._anchor)
        targets = self._targets(plan.workspace)
        # A --no-checkout fresh clone has an empty index and worktree. It has no
        # user edits to compare; existing clones must all still be clean here.
        for _, root in existing:
            self._clean(root)
        for name in WORKSPACE_REPOSITORIES:
            root = plan.workspace / name
            if self._git(root, "rev-parse", "HEAD") != targets[name] or any(
                n == name for n, _ in missing
            ):
                self._git(root, "checkout", "--detach", targets[name])
        if self.observe(plan).status != "success":
            raise ValueError("The workspace checkout failed actual proven-pin readback")
