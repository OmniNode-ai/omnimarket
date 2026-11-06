# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the union of released and exact-head lab-proven fixes on each tick."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.merge_control.hold_marker import evaluate_merge_hold
from omnimarket.nodes.node_install_superset_guard_effect import (
    HandlerInstallSuperset,
    ModelSupersetRequest,
    SupersetError,
)

from .models import (
    ModelFixHead,
    ModelFixInstallDrop,
    ModelFixInstallOverlay,
    ModelFixInstallReceipt,
    ModelFixInstallRequest,
    ModelFixInstallResult,
    ModelFixInstallTarget,
    ModelFixProofState,
    ModelFixWatcherState,
)

OVERLAY_ID = "node_automatic_fix_install_effect"
SHA = re.compile(r"[0-9a-f]{40}")
Runner = Callable[[tuple[str, ...], int], subprocess.CompletedProcess[str]]


class FixInstallError(RuntimeError):
    """A failed prerequisite. Its code is safe to put in a terminal receipt."""


def run_command(
    argv: tuple[str, ...], timeout: int
) -> subprocess.CompletedProcess[str]:
    # A hosting lane/hook may carry GIT_DIR for its own repository. An installer
    # or git -C must address the declared clone, never that ambient worktree.
    return subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=scrub_git_location_env(os.environ),
    )


class GitFixSuperset:
    """Compose commits without checking out, changing an index, fetching or pushing.

    Installed changes are the first parent. A conflicting incoming head is left
    out as one whole fix; independent subsequent heads can still be installed.
    """

    def __init__(self, clone: Path) -> None:
        self.clone = clone

    def git(
        self, *args: str, ok: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        result = run_command(("git", "-C", str(self.clone), *args), 120)
        if result.returncode not in ok:
            raise FixInstallError("git-operation-failed:" + args[0])
        return result

    def resolve(self, ref: str) -> str:
        value = self.git(
            "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"
        ).stdout.strip()
        if SHA.fullmatch(value) is None:
            raise FixInstallError("revision-unreadable")
        return value

    def ancestor(self, older: str, newer: str) -> bool:
        return (
            self.git("merge-base", "--is-ancestor", older, newer, ok=(0, 1)).returncode
            == 0
        )

    def merge(
        self, previous: str, fix: ModelFixHead
    ) -> tuple[str, ModelFixInstallDrop | None]:
        head = self.resolve(fix.head)
        if self.ancestor(head, previous):
            return previous, None
        if self.ancestor(previous, head):
            return head, None
        merged = self.git(
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            previous,
            head,
            ok=(0, 1),
        )
        lines = merged.stdout.splitlines()
        if merged.returncode == 1:
            return previous, ModelFixInstallDrop(
                fix=fix.fix, head=head, paths=tuple(p for p in lines[1:] if p)
            )
        if not lines or SHA.fullmatch(lines[0]) is None:
            raise FixInstallError("merge-tree-unreadable")
        when = self.git("show", "-s", "--format=%cI", head).stdout.strip()
        identity = {
            f"GIT_{role}_{key}": value
            for role in ("AUTHOR", "COMMITTER")
            for key, value in (
                ("NAME", "ONEX Fix Installer"),
                ("EMAIL", "fix-installer@example.invalid"),
                ("DATE", when),
            )
        }
        commit = subprocess.run(
            [
                "git",
                "-C",
                str(self.clone),
                "-c",
                "commit.gpgsign=false",
                "commit-tree",
                lines[0],
                "-p",
                previous,
                "-p",
                head,
                "-m",
                f"fix-install: include {fix.fix} at {head}",
            ],
            env={**scrub_git_location_env(os.environ), **identity},
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if commit.returncode or SHA.fullmatch(commit.stdout.strip()) is None:
            raise FixInstallError("superset-commit-failed")
        return commit.stdout.strip(), None


class HandlerAutomaticFixInstall:
    """A host tick has no model and accepts no execution route from its caller."""

    def __init__(
        self,
        *,
        overlay: ModelFixInstallOverlay | None = None,
        run: Runner = run_command,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.overlay = overlay
        self.run = run
        self.clock = clock

    def sources(
        self, overlay: ModelFixInstallOverlay
    ) -> tuple[ModelFixWatcherState, ModelFixProofState]:
        watcher = ModelFixWatcherState.model_validate_json(
            overlay.watcher_state.read_text()
        )
        proof = ModelFixProofState.model_validate_json(overlay.proof_state.read_text())
        now = self.clock()
        if watcher.last_tick.tzinfo is None or now.tzinfo is None:
            raise FixInstallError("watcher-time-unreadable")
        age = (now - watcher.last_tick).total_seconds()
        if age < 0 or age > overlay.max_snapshot_age_seconds:
            raise FixInstallError("watcher-snapshot-stale-or-future")
        return watcher, proof

    def heads(
        self,
        target: ModelFixInstallTarget,
        watcher: ModelFixWatcherState,
        proofs: ModelFixProofState,
    ) -> tuple[ModelFixHead, ...]:
        heads: list[ModelFixHead] = []
        for name, record in sorted(watcher.prs.items()):
            facts = record.facts
            if (
                facts is None
                or facts.repo != target.repository
                or name.partition("#")[0] != target.repository
            ):
                continue
            if (
                facts.state != "OPEN"
                or facts.draft is not False
                or facts.labels is None
            ):
                continue
            if not facts.title.lower().startswith(("fix:", "fix(")):
                continue
            hold = evaluate_merge_hold(title=facts.title, labels=facts.labels)
            if not hold.is_merge_eligible or hold.unobserved_sources:
                continue
            key = f"{name}@{facts.head_sha}"
            proof = proofs.passes.get(key)
            if (
                proof is None
                or key in proofs.fails
                or SHA.fullmatch(facts.head_sha) is None
            ):
                continue
            if proof.at.tzinfo is None or proof.at > self.clock():
                continue
            heads.append(ModelFixHead(fix=name, head=facts.head_sha))
        return tuple(heads)

    def installed(
        self, target: ModelFixInstallTarget, overlay: ModelFixInstallOverlay
    ) -> str:
        read = self.run(target.installed_command, overlay.command_timeout_seconds)
        revision = read.stdout.strip()
        if read.returncode or SHA.fullmatch(revision) is None:
            raise FixInstallError("installed-revision-unreadable")
        return revision

    @staticmethod
    def command(template: tuple[str, ...], revision: str) -> tuple[str, ...]:
        if not any("{revision}" in arg for arg in template):
            raise FixInstallError("command-missing-revision-binding")
        return tuple(arg.replace("{revision}", revision) for arg in template)

    def target(
        self,
        target: ModelFixInstallTarget,
        overlay: ModelFixInstallOverlay,
        request: ModelFixInstallRequest,
    ) -> ModelFixInstallReceipt:
        receipt = ModelFixInstallReceipt(
            key=target.key,
            host=target.host,
            surface=target.surface,
            repository=target.repository,
            status="refused",
        )
        try:
            watcher, proofs = self.sources(overlay)
            fixes = self.heads(target, watcher, proofs)
            git = GitFixSuperset(target.clone)
            previous = git.resolve(self.installed(target, overlay))
            desired = previous
            included: list[ModelFixHead] = []
            dropped: list[ModelFixInstallDrop] = []
            released = ModelFixHead(
                fix="released:" + target.released_ref,
                head=git.resolve(target.released_ref),
            )
            for fix in (released, *fixes):
                desired, drop = git.merge(desired, fix)
                if drop is not None:
                    dropped.append(drop)
                else:
                    included.append(fix)
            receipt = receipt.model_copy(
                update={
                    "previous": previous,
                    "desired": desired,
                    "included": tuple(included),
                    "dropped": tuple(dropped),
                }
            )
            # The existing guard owns containment, including installed branch commits.
            guard = HandlerInstallSuperset().handle(
                ModelSupersetRequest(
                    repo=target.clone,
                    new_commit=desired,
                    live_commit=previous,
                    lane=OVERLAY_ID,
                    main_ref=target.released_ref,
                )
            )
            if guard.verdict != "contained":
                raise FixInstallError("superset-guard-refused")
            install = self.command(target.install_command, desired)
            verify = self.command(target.verify_command, desired)
            if desired == previous:
                if request.apply:
                    observation = self.run(verify, overlay.command_timeout_seconds)
                    if observation.returncode:
                        return receipt.model_copy(
                            update={
                                "status": "failed",
                                "installed": previous,
                                "reason": f"observation-exit-{observation.returncode}",
                            }
                        )
                return receipt.model_copy(
                    update={
                        "status": "current",
                        "installed": previous,
                        "observed": "verification command exited 0 at " + previous
                        if request.apply
                        else "",
                    }
                )
            if not request.apply:
                return receipt.model_copy(update={"status": "planned"})
            # A moved branch, withdrawn proof/hold, released ref or outside installer
            # invalidates this plan. The next tick builds a fresh plan instead.
            current_watcher, current_proofs = self.sources(overlay)
            if (
                self.heads(target, current_watcher, current_proofs) != fixes
                or git.resolve(target.released_ref) != released.head
            ):
                raise FixInstallError("sources-changed-before-install")
            if self.installed(target, overlay) != previous:
                raise FixInstallError("installed-revision-changed-before-install")
            # Keep derived objects reachable for the existing local/remote installer.
            git.git(
                "update-ref", f"refs/onex/fix-installs/{target.key}/{desired}", desired
            )
            pending = overlay.state_dir / f"{target.key}.pending.json"
            pending.write_text(
                receipt.model_copy(update={"status": "planned"}).model_dump_json()
                + "\n"
            )
            outcome = self.run(install, overlay.command_timeout_seconds)
            if outcome.returncode:
                return receipt.model_copy(
                    update={
                        "status": "failed",
                        "reason": f"installer-exit-{outcome.returncode}",
                    }
                )
            readback = self.installed(target, overlay)
            receipt = receipt.model_copy(update={"installed": readback})
            if readback != desired:
                raise FixInstallError("installed-revision-mismatch")
            observation = self.run(verify, overlay.command_timeout_seconds)
            if observation.returncode:
                return receipt.model_copy(
                    update={
                        "status": "failed",
                        "reason": f"observation-exit-{observation.returncode}",
                    }
                )
            return receipt.model_copy(
                update={
                    "status": "installed",
                    "observed": "verification command exited 0 at " + desired,
                }
            )
        except (
            FixInstallError,
            SupersetError,
            OSError,
            subprocess.SubprocessError,
            ValidationError,
        ) as exc:
            reason = (
                str(exc) if isinstance(exc, FixInstallError) else type(exc).__name__
            )
            return receipt.model_copy(update={"status": "refused", "reason": reason})

    def handle(self, request: ModelFixInstallRequest) -> ModelFixInstallResult:
        result = ModelFixInstallResult(
            request_id=request.request_id, at=self.clock(), status="refused"
        )
        try:
            overlay = load_structured_overlay(
                OVERLAY_ID,
                ModelFixInstallOverlay,
                explicit=self.overlay,
                file_env="ONEX_FIX_INSTALL_OVERLAY",
            )
            paths = (
                overlay.state_dir,
                overlay.watcher_state,
                overlay.proof_state,
                *(target.clone for target in overlay.targets),
            )
            if any(not path.is_absolute() for path in paths):
                raise FixInstallError("overlay-paths-must-be-absolute")
            if len({target.key for target in overlay.targets}) != len(overlay.targets):
                raise FixInstallError("duplicate-target-key")
            overlay.state_dir.mkdir(parents=True, exist_ok=True)
            with (overlay.state_dir / "tick.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return result.model_copy(update={"status": "locked"})
                self.sources(overlay)
                outcomes: list[ModelFixInstallReceipt] = []
                for target in overlay.targets:
                    receipt = self.target(target, overlay, request)
                    outcomes.append(receipt)
                    if request.apply:
                        temporary = (
                            overlay.state_dir / f".{target.key}.{os.getpid()}.json"
                        )
                        try:
                            temporary.write_text(receipt.model_dump_json() + "\n")
                            temporary.replace(overlay.state_dir / f"{target.key}.json")
                        finally:
                            temporary.unlink(missing_ok=True)
                        (overlay.state_dir / f"{target.key}.pending.json").unlink(
                            missing_ok=True
                        )
                receipts = tuple(outcomes)
                partial = any(
                    r.status in {"failed", "refused"} or r.dropped for r in receipts
                )
                result = result.model_copy(
                    update={
                        "status": "partial" if partial else "completed",
                        "targets": receipts,
                    }
                )
                if request.apply:
                    temporary = overlay.state_dir / f".last-run.{os.getpid()}.json"
                    try:
                        temporary.write_text(result.model_dump_json() + "\n")
                        temporary.replace(overlay.state_dir / "last-run.json")
                    finally:
                        temporary.unlink(missing_ok=True)
                return result
        except (
            FixInstallError,
            ValueError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            reason = (
                str(exc) if isinstance(exc, FixInstallError) else type(exc).__name__
            )
            return result.model_copy(update={"findings": (reason,)})
