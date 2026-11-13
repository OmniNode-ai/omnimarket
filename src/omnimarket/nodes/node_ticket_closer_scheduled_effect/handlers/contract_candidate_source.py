# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-index candidate source of the ticket closer (OMN-24138).

``ContractCandidateSource`` reads the candidate tickets from the contracts on a product
default branch and the merges naming them: a ticket is a candidate when its contract
exists, its newest default-branch squash merge names it, none of its pull requests is
still open, and its own state is an eligible one with unchecked criteria. Each call
rotates a cursor through the contract ids, scanning at most ``scan_limit`` of them, so
the same unflippable tickets are not re-read every run. A part of the reading that
cannot be read is ``None``: the reading is unread, never an empty success.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Protocol

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.rules_lab_fill_tickets import parse_criteria
from omnimarket.models.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)
from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    ModelProofCloseRequest,
)
from omnimarket.models.ticket_closer import ModelCloserCandidate
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_contract_index import (
    ProtocolContractIndex,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
    clone_path,
)


class ProtocolCloserTicketReader(Protocol):
    """One ticket's own reading."""

    def read(self, ticket_id: str) -> tuple[str, str] | None:
        """The ticket's (state name, description); None when it cannot be read."""
        ...


class ProtocolCloserMergeReader(Protocol):
    """One ticket's merge on the product default branches."""

    def merge(self, ticket_id: str) -> tuple[str, str] | None:
        """The (clone path, merge commit sha) of the newest squash merge naming the ticket
        on a repository's default branch; None when there is none."""
        ...


class ProtocolCloserOpenPullRequests(Protocol):
    """The pull requests still open for a set of tickets."""

    def open_for(self, ticket_ids: frozenset[str]) -> frozenset[str] | None:
        """The subset of the ids that still have an open pull request; None when that
        cannot be read."""
        ...


def _suffix(ticket_id: str) -> int:
    return int(ticket_id.rsplit("-", 1)[-1])


class GitCloneMergeReader:
    """The newest squash merge naming the ticket on each listed repository's ``origin``
    default branch.

    A repository that cannot be read — no clone, no ``origin`` branch, a failed log — is
    skipped: the answer comes from the repositories that were read, and is None when none
    of them names the ticket.
    """

    def __init__(self, home: Path, overlay: ModelCloseoutScopeOverlay) -> None:
        self._home = home
        self._overlay = overlay

    @staticmethod
    def _git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )

    def merge(self, ticket_id: str) -> tuple[str, str] | None:
        grep = f"(^|[^0-9]){re.escape(ticket_id)}([^0-9]|$)"
        newest: tuple[int, str, Path] | None = None  # (committer time, sha, clone)
        for repository in self._overlay.pr_repositories:
            clone = clone_path(self._home, repository)
            if not clone.is_dir():
                continue
            ref = f"origin/{repository.default_branch}"
            if self._git(clone, "rev-parse", "-q", "--verify", ref).returncode != 0:
                continue
            log = self._git(
                clone,
                "log",
                ref,
                "-1",
                "--extended-regexp",
                "--grep",
                grep,
                "--format=%H%x09%ct",
            )
            if log.returncode != 0:
                continue
            for line in log.stdout.splitlines():
                sha, _, stamp = line.partition("\t")
                when = int(stamp) if stamp.isdigit() else -1
                if newest is None or when > newest[0]:
                    newest = (when, sha.strip(), clone)
        return None if newest is None else (str(newest[2]), newest[1])


class ContractCandidateSource:
    """The real backend candidate source: contracts, merges, tickets and open pull requests.

    Each call rotates a cursor through the contract ids, scanning at most ``scan_limit``
    of them, so the same unflippable tickets are not re-read every run. ``None`` is the
    reading that could not be taken — the contracts could not be listed, or the open
    pull requests could not be read — never an empty success.
    """

    def __init__(
        self,
        *,
        contracts: ProtocolContractIndex,
        merges: ProtocolCloserMergeReader,
        tickets: ProtocolCloserTicketReader,
        open_prs: ProtocolCloserOpenPullRequests,
        eligible_states: frozenset[str],
        scan_limit: int,
    ) -> None:
        if scan_limit <= 0:
            raise ValueError("scan_limit must be positive")
        self._contracts = contracts
        self._merges = merges
        self._tickets = tickets
        self._open_prs = open_prs
        self._eligible = frozenset(state.lower() for state in eligible_states)
        self._scan_limit = scan_limit
        self._cursor = 0  # the integer suffix last scanned

    def _window(self, ids: list[str]) -> list[str]:
        start = next(
            (i for i, ticket_id in enumerate(ids) if _suffix(ticket_id) > self._cursor),
            0,
        )
        window = ids[start : start + self._scan_limit]
        if window:
            self._cursor = _suffix(window[-1])
        return window

    def candidates(
        self, now: datetime, limit: int
    ) -> list[ModelCloserCandidate] | None:
        ids = sorted(self._contracts.contract_ids(), key=_suffix)
        if not ids:
            return None
        merges: dict[str, tuple[str, str]] = {}
        for ticket_id in self._window(ids):
            found = self._merges.merge(ticket_id)
            if found is not None:
                merges[ticket_id] = found
        open_ids = self._open_prs.open_for(frozenset(merges))
        if open_ids is None:
            return None
        candidates: list[ModelCloserCandidate] = []
        for ticket_id, (clone, sha) in merges.items():
            if ticket_id in open_ids:
                continue  # an open pull request means not all merged
            reading = self._tickets.read(ticket_id)
            if reading is None:
                continue
            state, description = reading
            if state.lower() not in self._eligible:
                continue
            criteria = parse_criteria(description)
            if not criteria:
                continue
            candidates.append(
                ModelCloserCandidate(
                    ticket_id=ticket_id,
                    criteria=criteria,
                    all_prs_merged=True,
                    runtime_proof=ModelProofCloseRequest(
                        ticket_id=ticket_id,
                        change_merged=True,
                        change_sha=sha,
                        repo_path=clone,
                    ),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates
