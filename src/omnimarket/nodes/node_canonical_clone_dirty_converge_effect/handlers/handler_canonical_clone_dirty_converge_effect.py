# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCanonicalCloneDirtyConvergeEffect -- converge a canonical clone that has been refused for
tracked changes for a declared age (OMN-21821).

The canonical-clone sync engine only fast-forwards a clean clone and refuses the rest. Nothing
escalated that refusal: a host's omniclaude sat 120 commits behind under four staged files for
eight days, and an omnidash clone logged REFUSED_TOO_LONG for 143103 s over a stale generated file.

One tick runs the engine with its refusal bound set to the declared age. The engine then names each
clone refused that long (``REFUSED_TOO_LONG <clone> refused for <n>s: <reason>``) and this handler
converges exactly those clones to their upstream and DROPS what they held: no rescue branch, no pin,
no tar (operator ruling 2026-10-31 about 20:00Z; the work is redone from its ticket).
converge-canonical-clone.sh writes the one ledger row, which names the dropped paths and local commits
and the head the clone was converged to (``--note``).

Left alone: a clone a live lane is using (an open CLAIM naming its path, or a process whose working
directory is inside it, or a host where that cannot be told), a clone off a tracking branch, one with
an operation in progress, and one with no tracked changes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
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

    # -- live use -----------------------------------------------------------------------------
    def _open_claim_naming(self, clone: Path) -> str:
        """The lane of an open CLAIM that names the clone's path, else an empty string."""
        ledger = self._env.get("ONEX_LEDGER_PATH", "")
        if not ledger:
            return ""
        try:
            lines = (
                Path(ledger).read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            return ""
        open_claims: dict[str, str] = {}
        for line in lines:
            parts = [p.strip() for p in line.split(" | ", 3)]
            if len(parts) < 3 or not parts[2].startswith("lane="):
                continue
            lane = parts[2][5:]
            if parts[1] == "CLAIM":
                open_claims[lane] = line
            elif parts[1] == "TERMINAL":
                open_claims.pop(lane, None)
        named = re.compile(
            "("
            + re.escape(str(clone))
            + r"|\$OMNI_HOME/"
            + re.escape(clone.name)
            + ")(?![\\w.-])"
        )
        return next(
            (lane for lane, text in open_claims.items() if named.search(text)), ""
        )

    def _process_inside(self, clone: Path) -> str:
        """A process whose working directory is inside the clone; 'unknown' when lsof cannot say."""
        proc = Path("/proc")
        if (proc / "self" / "cwd").exists():
            root = str(clone.resolve())
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    cwd = os.readlink(entry / "cwd")
                except OSError:
                    continue  # another user's process, or one that just exited
                if cwd == root or cwd.startswith(root + "/"):
                    return f"pid {entry.name}"
            return ""
        try:
            done = self._run(
                ["lsof", "-d", "cwd", "-F", "pn"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if done.returncode not in (0, 1):
            return "unknown"
        root = str(clone.resolve())
        pid = ""
        for line in (done.stdout or "").splitlines():
            if line.startswith("p"):
                pid = line[1:]
            elif line.startswith("n") and (
                line[1:] == root or line[1:].startswith(root + "/")
            ):
                return f"pid {pid}"
        return ""

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
        upstream = f"refs/remotes/{remote}/{merge_ref[11:]}"
        target = self._out(clone, "rev-parse", "--verify", "--quiet", upstream)
        git_dir_text = self._out(clone, "rev-parse", "--absolute-git-dir")
        if not (before and target and git_dir_text):
            return outcome(skipped, "HEAD or the upstream tip does not resolve")
        if before == target:
            return outcome(skipped, "already at the upstream tip")
        if any((Path(git_dir_text) / marker).exists() for marker in _IN_PROGRESS):
            return outcome(skipped, "a git operation is in progress in the clone")
        dirty = self._dirty_paths(clone)
        commits = [
            c
            for c in self._out(
                clone, "log", "--format=%h", f"{target}..HEAD"
            ).splitlines()
            if c
        ]
        if not dirty:
            return outcome(skipped, "no tracked changes: the refusal has another cause")
        lane = self._open_claim_naming(clone)
        if lane:
            return outcome(skipped, f"an open CLAIM of lane {lane} names the clone")
        inside = self._process_inside(clone)
        if inside:
            return outcome(skipped, f"a process is using the clone ({inside})")

        listed = self.declaration.max_listed

        def named(items: Sequence[str]) -> str:
            more = f" and {len(items) - listed} more" if len(items) > listed else ""
            return ", ".join(items[:listed]) + more

        dropped = f"{len(dirty)} tracked path(s) ({named(dirty)})"
        if commits:
            dropped += f" and {len(commits)} local commit(s) ({named(commits)})"
        note = f"dropped, not saved: {dropped}; converged to {target[:12]}"
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
                    note,
                ],
                capture_output=True,
                text=True,
                env={k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV},
                timeout=_TIMEOUTS.get("dirty_converge_script_seconds"),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return outcome(
                failed, f"converge could not run: {type(exc).__name__}: {exc}"
            )
        head = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD")
        if done.returncode or head != target:
            tail = " ".join((done.stderr or done.stdout or "").split())[-300:]
            return outcome(
                failed, f"converge-canonical-clone.sh exit {done.returncode}: {tail}"
            )
        return outcome(
            EnumDirtyConvergeStatus.CONVERGED,
            note,
            dropped_paths=tuple(dirty),
            dropped_commits=tuple(commits),
            converged_to=target,
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
