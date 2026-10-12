# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters of the ledger-reconcile effect ports (OMN-20677).

They reach what the reconciler always reached, the same way: the ledger file named
by ``ONEX_LEDGER_PATH`` and the archive directory beside it, the clone registry
named by ``OMNI_HOME``, ``gh pr view`` for pull requests, ``git`` in the clones for
commits, and the ledger writer's ``--append`` for rows. The deployment's repository
aliases, branch prefixes and GitHub organisation come from an overlay, never from
this module: ``ONEX_LEDGER_RECONCILE_OVERLAY`` names the file, otherwise the first
``node_ledger_reconcile_effect/overlay.yaml`` under a root of
``ONEX_SKILL_OVERLAY_ROOTS``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.ledger_reconcile import (
    ModelPrFact,
    ModelPushFact,
    ModelReconcileOverlay,
    ModelReconcileSource,
    ModelShaFact,
    ModelShaRef,
)

from .protocol_ledger_reconcile_effect import ReconcilePortError

ARCHIVE_GLOB = "ROLLING_WORK_LEDGER_*.md"
OVERLAY_ENV = "ONEX_LEDGER_RECONCILE_OVERLAY"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
OVERLAY_NODE = "node_ledger_reconcile_effect"
APPEND_COMMAND_ENV = "ONEX_LEDGER_COMMAND"
DEFAULT_APPEND_COMMAND = "onex-ledger"
LOCK_RETRIES = 5
LOCK_RETRY_BASE_SECONDS = 2.0
_TICKET_RE = re.compile(r"\bOMN-\d+\b")


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


class SystemClock:
    """The wall clock, whole seconds, UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class LocalReconcileHost:
    """The operator's ledger, archives, clone registry, roster and overlay."""

    def prerequisites(self) -> str:
        for name in ("OMNI_HOME", "ONEX_LEDGER_PATH"):
            if not os.environ.get(name):
                return f"{name} is not set (required, no default)"
        if shutil.which("gh") is None:
            return "gh CLI not on PATH — live PR verification is impossible"
        if shutil.which("git") is None:
            return "git not on PATH"
        return ""

    def ledger_path(self) -> Path:
        return Path(os.environ["ONEX_LEDGER_PATH"])

    def registry_root(self) -> Path:
        return Path(os.environ["OMNI_HOME"])

    def read_ledger(
        self, ledger: Path
    ) -> tuple[ModelReconcileSource, tuple[ModelReconcileSource, ...]]:
        if not ledger.is_file():
            raise ReconcilePortError(f"live ledger missing: {ledger}")
        live = ModelReconcileSource(
            name=ledger.name, text=ledger.read_text(encoding="utf-8")
        )
        archive_dir = ledger.parent / "archive"
        archives = (
            tuple(
                ModelReconcileSource(name=p.name, text=p.read_text(encoding="utf-8"))
                for p in sorted(archive_dir.glob(ARCHIVE_GLOB))
            )
            if archive_dir.is_dir()
            else ()
        )
        return live, archives

    def clone_names(self, root: Path) -> tuple[str, ...]:
        return tuple(
            child.name
            for child in root.iterdir()
            if child.is_dir() and (child / ".git").exists()
        )

    def read_roster(self, path: Path) -> frozenset[str]:
        """Lane names from a roster file: whitespace- or comma-separated, '#' starts a comment.

        A named file that is missing is a hard failure: a roster silently read as
        empty would let ``--apply`` close live lanes.
        """
        if not path.is_file():
            raise ReconcilePortError(f"--live-lanes file missing: {path}")
        names: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            for name in re.split(r"[\s,]+", line.split("#", 1)[0]):
                if name:
                    names.add(name.strip().strip("`").strip("*").strip())
        return frozenset(names)

    def overlay(self) -> ModelReconcileOverlay:
        path = self._overlay_path()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("overlay must be a mapping")
            return ModelReconcileOverlay.model_validate(raw)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise ReconcilePortError(
                f"ledger-reconcile overlay {path} is unusable: {exc}"
            ) from exc

    def _overlay_path(self) -> Path:
        if OVERLAY_ENV in os.environ:
            pointer = os.environ[OVERLAY_ENV]
            if not pointer:
                raise ReconcilePortError(f"{OVERLAY_ENV}: empty overlay pointer")
            return Path(pointer)
        for root in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep):
            candidate = Path(root) / OVERLAY_NODE / "overlay.yaml" if root else None
            if candidate is not None and candidate.exists():
                return candidate
        raise ReconcilePortError(
            f"no ledger-reconcile overlay: set {OVERLAY_ENV} to an overlay.yaml, or put "
            f"{OVERLAY_NODE}/overlay.yaml under a root in {OVERLAY_ROOTS_ENV}"
        )


class GhPullRequests:
    """Pull requests as ``gh`` reports them."""

    def pr(self, org: str, repo: str, number: int) -> ModelPrFact:
        failed = ModelPrFact(repo=repo, number=number, state="LOOKUP_FAILED")
        try:
            proc = _run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    f"{org}/{repo}",
                    "--json",
                    "state,mergedAt,mergeCommit,title",
                ]
            )
        except (subprocess.TimeoutExpired, OSError):
            return failed
        if proc.returncode != 0:
            return failed
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return failed
        merge_commit = data.get("mergeCommit") or {}
        return ModelPrFact(
            repo=repo,
            number=number,
            state=str(data.get("state", "LOOKUP_FAILED")),
            merged_at=str(data.get("mergedAt") or ""),
            merge_sha=str(merge_commit.get("oid") or ""),
            title=str(data.get("title") or ""),
        )

    def pushed_at(self, org: str, repo: str, number: int) -> ModelPushFact:
        """When the PR's head commit was last committed: the push proxy, '' when unreadable."""
        try:
            proc = _run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    f"{org}/{repo}",
                    "--json",
                    "commits",
                    "--jq",
                    ".commits[-1].committedDate",
                ]
            )
        except (subprocess.TimeoutExpired, OSError):
            return ModelPushFact(repo=repo, number=number)
        committed = proc.stdout.strip() if proc.returncode == 0 else ""
        return ModelPushFact(repo=repo, number=number, committed_at=committed)


class GitCommits:
    """Commits looked up in the canonical clones."""

    def probe(self, root: Path, registry_name: str, ref: ModelShaRef) -> ModelShaFact:
        missing = ModelShaFact(sha=ref.sha, candidates=ref.candidates)
        for name in ref.candidates:
            clone = root if name == registry_name else root / name
            try:
                exists = _run(
                    [
                        "git",
                        "-C",
                        str(clone),
                        "cat-file",
                        "-e",
                        f"{ref.sha}^{{commit}}",
                    ],
                    timeout=15,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if exists.returncode != 0:
                continue
            return self._found(clone, name, ref)
        return missing

    def _found(self, clone: Path, name: str, ref: ModelShaRef) -> ModelShaFact:
        committer_at = ""
        msg_tickets: tuple[str, ...] = ()
        try:
            show = _run(
                ["git", "-C", str(clone), "show", "-s", "--format=%ct%x1f%B", ref.sha],
                timeout=15,
            )
            if show.returncode == 0 and "\x1f" in show.stdout:
                epoch, _, message = show.stdout.partition("\x1f")
                if epoch.strip().isdigit():
                    committer_at = datetime.fromtimestamp(
                        int(epoch.strip()), tz=UTC
                    ).isoformat()
                msg_tickets = tuple(sorted(set(_TICKET_RE.findall(message[:4000]))))
        except (subprocess.TimeoutExpired, OSError):
            pass
        landed = False
        for landing_ref in ("origin/dev", "origin/main", "HEAD"):
            try:
                ancestor = _run(
                    [
                        "git",
                        "-C",
                        str(clone),
                        "merge-base",
                        "--is-ancestor",
                        ref.sha,
                        landing_ref,
                    ],
                    timeout=15,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if ancestor.returncode == 0:
                landed = True
                break
        return ModelShaFact(
            sha=ref.sha,
            candidates=ref.candidates,
            found_in=name,
            landed=landed,
            committer_at=committer_at,
            msg_tickets=msg_tickets,
        )


class LedgerWriterAppender:
    """Appends one row through the ledger writer, retrying its exit-75 contention code."""

    def append(self, ledger: Path, row: str) -> str:
        command = shlex.split(
            os.environ.get(APPEND_COMMAND_ENV, DEFAULT_APPEND_COMMAND)
        )
        for attempt in range(1, LOCK_RETRIES + 1):
            try:
                proc = _run([*command, str(ledger), "--append", row], timeout=360)
            except (subprocess.TimeoutExpired, OSError) as exc:
                return f"ledger writer did not run: {type(exc).__name__}: {exc}"
            if proc.returncode == 0:
                return ""
            if proc.returncode == 75:
                if attempt == LOCK_RETRIES:
                    return f"exit 75 (contention) after {LOCK_RETRIES} attempts"
                time.sleep(LOCK_RETRY_BASE_SECONDS * attempt)
                continue
            return f"ledger_lock exit {proc.returncode}: {proc.stderr.strip()[:300]}"
        return "unreachable"
