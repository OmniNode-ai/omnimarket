# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fake ports of the ledger-reconcile effect node: the ledger host, GitHub, git, the writer and the clock (OMN-20677)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.ledger_reconcile import (
    ModelPrFact,
    ModelPushFact,
    ModelReconcileOverlay,
    ModelReconcileSource,
    ModelShaFact,
    ModelShaRef,
)
from omnimarket.nodes.node_ledger_reconcile_effect.protocols import ReconcilePortError

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
OVERLAY = ModelReconcileOverlay(
    github_org="Example-Org",
    repo_aliases={"market": "omnimarket", "infra": "omnibase_infra"},
    branch_prefixes=("lane", "promotion"),
)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeHost:
    """A ledger host held in memory."""

    def __init__(
        self,
        rows: list[str] | None = None,
        *,
        archives: tuple[ModelReconcileSource, ...] = (),
        clones: tuple[str, ...] = ("omnimarket",),
        roster: frozenset[str] = frozenset(),
        blocked: str = "",
        overlay: ModelReconcileOverlay | None = OVERLAY,
        ledger_name: str = "ledger.md",
        ledger_missing: bool = False,
    ) -> None:
        self.text = "# Ledger\n" + "\n".join(rows or []) + "\n"
        self.archives = archives
        self.clones = clones
        self.roster = roster
        self.blocked = blocked
        self._overlay = overlay
        self.ledger_name = ledger_name
        self.ledger_missing = ledger_missing
        self.roster_reads: list[Path] = []

    def prerequisites(self) -> str:
        return self.blocked

    def ledger_path(self) -> Path:
        return Path("/registry/ledger") / self.ledger_name

    def registry_root(self) -> Path:
        return Path("/registry/registry_root")

    def read_ledger(
        self, ledger: Path
    ) -> tuple[ModelReconcileSource, tuple[ModelReconcileSource, ...]]:
        if self.ledger_missing:
            raise ReconcilePortError(f"live ledger missing: {ledger}")
        return ModelReconcileSource(name=ledger.name, text=self.text), self.archives

    def clone_names(self, root: Path) -> tuple[str, ...]:
        del root
        return self.clones

    def read_roster(self, path: Path) -> frozenset[str]:
        self.roster_reads.append(path)
        if path.name == "absent.txt":
            raise ReconcilePortError(f"--live-lanes file missing: {path}")
        return self.roster

    def overlay(self) -> ModelReconcileOverlay:
        if self._overlay is None:
            raise ReconcilePortError("no ledger-reconcile overlay")
        return self._overlay


class FakeGitHub:
    """Pull requests by number; unknown numbers are OPEN; ``lookups`` records every call."""

    def __init__(
        self,
        merged: Mapping[int, str] | None = None,
        *,
        states: Mapping[int, str] | None = None,
        title: str = "fix(OMN-1): claimed work",
        merge_sha: str = "a" * 40,
        pushes: Mapping[int, str] | None = None,
        sequence: list[str] | None = None,
    ) -> None:
        self.merged = dict(merged or {})
        self.states = dict(states or {})
        self.title = title
        self.merge_sha = merge_sha
        self.pushes = dict(pushes or {})
        self.sequence = sequence
        self.lookups: list[tuple[str, str, int]] = []
        self.push_lookups: list[tuple[str, str, int]] = []

    def pr(self, org: str, repo: str, number: int) -> ModelPrFact:
        self.lookups.append((org, repo, number))
        if self.sequence is not None:
            state = self.sequence.pop(0)
            merged_at = "2026-09-22T11:00:00Z" if state == "MERGED" else ""
            return ModelPrFact(
                repo=repo,
                number=number,
                state=state,
                merged_at=merged_at,
                merge_sha=self.merge_sha if state == "MERGED" else "",
                title=self.title,
            )
        if number in self.merged:
            return ModelPrFact(
                repo=repo,
                number=number,
                state="MERGED",
                merged_at=self.merged[number],
                merge_sha=self.merge_sha,
                title=self.title,
            )
        return ModelPrFact(
            repo=repo,
            number=number,
            state=self.states.get(number, "OPEN"),
            title=self.title,
        )

    def pushed_at(self, org: str, repo: str, number: int) -> ModelPushFact:
        self.push_lookups.append((org, repo, number))
        return ModelPushFact(
            repo=repo, number=number, committed_at=self.pushes.get(number, "")
        )


class FakeGit:
    """Commits by sha; ``landed`` shas are found in the first candidate and are ancestors."""

    def __init__(
        self,
        landed: Mapping[str, tuple[str, str, tuple[str, ...]]] | None = None,
        unlanded: frozenset[str] = frozenset(),
    ) -> None:
        self.landed = dict(landed or {})
        self.unlanded = unlanded
        self.probes: list[ModelShaRef] = []

    def probe(self, root: Path, registry_name: str, ref: ModelShaRef) -> ModelShaFact:
        del root, registry_name
        self.probes.append(ref)
        if ref.sha in self.landed:
            found, committer_at, tickets = self.landed[ref.sha]
            return ModelShaFact(
                sha=ref.sha,
                candidates=ref.candidates,
                found_in=found,
                landed=True,
                committer_at=committer_at,
                msg_tickets=tickets,
            )
        if ref.sha in self.unlanded:
            return ModelShaFact(
                sha=ref.sha,
                candidates=ref.candidates,
                found_in=ref.candidates[0],
                landed=False,
            )
        return ModelShaFact(sha=ref.sha, candidates=ref.candidates)


class FakeAppender:
    """Records every row; ``error`` is returned for each when set."""

    def __init__(self, error: str = "", errors: dict[int, str] | None = None) -> None:
        self.error = error
        self.errors = errors or {}
        self.rows: list[str] = []
        self.ledgers: list[Path] = []

    def append(self, ledger: Path, row: str) -> str:
        self.ledgers.append(ledger)
        self.rows.append(row)
        return self.errors.get(len(self.rows) - 1, self.error)
