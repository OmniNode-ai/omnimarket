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
from collections.abc import Collection
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

    def merge_many(self, ticket_ids: Collection[str]) -> dict[str, tuple[str, str]]:
        """The (clone path, merge commit sha) of the newest squash merge naming each
        ticket on a repository's default branch, read once for the whole set; an id with
        no merge is absent."""
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
    """The newest squash merge naming each ticket on each listed repository's ``origin``
    default branch.

    ``merge_many`` reads each clone once per run (OMN-26152): one ``git log`` of the
    subject and body of the commits since a bounded horizon, an id to merge map built in
    memory, every id answered from it. The answer is the one the per-ticket
    ``git log -1 --grep`` gave: the first commit in log order naming the id on a clone,
    and the newest committer time across clones. A repository that cannot be read — no
    clone, no ``origin`` branch, a failed log — is skipped, and so is every repository
    past ``max_git_processes`` for the run: the answer comes from the repositories that
    were read, and an id no read repository names is absent.
    """

    # An id as the per-ticket grep matched it: not glued to a digit on either side.
    _ID = re.compile(r"(?<![0-9])(OMN-[0-9]+)(?![0-9])")
    _RECORD = "\x1e"
    _FIELD = "\x1f"

    def __init__(
        self,
        home: Path,
        overlay: ModelCloseoutScopeOverlay,
        *,
        horizon_days: int | None = 400,
        max_git_processes: int = 16,
    ) -> None:
        if max_git_processes <= 0:
            raise ValueError("max_git_processes must be positive")
        self._home = home
        self._overlay = overlay
        self._horizon_days = horizon_days
        self._max_git_processes = max_git_processes

    @staticmethod
    def _git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )

    def _index(self, clone: Path, ref: str) -> dict[str, tuple[int, str]] | None:
        """The first commit naming each id on the clone's ref, in log order; None when
        the log cannot be read."""
        args = ["log", ref, f"--format=%H{self._FIELD}%ct{self._FIELD}%B{self._RECORD}"]
        if self._horizon_days is not None:
            args.append(f"--since={self._horizon_days}.days.ago")
        log = self._git(clone, *args)
        if log.returncode != 0:
            return None
        index: dict[str, tuple[int, str]] = {}
        for record in log.stdout.split(self._RECORD):
            sha, _, rest = record.strip("\n").partition(self._FIELD)
            stamp, _, message = rest.partition(self._FIELD)
            if not sha:
                continue
            when = int(stamp) if stamp.isdigit() else -1
            for ticket_id in self._ID.findall(message):
                index.setdefault(ticket_id, (when, sha.strip()))
        return index

    def merge_many(self, ticket_ids: Collection[str]) -> dict[str, tuple[str, str]]:
        """The (clone path, merge sha) of each id's newest merge; ids with none absent."""
        wanted = frozenset(ticket_ids)
        newest: dict[str, tuple[int, str, Path]] = {}
        spawned = 0
        for repository in self._overlay.pr_repositories:
            clone = clone_path(self._home, repository)
            if not clone.is_dir():
                continue
            if spawned >= self._max_git_processes:
                break
            spawned += 1
            index = self._index(clone, f"origin/{repository.default_branch}")
            if index is None:
                continue
            for ticket_id in wanted & index.keys():
                when, sha = index[ticket_id]
                held = newest.get(ticket_id)
                if held is None or when > held[0]:
                    newest[ticket_id] = (when, sha, clone)
        return {t: (str(clone), sha) for t, (_, sha, clone) in newest.items()}

    def merge(self, ticket_id: str) -> tuple[str, str] | None:
        return self.merge_many((ticket_id,)).get(ticket_id)


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
        window = self._window(ids)
        found = self._merges.merge_many(window)
        merges = {t: found[t] for t in window if t in found}
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
