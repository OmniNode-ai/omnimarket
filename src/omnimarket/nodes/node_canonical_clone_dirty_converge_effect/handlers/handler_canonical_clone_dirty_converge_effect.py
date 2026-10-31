# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCanonicalCloneDirtyConverge -- converge a canonical clone that has been refused for
tracked changes for a declared age (OMN-21821).

The canonical-clone sync engine only fast-forwards a clean clone and refuses the rest. Nothing
escalated that refusal: a host's omniclaude sat 120 commits behind under four staged files for
eight days, and an omnidash clone logged REFUSED_TOO_LONG for 143103 s over a stale generated file.

One tick of this handler runs the engine with its refusal bound set to the declared age. The engine
then names each clone refused that long (``REFUSED_TOO_LONG <clone> refused for <n>s: <reason>``)
and this handler acts on exactly those clones, in this order:

1. every dirty tracked path is classified. A path that looks generated, or whose added lines
   carry a host value or a credential, is kept only in the converge archive and never pushed. Every
   other path is source;
2. the source paths are committed on top of the clone's HEAD in a throwaway index, so the clone's
   own index, HEAD and refs are untouched, and that commit is pushed to a rescue branch. When the
   push fails nothing else happens and the clone stays refused;
3. converge-canonical-clone.sh preserves the whole tree again, resets the clone to its upstream and
   writes the one ledger row, which names the rescue branch and the archive-only paths (``--note``).

A clone that is ahead of or diverged from its upstream, has an operation in progress, or is not on a
tracking branch is left alone: converging it would lose commits, not edits.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import click

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    EnumDirtyConvergeStatus,
    ModelDirtyConvergeDeclaration,
    ModelDirtyConvergeOutcome,
    ModelDirtyConvergeTickReceipt,
    ModelDirtyConvergeTickRequest,
)

NODE = "node_canonical_clone_dirty_converge_effect"
AGE_ENV = "ONEX_CLONE_DIRTY_CONVERGE_AFTER_S"
ENGINE_BOUND_ENV = "ONEX_CLONE_REFUSAL_BOUND_S"
_TIMEOUTS = load_node_timeouts(NODE)
_BREACH = re.compile(
    r"^REFUSED_TOO_LONG (?P<clone>\S+) refused for (?P<age>\d+)s:(?P<why>.*)$"
)
_IN_PROGRESS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "BISECT_LOG",
    "index.lock",
)
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

Run = Callable[..., Any]


def load_declaration() -> ModelDirtyConvergeDeclaration:
    section = load_packaged_contract_section(NODE, "dirty_converge")
    return ModelDirtyConvergeDeclaration.model_validate(section)


def declared_age_seconds(env: Mapping[str, str]) -> float:
    """The contract's age, replaced by the host overlay variable when it holds a positive number."""
    try:
        override = float(env.get(AGE_ENV, ""))
    except ValueError:
        override = 0.0
    return override if override > 0 else _TIMEOUTS.get("dirty_converge_after_seconds")


class HandlerCanonicalCloneDirtyConvergeEffect:
    """One tick: run the engine, then save and converge every clone it reports refused too long."""

    def __init__(
        self,
        registry_root: Path,
        *,
        env: Mapping[str, str],
        declaration: ModelDirtyConvergeDeclaration | None = None,
        run: Run = subprocess.run,
        engine_argv: Sequence[str] | None = None,
    ) -> None:
        self.registry_root = registry_root
        self._env = dict(env)
        self.declaration = declaration or load_declaration()
        self._run = run
        self.age = declared_age_seconds(env)
        self._engine_argv = list(
            engine_argv
            or [
                sys.executable,
                str(registry_root / self.declaration.engine),
                "sync",
                "--trigger",
                "timer",
            ]
        )
        self._patterns = {
            "host": [re.compile(p) for p in self.declaration.host_value_patterns],
            "path": [re.compile(p) for p in self.declaration.generated_path_patterns],
            "marker": [re.compile(p) for p in self.declaration.generated_markers],
        }

    # -- git ----------------------------------------------------------------------------------
    def _git(
        self,
        clone: Path,
        *args: str,
        timeout: float = 60,
        extra_env: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        env = {k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV}
        env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(extra_env or {})})
        try:
            done = self._run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, f"{type(exc).__name__}: {exc}"
        text = (
            (done.stdout or "").strip()
            if done.returncode == 0
            else (done.stderr or done.stdout or "").strip()
        )
        return done.returncode, text

    def _out(self, clone: Path, *args: str) -> str:
        code, out = self._git(clone, *args)
        return out if code == 0 else ""

    def _dirty_paths(self, clone: Path) -> list[str]:
        env = {k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV}
        done = self._run(
            [
                "git",
                "-C",
                str(clone),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if done.returncode:
            return []
        paths: list[str] = []
        entries = [e for e in done.stdout.split("\0") if e]
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            if entry[0] in "RC":
                index += 1  # the rename source follows as its own field
            paths.append(entry[3:])
        return paths

    # -- classification -----------------------------------------------------------------------
    def classify(
        self, clone: Path, paths: Iterable[str]
    ) -> tuple[list[str], dict[str, str]]:
        """(source paths to push, {archive-only path: why})."""
        source: list[str] = []
        archive_only: dict[str, str] = {}
        for path in sorted(set(paths)):
            _, diff = self._git(clone, "diff", "HEAD", "--no-color", "--", path)
            added = "\n".join(
                line[1:]
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            try:
                working = (clone / path).read_text(errors="ignore")[:2000]
            except OSError:
                working = ""
            committed = self._out(clone, "show", f"HEAD:{path}")[:2000]
            if any(p.search(added) for p in self._patterns["host"]):
                archive_only[path] = "carries a host value"
            elif any(p.search(path) for p in self._patterns["path"]) or any(
                p.search(text)
                for p in self._patterns["marker"]
                for text in (working, committed)
            ):
                archive_only[path] = "generated file"
            else:
                source.append(path)
        return source, archive_only

    def _rescue_commit(
        self, clone: Path, git_dir: Path, paths: Sequence[str]
    ) -> str | None:
        """A commit on HEAD holding the working-tree content of ``paths``, built in a throwaway
        index so the clone's own index, HEAD and refs are untouched."""
        index = git_dir / f"dirty-converge-index.{os.getpid()}"
        idx = {"GIT_INDEX_FILE": str(index)}
        who = self.declaration.lane
        ident = {
            "GIT_AUTHOR_NAME": who,
            "GIT_AUTHOR_EMAIL": f"{who}@users.noreply.github.com",
            "GIT_COMMITTER_NAME": who,
            "GIT_COMMITTER_EMAIL": f"{who}@users.noreply.github.com",
        }
        try:
            if self._git(clone, "read-tree", "HEAD", extra_env=idx)[0]:
                return None
            for path in paths:
                full = clone / path
                if not full.exists() and not full.is_symlink():
                    code, _ = self._git(
                        clone,
                        "update-index",
                        "--force-remove",
                        "--",
                        path,
                        extra_env=idx,
                    )
                else:
                    code, blob = self._git(clone, "hash-object", "-w", "--", str(full))
                    if code:
                        return None
                    mode = (
                        "120000"
                        if full.is_symlink()
                        else ("100755" if os.access(full, os.X_OK) else "100644")
                    )
                    code, _ = self._git(
                        clone,
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{mode},{blob},{path}",
                        extra_env=idx,
                    )
                if code:
                    return None
            code, tree = self._git(clone, "write-tree", extra_env=idx)
            if code:
                return None
            code, commit = self._git(
                clone,
                "commit-tree",
                tree,
                "-p",
                "HEAD",
                "-m",
                "Rescue: tracked changes saved before the canonical clone was converged",
                extra_env={**idx, **ident},
            )
            return commit if code == 0 else None
        finally:
            with contextlib.suppress(OSError):
                index.unlink()

    # -- one clone ----------------------------------------------------------------------------
    def converge_clone(
        self, clone: Path, refused_for: int
    ) -> ModelDirtyConvergeOutcome:
        def outcome(
            status: EnumDirtyConvergeStatus, reason: str, **extra: Any
        ) -> ModelDirtyConvergeOutcome:
            return ModelDirtyConvergeOutcome(
                clone=str(clone),
                status=status,
                reason=reason,
                refused_for_seconds=refused_for,
                **extra,
            )

        skipped = EnumDirtyConvergeStatus.SKIPPED
        failed = EnumDirtyConvergeStatus.FAILED
        branch = self._out(clone, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch not in self.declaration.tracking_branches:
            return outcome(
                skipped,
                f"checked out on {branch or 'a detached HEAD'}, not a tracking branch",
            )
        remote = self._out(clone, "config", "--get", f"branch.{branch}.remote")
        merge_ref = self._out(clone, "config", "--get", f"branch.{branch}.merge")
        if not remote or not merge_ref.startswith("refs/heads/"):
            return outcome(skipped, f"branch {branch} has no remote upstream")
        before = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD")
        target = self._out(
            clone,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/{remote}/{merge_ref[11:]}",
        )
        git_dir_text = self._out(clone, "rev-parse", "--absolute-git-dir")
        if not (before and target and git_dir_text):
            return outcome(skipped, "HEAD or the upstream tip does not resolve")
        git_dir = Path(git_dir_text)
        if before == target:
            return outcome(skipped, "already at the upstream tip")
        if any((git_dir / marker).exists() for marker in _IN_PROGRESS):
            return outcome(skipped, "a git operation is in progress in the clone")
        if self._git(clone, "merge-base", "--is-ancestor", before, target)[0]:
            return outcome(
                skipped,
                "the clone is ahead of or diverged from its upstream: commits would be lost",
            )
        dirty = self._dirty_paths(clone)
        if not dirty:
            return outcome(skipped, "no tracked changes: the refusal has another cause")
        source, archive_only = self.classify(clone, dirty)

        saved: list[str] = []
        branch_name = ""
        if source:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            slug = re.sub(r"[^A-Za-z0-9._-]", "-", clone.name)
            host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname().split(".")[0])
            branch_name = f"{self.declaration.rescue_branch_prefix}{host}-{slug}-dirty-converge-{stamp}"
            commit = self._rescue_commit(clone, git_dir, source)
            if commit is None:
                return outcome(failed, "the rescue commit could not be built")
            code, text = self._git(
                clone,
                "push",
                remote,
                f"{commit}:refs/heads/{branch_name}",
                timeout=_TIMEOUTS.get("dirty_converge_rescue_push_seconds"),
            )
            if code:
                return outcome(failed, f"the rescue push failed: {text[-300:]}")
            saved.append(
                f"{len(source)} source path(s) pushed to {remote} branch {branch_name} ({', '.join(source[:5])})"
            )
        if archive_only:
            listing = ", ".join(
                f"{p} ({why})" for p, why in sorted(archive_only.items())[:5]
            )
            saved.append(
                f"{len(archive_only)} path(s) kept only in the converge archive, never pushed: {listing}"
            )
        script = self.registry_root / self.declaration.converge_script
        try:
            done = self._run(
                [
                    "/bin/bash",
                    str(script),
                    str(clone),
                    "--execute",
                    "--ticket",
                    self.declaration.ticket,
                    "--lane",
                    self.declaration.lane,
                    "--note",
                    "; ".join(saved),
                ],
                capture_output=True,
                text=True,
                env={k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV},
                timeout=_TIMEOUTS.get("dirty_converge_script_seconds"),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return outcome(
                failed,
                f"converge could not run: {type(exc).__name__}: {exc}",
                rescue_branch=branch_name,
            )
        head = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD")
        if done.returncode or head != target:
            tail = " ".join((done.stderr or done.stdout or "").split())[-300:]
            return outcome(
                failed,
                f"converge-canonical-clone.sh exit {done.returncode}: {tail}",
                rescue_branch=branch_name,
            )
        return outcome(
            EnumDirtyConvergeStatus.CONVERGED,
            "; ".join(saved),
            rescue_branch=branch_name,
            source_paths=tuple(source),
            archive_only_paths=archive_only,
        )

    # -- the tick -----------------------------------------------------------------------------
    def handle(
        self, request: ModelDirtyConvergeTickRequest
    ) -> ModelDirtyConvergeTickReceipt:
        """One tick for the registry root the handler was composed with."""
        return ModelDirtyConvergeTickReceipt(
            after_seconds=int(self.age), outcomes=tuple(self.tick())
        )

    def tick(self) -> list[ModelDirtyConvergeOutcome]:
        env = {k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV}
        env[ENGINE_BOUND_ENV] = str(int(self.age))
        env["OMNI_HOME"] = str(self.registry_root)
        try:
            done = self._run(
                self._engine_argv,
                capture_output=True,
                text=True,
                timeout=_TIMEOUTS.get("dirty_converge_engine_seconds"),
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return [
                ModelDirtyConvergeOutcome(
                    clone=str(self.registry_root),
                    status=EnumDirtyConvergeStatus.FAILED,
                    reason=f"the sync engine did not run: {type(exc).__name__}: {exc}",
                    refused_for_seconds=0,
                )
            ]
        sys.stdout.write(done.stdout or "")
        outcomes = []
        for line in (done.stdout or "").splitlines():
            match = _BREACH.match(line)
            if match:
                outcomes.append(
                    self.converge_clone(Path(match["clone"]), int(match["age"]))
                )
        return outcomes


def registry_root_from(env: Mapping[str, str]) -> Path:
    if not env.get("OMNI_HOME"):
        raise click.UsageError("set OMNI_HOME (there is no default registry root)")
    return Path(env["OMNI_HOME"])


@click.command("dirty-converge")
def dirty_converge_command() -> None:
    """Sync every canonical clone, then save and converge each one refused past the declared age."""
    handler = HandlerCanonicalCloneDirtyConvergeEffect(
        registry_root_from(os.environ), env=os.environ
    )
    outcomes = handler.tick()
    for item in outcomes:
        click.echo(
            f"{item.status.value.upper():9} {item.clone} refused {item.refused_for_seconds}s: {item.reason}"
        )
    if any(item.status is EnumDirtyConvergeStatus.FAILED for item in outcomes):
        sys.exit(1)


def main() -> None:
    dirty_converge_command()


if __name__ == "__main__":
    main()
