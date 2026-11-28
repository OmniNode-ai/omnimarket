# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract-index candidate source and its git merge reader (OMN-24138)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_contract_index import (
    InMemoryContractIndex,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.contract_candidate_source import (
    ContractCandidateSource,
    GitCloneMergeReader,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
BODY = "- [ ] AC1: it works -- falsifier: a test\n- [ ] AC2: it is shown -- falsifier: a read\n"


class Merges:
    def __init__(self, found: dict[str, tuple[str, str]]) -> None:
        self.found = found
        self.asked: list[str] = []
        self.batches: list[list[str]] = []

    def merge_many(self, ticket_ids: Collection[str]) -> dict[str, tuple[str, str]]:
        self.batches.append(sorted(ticket_ids))
        return {t: self.found[t] for t in ticket_ids if t in self.found}

    def merge(self, ticket_id: str) -> tuple[str, str] | None:
        self.asked.append(ticket_id)
        return self.found.get(ticket_id)


class Tickets:
    def __init__(self, found: dict[str, tuple[str, str]]) -> None:
        self.found = found

    def read(self, ticket_id: str) -> tuple[str, str] | None:
        return self.found.get(ticket_id)


class OpenPrs:
    def __init__(self, open_ids: frozenset[str] | None) -> None:
        self.open_ids = open_ids

    def open_for(self, ticket_ids: frozenset[str]) -> frozenset[str] | None:
        return None if self.open_ids is None else ticket_ids & self.open_ids


def source(
    *,
    ids: frozenset[str] = frozenset({"OMN-1", "OMN-2", "OMN-3"}),
    merged: tuple[str, ...] = ("OMN-1", "OMN-2", "OMN-3"),
    tickets: dict[str, tuple[str, str]] | None = None,
    open_ids: frozenset[str] | None = frozenset(),
    scan_limit: int = 10,
) -> ContractCandidateSource:
    return ContractCandidateSource(
        contracts=InMemoryContractIndex(ids),
        merges=Merges({t: ("/clone", f"sha-{t}") for t in merged}),
        tickets=Tickets(
            tickets
            if tickets is not None
            else dict.fromkeys(("OMN-1", "OMN-2", "OMN-3"), ("In Review", BODY))
        ),
        open_prs=OpenPrs(open_ids),
        eligible_states=frozenset({"in progress", "in review"}),
        scan_limit=scan_limit,
    )


def test_golden_a_merged_eligible_ticket_is_a_candidate_with_its_merge_as_the_change() -> (
    None
):
    found = source().candidates(NOW, 10)
    assert found is not None
    assert [c.ticket_id for c in found] == ["OMN-1", "OMN-2", "OMN-3"]
    first = found[0]
    assert first.criteria == ("AC1", "AC2")
    assert first.all_prs_merged is True
    assert first.runtime_proof is not None
    assert first.runtime_proof.change_sha == "sha-OMN-1"
    assert first.runtime_proof.repo_path == "/clone"
    assert first.runtime_proof.receipts == ()


def test_control_each_exclusion_drops_the_ticket() -> None:
    cases = {
        "no merge": source(merged=("OMN-2", "OMN-3")),
        "open pull request": source(open_ids=frozenset({"OMN-1"})),
        "ineligible state": source(
            tickets={
                "OMN-1": ("Done", BODY),
                "OMN-2": ("In Review", BODY),
                "OMN-3": ("In Review", BODY),
            }
        ),
        "no criteria": source(
            tickets={
                "OMN-1": ("In Review", "no boxes here"),
                "OMN-2": ("In Review", BODY),
                "OMN-3": ("In Review", BODY),
            }
        ),
        "ticket unreadable": source(
            tickets={"OMN-2": ("In Review", BODY), "OMN-3": ("In Review", BODY)}
        ),
    }
    for name, candidates_source in cases.items():
        found = candidates_source.candidates(NOW, 10)
        assert found is not None, name
        assert [c.ticket_id for c in found] == ["OMN-2", "OMN-3"], name


def test_error_no_contracts_is_unread_never_an_empty_success() -> None:
    assert source(ids=frozenset()).candidates(NOW, 10) is None


def test_error_unreadable_open_pull_requests_is_unread() -> None:
    assert source(open_ids=None).candidates(NOW, 10) is None


def test_the_limit_bounds_the_candidates() -> None:
    found = source().candidates(NOW, 2)
    assert found is not None
    assert [c.ticket_id for c in found] == ["OMN-1", "OMN-2"]


def test_the_scan_rotates_so_the_same_tickets_are_not_re_read_every_run() -> None:
    rotating = source(scan_limit=2)
    first = rotating.candidates(NOW, 10)
    second = rotating.candidates(NOW, 10)
    third = rotating.candidates(NOW, 10)
    assert first is not None
    assert second is not None
    assert third is not None
    assert [c.ticket_id for c in first] == ["OMN-1", "OMN-2"]
    assert [c.ticket_id for c in second] == ["OMN-3"]
    assert [c.ticket_id for c in third] == ["OMN-1", "OMN-2"]


def test_a_non_positive_scan_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="scan_limit"):
        source(scan_limit=0)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.email=closer@example.invalid",
            "-c",
            "user.name=closer",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def test_the_git_merge_reader_finds_the_newest_squash_merge_naming_the_ticket(
    tmp_path: Path,
) -> None:
    home = tmp_path / "registry"
    clone = home / "omnimarket"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "dev")
    shas = {}
    for subject in (
        "OMN-7: first change (#1)",
        "OMN-70: other (#2)",
        "OMN-7: second (#3)",
    ):
        (clone / "f.txt").write_text(subject, encoding="utf-8")
        _git(clone, "add", ".")
        _git(clone, "commit", "-q", "-m", subject)
        shas[subject] = _git(clone, "rev-parse", "HEAD")
    _git(clone, "update-ref", "refs/remotes/origin/dev", "HEAD")
    overlay = load_scope_overlay().model_copy(
        update={
            "pr_repositories": tuple(
                r
                for r in load_scope_overlay().pr_repositories
                if r.name == "omnimarket"
            )
        }
    )
    reader = GitCloneMergeReader(home, overlay)
    found = reader.merge("OMN-7")
    assert found == (str(clone), shas["OMN-7: second (#3)"])
    assert reader.merge("OMN-700") is None
    assert reader.merge("OMN-70") == (str(clone), shas["OMN-70: other (#2)"])


def _legacy_merge(
    reader_home: Path, overlay: object, ticket_id: str
) -> tuple[str, str] | None:
    """The per-ticket reading the closer ran before OMN-26152: a ``git log -1 --grep``
    per ticket per clone. Kept here as the reference the batched reader must equal."""
    import re

    from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
        clone_path,
    )

    grep = f"(^|[^0-9]){re.escape(ticket_id)}([^0-9]|$)"
    newest: tuple[int, str, Path] | None = None
    for repository in overlay.pr_repositories:  # type: ignore[attr-defined]
        clone = clone_path(reader_home, repository)
        ref = f"origin/{repository.default_branch}"
        out = _git(
            clone, "log", ref, "-1", "--extended-regexp", "--grep", grep,
            "--format=%H%x09%ct",
        )  # fmt: skip
        for line in out.splitlines():
            sha, _, stamp = line.partition("\t")
            when = int(stamp)
            if newest is None or when > newest[0]:
                newest = (when, sha.strip(), clone)
    return None if newest is None else (str(newest[2]), newest[1])


def _fixture_registry(tmp_path: Path) -> tuple[Path, object]:
    home = tmp_path / "registry"
    overlay_all = load_scope_overlay()
    repos = tuple(
        r
        for r in overlay_all.pr_repositories
        if r.name in {"omnimarket", "omnibase_core"}
    )
    assert len(repos) == 2
    subjects = {
        "omnimarket": [
            "OMN-7: first change (#1)",
            "OMN-70: other (#2)",
            "OMN-7: second (#3)",
            "feat: no ticket here (#4)",
        ],
        "omnibase_core": [
            "OMN-9: core change (#5)",
            "fix (#6)\n\nbody names OMN-70 and OMN-12 in the body",
            "XOMN-5: letter prefix (#7)",
            "OMN-500: not five (#8)",
        ],
    }
    for repo in repos:
        clone = home / repo.name
        clone.mkdir(parents=True)
        _git(clone, "init", "-q", "-b", repo.default_branch)
        for subject in subjects[repo.name]:
            (clone / "f.txt").write_text(subject, encoding="utf-8")
            _git(clone, "add", ".")
            _git(clone, "commit", "-q", "-m", subject)
        _git(clone, "update-ref", f"refs/remotes/origin/{repo.default_branch}", "HEAD")
    return home, overlay_all.model_copy(update={"pr_repositories": repos})


IDS = ("OMN-7", "OMN-70", "OMN-9", "OMN-12", "OMN-5", "OMN-50", "OMN-500", "OMN-404")


def test_the_batched_reader_gives_the_answers_of_the_per_ticket_reading(
    tmp_path: Path,
) -> None:
    home, overlay = _fixture_registry(tmp_path)
    batched = GitCloneMergeReader(home, overlay).merge_many(IDS)
    for ticket_id in IDS:
        assert batched.get(ticket_id) == _legacy_merge(home, overlay, ticket_id), (
            ticket_id
        )
    assert batched["OMN-12"] is not None  # named only in a commit body


def test_a_run_reads_each_clone_once_whatever_the_number_of_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, overlay = _fixture_registry(tmp_path)
    calls: list[tuple[str, ...]] = []
    real = GitCloneMergeReader._git

    def counting(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return real(clone, *args)

    monkeypatch.setattr(GitCloneMergeReader, "_git", staticmethod(counting))
    many = tuple(f"OMN-{n}" for n in range(1, 101))
    GitCloneMergeReader(home, overlay).merge_many(many)
    assert len(calls) == 2  # one git log per clone, not two per id per clone


def test_the_git_processes_of_a_run_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, overlay = _fixture_registry(tmp_path)
    calls: list[tuple[str, ...]] = []
    real = GitCloneMergeReader._git

    def counting(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return real(clone, *args)

    monkeypatch.setattr(GitCloneMergeReader, "_git", staticmethod(counting))
    reader = GitCloneMergeReader(home, overlay, max_git_processes=1)
    found = reader.merge_many(IDS)
    assert len(calls) == 1
    # one clone was read and the other skipped, never guessed
    assert len({clone for clone, _ in found.values()}) == 1
    assert len(found) < len(IDS)


def test_the_candidate_source_asks_the_merge_reader_once_per_run() -> None:
    merges = Merges({"OMN-1": ("/clone", "sha-OMN-1")})
    src = source()
    src._merges = merges  # the fake counts its calls
    src.candidates(NOW, 10)
    assert merges.batches == [["OMN-1", "OMN-2", "OMN-3"]]
    assert merges.asked == []
