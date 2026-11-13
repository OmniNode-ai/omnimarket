# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCanonicalCloneDirtyConvergeEffect -- act on a canonical clone that has been refused for a
declared age, and never discard local work (OMN-21821, OMN-19998).

The canonical-clone sync engine only fast-forwards a clean clone and refuses the rest. Nothing
escalated that refusal: a host's omniclaude sat 120 commits behind under four staged files for
eight days, and an omnidash clone logged REFUSED_TOO_LONG for 143103 s over a stale generated file.

One tick runs the engine with its refusal bound set to the declared age. The engine then names each
clone refused that long (``REFUSED_TOO_LONG <clone> refused for <n>s: <reason>``). For each of those
the handler first reads the clone for local work: a staged or modified tracked file, an untracked
file, or a commit no remote has. A clone that holds any is REFUSED: nothing runs, the clone stays
byte for byte as found, the outcome names the clone and the kind of work, and the tick raises one
owned alarm for it through the lab alarm delivery. Earlier versions converged such a clone with
``converge-canonical-clone.sh`` (``git reset --hard``) and dropped what it held: the Mac's registry-root
clone (one unpushed commit, staged files) and its knowledge base clone (280 uncommitted lines and
untracked files) would have been lost by the declared tick.

Only a clone with no local work is moved, and only to put it back where it belongs: a detached HEAD
or a lane branch returns to its tracking branch, and ``core.bare=true`` on a clone with a working
tree is cleared. A clone that is clean and behind is fast-forwarded by the engine itself.

Left alone (SKIPPED): a clone a live lane is using (an open CLAIM naming its path, or a process whose
working directory is inside it, or a host where that cannot be told), a clone an image build holds,
one with an operation in progress, and one with no local work whose refusal has another cause.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import click

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    EnumDirtyConvergeStatus,
    EnumLocalWorkKind,
    ModelDirtyConvergeDeclaration,
    ModelDirtyConvergeOutcome,
    ModelDirtyConvergeTickReceipt,
    ModelDirtyConvergeTickRequest,
    ModelLocalWork,
)
from omnimarket.nodes.node_lab_alarm.protocols import ProtocolAlarmDelivery
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import (
    load_overlay as load_delivery_overlay,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler as build_delivery_handler,
)

NODE = "node_canonical_clone_dirty_converge_effect"
AGE_ENV = "ONEX_CLONE_DIRTY_CONVERGE_AFTER_S"
DETACHED_AGE_ENV = "ONEX_CLONE_DETACHED_REATTACH_AFTER_S"
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


class _LocalWorkUnreadableError(Exception):
    """A git read of the clone's local work failed; the clone is then treated as holding some."""


#: Outcome.alarm values of a REFUSED clone besides "failed: <why>".
ALARM_DELIVERED = "delivered"
ALARM_SUPPRESSED = "suppressed"
ALARM_NOT_CONFIGURED = "not-configured"
ALARM_FAILED = "failed"


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


def declared_detached_age_seconds(env: Mapping[str, str]) -> float:
    """The contract's detached-HEAD age, replaced by the host overlay variable when it is positive."""
    try:
        override = float(env.get(DETACHED_AGE_ENV, ""))
    except ValueError:
        override = 0.0
    return (
        override if override > 0 else _TIMEOUTS.get("detached_reattach_after_seconds")
    )


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
        delivery: ProtocolAlarmDelivery | None = None,
    ) -> None:
        self.registry_root = registry_root
        self._delivery = delivery
        self._env = dict(env)
        self.declaration = declaration or load_declaration()
        self._run = run
        self.age = declared_age_seconds(env)
        self.detached_age = declared_detached_age_seconds(env)
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

    # -- local work ---------------------------------------------------------------------------
    def _git_raw(self, clone: Path, *args: str) -> tuple[int, str]:
        """One read-only git call whose stdout is returned unstripped (a porcelain entry may start
        with a space, and a failure is a non-zero code, never an empty answer)."""
        env = {k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV}
        env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        try:
            done = self._run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, f"{type(exc).__name__}: {exc}"
        return done.returncode, (
            done.stdout or "" if done.returncode == 0 else done.stderr or ""
        )

    def _commits(self, clone: Path, *rev_args: str) -> list[str]:
        code, out = self._git_raw(clone, "log", "--format=%h", *rev_args)
        if code:
            raise _LocalWorkUnreadableError(
                f"git log {' '.join(rev_args)}: {out.strip()}"
            )
        return [line for line in out.splitlines() if line]

    def _local_work(
        self, clone: Path, *, upstream: str = "", bare: bool = False
    ) -> tuple[ModelLocalWork, ...]:
        """Everything in the clone that a reset would lose, by kind; empty when the clone holds none.

        Read-only. Staged and modified come from the index and the work tree, untracked from the
        same one status call. Unpushed commits are the commits HEAD holds that the clone's upstream
        tip does not (when one is given), the commits HEAD holds that no remote-tracking ref has, and
        the commits a local tracking branch (the branch a reattach resets) holds beyond its own
        upstream. A read that fails raises, so an unreadable clone is never taken for a clean one.
        """
        # core.bare=true on disk would make git refuse a work-tree command, and -c does not override
        # it at repository discovery; an explicit git dir and work tree read the clone as it is.
        prefix = (
            ("--git-dir", str(clone / ".git"), "--work-tree", str(clone))
            if bare
            else ()
        )
        code, out = self._git_raw(
            clone,
            *prefix,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if code:
            raise _LocalWorkUnreadableError(f"git status: {out.strip()}")
        names: dict[EnumLocalWorkKind, list[str]] = {
            EnumLocalWorkKind.STAGED: [],
            EnumLocalWorkKind.MODIFIED: [],
            EnumLocalWorkKind.UNTRACKED: [],
            EnumLocalWorkKind.UNPUSHED_COMMITS: [],
        }
        entries = [e for e in out.split("\0") if e]
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            x, y, path = entry[0], entry[1], entry[3:]
            if x in "RC":
                index += 1  # the rename source follows as its own field
            if x == "?" and y == "?":
                names[EnumLocalWorkKind.UNTRACKED].append(path)
                continue
            if x not in " ?!":
                names[EnumLocalWorkKind.STAGED].append(path)
            if y not in " ?!":
                names[EnumLocalWorkKind.MODIFIED].append(path)
        commits: list[str] = []
        if upstream:
            commits += self._commits(clone, f"{upstream}..HEAD")
        commits += self._commits(clone, "HEAD", "--not", "--remotes")
        for branch in self.declaration.tracking_branches:
            ref = f"refs/heads/{branch}"
            if not self._out(clone, "rev-parse", "--verify", "--quiet", ref):
                continue
            tracked = self._out(
                clone,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                f"{branch}@{{u}}",
            )
            if tracked:
                commits += self._commits(clone, f"{tracked}..{ref}")
        names[EnumLocalWorkKind.UNPUSHED_COMMITS] = list(dict.fromkeys(commits))
        listed = self.declaration.max_listed
        return tuple(
            ModelLocalWork(kind=kind, count=len(items), names=tuple(items[:listed]))
            for kind, items in names.items()
            if items
        )

    def _refuse_local_work(
        self,
        clone: Path,
        refused_for: int,
        *,
        upstream: str = "",
        bare: bool = False,
    ) -> ModelDirtyConvergeOutcome | None:
        """REFUSED when the clone holds local work, FAILED when it cannot be read, else None.

        Called before every step that could move the clone. A REFUSED clone is left exactly as found.
        """

        def outcome(
            status: EnumDirtyConvergeStatus,
            reason: str,
            work: tuple[ModelLocalWork, ...] = (),
        ) -> ModelDirtyConvergeOutcome:
            return ModelDirtyConvergeOutcome(
                clone=str(clone),
                status=status,
                reason=reason,
                refused_for_seconds=refused_for,
                local_work=work,
            )

        try:
            work = self._local_work(clone, upstream=upstream, bare=bare)
        except _LocalWorkUnreadableError as exc:
            return outcome(
                EnumDirtyConvergeStatus.FAILED,
                f"local work in clone {clone} could not be read, so nothing moved: {exc}"[
                    :1900
                ],
            )
        if not work:
            return None
        listed = self.declaration.max_listed
        kinds = "; ".join(
            f"{item.kind.value} {item.count} ("
            + ", ".join(item.names)
            + (
                f" and {item.count - listed} more"
                if item.count > len(item.names)
                else ""
            )
            + ")"
            for item in work
        )
        return outcome(
            EnumDirtyConvergeStatus.REFUSED,
            f"refused: clone {clone} holds local work: {kinds}; left as found"[:1990],
            work,
        )

    # -- live use -----------------------------------------------------------------------------
    def _live_build_lock(self) -> str:
        """The first declared build lock that is held and younger than the declared age, else ''.

        The deploy agent takes these mkdir locks from staging through its last image build; its holder
        record's time (the directory's, without one) is the age, the reclaim rule the agent applies. A
        holder on this host whose process is gone is reclaimable at once, as the agent reclaims it.
        """
        now = time.time()
        for relative in self.declaration.build_lock_dirs:
            lock = self.registry_root / relative
            if not lock.is_dir():
                continue
            holder = lock / "holder"
            try:
                age = now - (holder if holder.is_file() else lock).stat().st_mtime
            except OSError:
                continue  # an unreadable age is not evidence of a live build
            if age > self.declaration.build_lock_max_age_seconds:
                continue
            if self._holder_is_gone(holder):
                continue
            return lock.name
        return ""

    @staticmethod
    def _holder_is_gone(holder: Path) -> bool:
        """True only when the record names this host and a process that is not running."""
        try:
            fields = dict(
                line.partition("=")[::2]
                for line in holder.read_text(encoding="utf-8").splitlines()
            )
            pid = int(fields.get("pid", ""))
        except (OSError, UnicodeError, ValueError):
            return False  # no readable record proves nothing about the build
        if fields.get("host") != socket.gethostname():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (OSError, OverflowError):
            return False  # PermissionError means alive; other errors prove nothing
        return False

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
        building = self._live_build_lock()
        if building:
            return outcome(
                skipped, f"an image build holds {building}: converged after it ends"
            )
        if self._out(clone, "rev-parse", "--is-bare-repository") == "true":
            return self._repair_bare(clone, refused_for, outcome)
        branch = self._out(clone, "symbolic-ref", "--quiet", "--short", "HEAD")
        if not branch:
            return self._reattach_detached(clone, refused_for, outcome)
        if branch not in self.declaration.tracking_branches:
            # A canonical clone parked on a lane branch serves that branch to every reader and the
            # engine refuses it on every run, so it is returned to its remote default branch the
            # same way a detached HEAD is reattached (OMN-23032).
            return self._reattach_detached(
                clone, refused_for, outcome, parked_on=branch
            )
        if refused_for < self.age:
            return outcome(
                skipped,
                f"refused for {refused_for}s, below the declared {int(self.age)}s",
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
        lane = self._open_claim_naming(clone)
        if lane:
            return outcome(skipped, f"an open CLAIM of lane {lane} names the clone")
        inside = self._process_inside(clone)
        if inside:
            return outcome(skipped, f"a process is using the clone ({inside})")
        refusal = self._refuse_local_work(clone, refused_for, upstream=upstream)
        if refusal is not None:
            return refusal
        # No local work: the engine fast-forwards a clean clone itself, so a clean clone it still
        # refuses is refused for another cause, and nothing here moves it.
        return outcome(
            skipped,
            "no local work in the clone: the refusal has another cause, and a clone is "
            "never converged by reset",
        )

    # -- a detached HEAD ----------------------------------------------------------------------
    def _reattach_detached(
        self,
        clone: Path,
        refused_for: int,
        outcome: Callable[..., ModelDirtyConvergeOutcome],
        *,
        parked_on: str = "",
    ) -> ModelDirtyConvergeOutcome:
        """Reattach a clone left on a detached HEAD to its tracking branch (OMN-20344).

        Landing text and onex delegate refuse on the host while the shared clone stays detached. The
        converge script derives the branch (the HEAD reflog, then the remote's default branch). A clone
        that holds local work is REFUSED before the script runs (OMN-19998). A clone it cannot
        reattach is FAILED, never skipped: the command exits non-zero and the run is loud.
        """
        skipped = EnumDirtyConvergeStatus.SKIPPED
        failed = EnumDirtyConvergeStatus.FAILED
        where = f"on {parked_on}" if parked_on else "detached"
        if refused_for < self.detached_age:
            return outcome(
                skipped,
                f"{where} for {refused_for}s, below the declared {int(self.detached_age)}s",
            )
        # A raw ref can name a missing object. Require the commit before allowing
        # the converge script to move HEAD and potentially lose its only reference.
        before = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if not before:
            return outcome(failed, "HEAD does not resolve to a commit")
        git_dir_text = self._out(clone, "rev-parse", "--absolute-git-dir")
        if not git_dir_text:
            return outcome(failed, "git directory does not resolve")
        if any((Path(git_dir_text) / marker).exists() for marker in _IN_PROGRESS):
            return outcome(skipped, "a git operation is in progress in the clone")
        lane = self._open_claim_naming(clone)
        if lane:
            return outcome(skipped, f"an open CLAIM of lane {lane} names the clone")
        inside = self._process_inside(clone)
        if inside:
            return outcome(skipped, f"a process is using the clone ({inside})")

        # The converge script resets the tracking branch and force-checks it out, so a clone that
        # holds local work (a dirty or untracked file, a commit no remote has, a local tracking
        # branch ahead of its upstream) is refused here, before the script can run.
        refusal = self._refuse_local_work(clone, refused_for)
        if refusal is not None:
            return refusal
        origin = f"the lane branch {parked_on}" if parked_on else "a detached HEAD"
        note = f"reattached from {origin} at {before[:12]}"
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
                failed, f"reattach could not run: {type(exc).__name__}: {exc}"
            )
        branch = self._out(clone, "symbolic-ref", "--quiet", "--short", "HEAD")
        head = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD")
        if done.returncode or branch not in self.declaration.tracking_branches:
            tail = " ".join((done.stderr or done.stdout or "").split())[-300:]
            return outcome(
                failed,
                f"clone still detached or off a tracking branch ({branch or 'detached'}); "
                f"converge-canonical-clone.sh exit {done.returncode}: {tail}",
            )
        return outcome(
            EnumDirtyConvergeStatus.CONVERGED,
            f"{note}; now on {branch} at {head[:12]}",
            converged_to=head,
        )

    # -- a clone git wrongly treats as bare (OMN-24240) -----------------------------------------
    def _repair_bare(
        self,
        clone: Path,
        refused_for: int,
        outcome: Callable[..., ModelDirtyConvergeOutcome],
    ) -> ModelDirtyConvergeOutcome:
        """Clear core.bare on a clone that has a working tree and no local work, then converge it (OMN-24240).

        A lab host's clone had a full working tree and a .git directory but core.bare=true, so the
        engine refused it forever (a fetch lands and no checkout ever does). A true bare mirror has
        no .git directory inside it and is never touched; a clone with a working tree is repaired.
        """
        skipped = EnumDirtyConvergeStatus.SKIPPED
        failed = EnumDirtyConvergeStatus.FAILED
        if refused_for < self.detached_age:
            return outcome(
                skipped,
                f"core.bare=true for {refused_for}s, below the declared {int(self.detached_age)}s",
            )
        if not (clone / ".git").is_dir():
            return outcome(
                skipped, "a bare mirror with no working tree: nothing to repair"
            )
        if any((clone / ".git" / marker).exists() for marker in _IN_PROGRESS):
            return outcome(skipped, "a git operation is in progress in the clone")
        lane = self._open_claim_naming(clone)
        if lane:
            return outcome(skipped, f"an open CLAIM of lane {lane} names the clone")
        inside = self._process_inside(clone)
        if inside:
            return outcome(skipped, f"a process is using the clone ({inside})")
        before = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD")
        if not before:
            return outcome(failed, "HEAD does not resolve to a commit")
        # Read the clone as a working-tree clone first (core.bare is still true on disk), and clear
        # the flag only when it holds no local work: the converge that follows resets it.
        refusal = self._refuse_local_work(clone, refused_for, bare=True)
        if refusal is not None:
            return refusal
        code, text = self._git(clone, "config", "core.bare", "false")
        if code:
            return outcome(failed, f"could not clear core.bare: {text}")
        note = f"cleared core.bare=true on a clone with a working tree at {before[:12]}"
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
        still_bare = self._out(clone, "rev-parse", "--is-bare-repository")
        branch = self._out(clone, "symbolic-ref", "--quiet", "--short", "HEAD")
        head = self._out(clone, "rev-parse", "--verify", "--quiet", "HEAD")
        if (
            done.returncode
            or still_bare == "true"
            or branch not in self.declaration.tracking_branches
        ):
            tail = " ".join((done.stderr or done.stdout or "").split())[-300:]
            return outcome(
                failed,
                f"clone still bare or off a tracking branch ({branch or 'detached'}); "
                f"converge-canonical-clone.sh exit {done.returncode}: {tail}",
            )
        return outcome(
            EnumDirtyConvergeStatus.CONVERGED,
            f"{note}; now at {head[:12]}",
            converged_to=head,
        )

    # -- the tick -----------------------------------------------------------------------------
    def handle(
        self, request: ModelDirtyConvergeTickRequest
    ) -> ModelDirtyConvergeTickReceipt:
        """One tick for the registry root the handler was composed with."""
        return ModelDirtyConvergeTickReceipt(
            after_seconds=int(min(self.age, self.detached_age)),
            outcomes=tuple(self.tick()),
        )

    def tick(self) -> list[ModelDirtyConvergeOutcome]:
        env = {k: v for k, v in self._env.items() if k not in _GIT_LOCATION_ENV}
        env[ENGINE_BOUND_ENV] = str(int(min(self.age, self.detached_age)))
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
        return self._raise_alarms(outcomes)

    # -- the owned alarm ----------------------------------------------------------------------
    def _raise_alarms(
        self, outcomes: list[ModelDirtyConvergeOutcome]
    ) -> list[ModelDirtyConvergeOutcome]:
        """One alarm delivery per tick: an ALARM naming every REFUSED clone, or an OK that ends an episode.

        Delivery is edge-triggered (node_lab_alarm_deliver_effect): a clone is posted and written to the
        ledger when it enters the refused state, and again only after a tick that reads OK. Every
        REFUSED outcome carries how its alarm ended; none is left unrecorded.
        """
        refused = [o for o in outcomes if o.status is EnumDirtyConvergeStatus.REFUSED]
        if self._delivery is None:
            return [
                o.model_copy(update={"alarm": ALARM_NOT_CONFIGURED})
                if o.status is EnumDirtyConvergeStatus.REFUSED
                else o
                for o in outcomes
            ]
        condition = self.declaration.alarm_condition
        try:
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=str(uuid.uuid4()),
                    condition=condition,
                    outcome=(
                        EnumDeliveryCondition.ALARM
                        if refused
                        else EnumDeliveryCondition.OK
                    ),
                    alarms=tuple(
                        ModelLabAlarm(
                            condition=condition, subject=o.clone, detail=o.reason
                        )
                        for o in refused
                    ),
                    evidence=f"{len(refused)} clone(s) hold local work",
                )
            )
        except (ValueError, OSError) as exc:
            failure = f"{ALARM_FAILED}: {type(exc).__name__}: {exc}"[:500]
            if not refused:
                sys.stderr.write(f"alarm delivery (OK) did not run: {failure}\n")
            return [
                o.model_copy(update={"alarm": failure})
                if o.status is EnumDirtyConvergeStatus.REFUSED
                else o
                for o in outcomes
            ]
        return [self._with_alarm(o, condition, reply) for o in outcomes]

    @staticmethod
    def _with_alarm(
        outcome: ModelDirtyConvergeOutcome,
        condition: str,
        reply: ModelLabAlarmDeliveryResult,
    ) -> ModelDirtyConvergeOutcome:
        if outcome.status is not EnumDirtyConvergeStatus.REFUSED:
            return outcome
        entry = next(
            (d for d in reply.deliveries if d.key == f"{condition}:{outcome.clone}"),
            None,
        )
        if reply.outcome is EnumDeliveryOutcome.FAILED or (
            entry is not None
            and EnumDeliveryStep.FAILED in (entry.channel, entry.ledger)
        ):
            why = (entry.failure if entry else "") or reply.failure or "delivery failed"
            return outcome.model_copy(update={"alarm": f"{ALARM_FAILED}: {why}"[:500]})
        if entry is None:
            return outcome.model_copy(
                update={"alarm": f"{ALARM_FAILED}: no delivery entry for the clone"}
            )
        quiet = {
            EnumDeliveryStep.SUPPRESSED,
            EnumDeliveryStep.NOT_DUE,
            EnumDeliveryStep.DUPLICATE,
        }
        done = (
            ALARM_SUPPRESSED
            if {entry.channel, entry.ledger} <= quiet
            else ALARM_DELIVERED
        )
        return outcome.model_copy(update={"alarm": done})


def registry_root_from(env: Mapping[str, str]) -> Path:
    if not env.get("OMNI_HOME"):
        raise click.UsageError("set OMNI_HOME (there is no default registry root)")
    return Path(env["OMNI_HOME"])


def delivery_from_overlay(env: Mapping[str, str]) -> ProtocolAlarmDelivery | None:
    """The lab alarm delivery of this host's overlay, or None when the host declares none.

    A host with no delivery overlay has no owned alarm to raise: a clone it refuses still ends the
    tick non-zero (alarm "not-configured"). A delivery overlay that exists and is broken is named on
    stderr and also reads as not configured.
    """
    try:
        return build_delivery_handler(load_delivery_overlay(env))
    except NodeOverlayNotFoundError:
        return None
    except NodeOverlayError as exc:
        sys.stderr.write(f"alarm delivery overlay unusable: {exc}\n")
        return None


def alarm_unrecorded(outcomes: Sequence[ModelDirtyConvergeOutcome]) -> bool:
    """True when a REFUSED clone's alarm was neither delivered nor already open."""
    return any(
        o.status is EnumDirtyConvergeStatus.REFUSED
        and o.alarm not in (ALARM_DELIVERED, ALARM_SUPPRESSED)
        for o in outcomes
    )


@click.command("dirty-converge")
def dirty_converge_command() -> None:
    """Sync every canonical clone, then refuse and alarm each one refused past the age that holds local work."""
    handler = HandlerCanonicalCloneDirtyConvergeEffect(
        registry_root_from(os.environ),
        env=os.environ,
        delivery=delivery_from_overlay(os.environ),
    )
    outcomes = handler.tick()
    for item in outcomes:
        click.echo(
            f"{item.status.value.upper():9} {item.clone} refused {item.refused_for_seconds}s: {item.reason}"
            + (f" [alarm: {item.alarm}]" if item.alarm else "")
        )
    if alarm_unrecorded(outcomes) or any(
        item.status is EnumDirtyConvergeStatus.FAILED for item in outcomes
    ):
        sys.exit(1)


def main() -> None:
    dirty_converge_command()


if __name__ == "__main__":
    main()
