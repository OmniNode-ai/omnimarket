# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15777: cross-identity observation PRs must share ONE open PR, not N.

`_open_pr_for_identity` (OMN-15300) only ever suppressed a SECOND PR for the
SAME identity (same product repo/PR/head_sha/policy_version). Two DIFFERENT
identities each still branched + opened their own PR, and because every
observation's self-bind commit appends to the identical
``contracts/OMN-14888.yaml`` tail, any two simultaneously-open observation PRs
are structural conflicts with each other (live evidence: 10/10 open OCC PRs
CONFLICTING/DIRTY on 2026-08-09).

The fix: when ANY `auto/occ-observation-*` PR is already open — regardless of
identity — this run's record + self-bind commits land as two MORE commits on
THAT branch instead of a fresh branch/PR. Evidence-item ids are keyed on
workflow-run-id/attempt (`occ_observation_evidence_item_id`), so two different
identities appending to the same branch never collide on an id, and each
append starts from the branch's current tip, so a later append's insertion
point is always past the earlier append's entry — no merge, no conflict.

Hermetic: every GitHub call is stubbed, git is a real local repo, no network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATION_EVIDENCE_TICKET,
    occ_observation_contract_relpath,
    occ_observation_evidence_item_id,
    occ_observation_record_relpath,
)
from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
    _branch_name,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)
from tests.unit.nodes.node_occ_observation_effect.conftest import OCC_FIXTURE_ROOT


def _record(
    product_pr_number: int, head_sha: str, workflow_run_id: int
) -> ModelOccObservationRecord:
    """A distinct IDENTITY per call (different product_pr_number/head_sha)."""
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=product_pr_number,
        head_sha=head_sha,
        policy_version="v1",
        workflow_run_id=workflow_run_id,
        run_attempt=1,
        recorded_at="2026-08-09T00:00:00Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=product_pr_number,
            occ_pr_number=None,
            minted_by_node=True,
            attestation_match=False,
            occ_preflight_eligible=True,
            observed_at="2026-08-09T00:00:00Z",
            reason="mismatch",
        ),
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_repo(tmp_path: Path, name: str = "seed") -> Path:
    seed = tmp_path / name
    shutil.copytree(OCC_FIXTURE_ROOT, seed)
    _git(seed, "init", "-q")
    _git(seed, "config", "user.name", "test")
    _git(seed, "config", "user.email", "test@omninode.ai")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "OCC fixture base")
    return seed


class FakeGitHub:
    """Minimal stand-in for the REST calls the handler makes, PLUS a real
    in-memory `origin` bare repo so a cloned branch can genuinely carry a
    PRIOR run's commits (proving requirement (d): sequential appends onto an
    already-advanced branch never conflict)."""

    def __init__(self, tmp_path: Path, seed: Path) -> None:
        self.tmp_path = tmp_path
        self.origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(seed), str(self.origin)],
            check=True,
            capture_output=True,
        )
        self.open_prs: list[dict[str, Any]] = []
        self.created_titles: list[str] = []
        self.pushes: list[tuple[str, str]] = []  # (branch, head_sha_after_push)
        self._next_number = 7001
        #: PR number -> single-PR GET payload (OMN-15777 hardening). A number
        #: absent from this map defaults to clean/mergeable, matching every
        #: freshly-created branch's real starting state and preserving every
        #: pre-existing test's behavior unmodified.
        self.mergeable_by_number: dict[int, dict[str, Any]] = {}
        #: PR number -> a SEQUENCE of payloads, popped one per poll attempt
        #: (OMN-15777 hardening, unresolved-mergeable-state retry). The last
        #: entry repeats once the sequence is exhausted, so a short sequence
        #: models "resolves on attempt N then stays that way". Checked before
        #: ``mergeable_by_number``.
        self.mergeable_sequence_by_number: dict[int, list[dict[str, Any]]] = {}
        self.mergeable_probe_numbers: list[int] = []

    # -- rest_json_array (PR listing) ---------------------------------------
    def rest_json_array(
        self, _method: str, path: str, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        if "head=" in path:
            wanted = path.split("head=")[1].split("&")[0].split(":", 1)[1]
            return [pr for pr in self.open_prs if pr["head"]["ref"] == wanted]
        return list(self.open_prs)

    # -- rest_json (PR create + commit probe + single-PR mergeable check) ----
    def rest_json(
        self,
        _method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if body is not None:
            number = self._next_number
            return self._create_pr(body, number)
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            return {"sha": sha}
        # Single-PR GET (mergeable check, OMN-15777 hardening):
        # /repos/{owner}/{name}/pulls/{number}
        number = int(path.rsplit("/", 1)[-1])
        self.mergeable_probe_numbers.append(number)
        sequence = self.mergeable_sequence_by_number.get(number)
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.mergeable_by_number.get(
            number, {"mergeable": True, "mergeable_state": "clean"}
        )

    def _create_pr(self, body: dict[str, Any], number: int) -> dict[str, Any]:
        self._next_number += 1
        url = f"https://github.com/OmniNode-ai/onex_change_control/pull/{number}"
        self.open_prs.append(
            {
                "number": number,
                "html_url": url,
                # OMN-15777: the selector requires the head branch to be IN
                # the OCC repo itself (fork-repo hardening).
                "head": {
                    "ref": body["head"],
                    "repo": {"full_name": "OmniNode-ai/onex_change_control"},
                },
            }
        )
        self.created_titles.append(body["title"])
        return {"number": number, "html_url": url}

    # -- clone / push against the real bare origin ---------------------------
    @staticmethod
    def _configure_identity(clone_dir: str) -> None:
        """CI runners carry no global git identity (unlike a dev machine), and
        the real ``_clone_default``/``_clone_branch`` always configure this
        after cloning — the fake must too, or the handler's later ``git
        commit`` fails with exit 128 ("Please tell me who you are") only in
        an identity-less environment, never locally."""
        subprocess.run(
            ["git", "config", "user.name", "test"], cwd=clone_dir, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@omninode.ai"],
            cwd=clone_dir,
            check=True,
        )

    def clone_default(self, clone_dir: str, _token: str, _occ_repo: str) -> str:
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), clone_dir],
            check=True,
            capture_output=True,
        )
        self._configure_identity(clone_dir)
        return "dev"

    def clone_branch(
        self, clone_dir: str, _token: str, _occ_repo: str, branch: str
    ) -> None:
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--single-branch",
                "--branch",
                branch,
                str(self.origin),
                clone_dir,
            ],
            check=True,
            capture_output=True,
        )
        self._configure_identity(clone_dir)

    def push(self, clone_dir: str, branch: str, _token: str, _occ_repo: str) -> None:
        subprocess.run(
            ["git", "push", "-q", str(self.origin), f"HEAD:refs/heads/{branch}"],
            cwd=clone_dir,
            check=True,
            capture_output=True,
        )
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.pushes.append((branch, head_sha))

    def contract_text_on(self, branch: str) -> str:
        return subprocess.run(
            ["git", "show", f"{branch}:{OCC_OBSERVATION_CONTRACT_RELPATH}"],
            cwd=str(self.origin),
            check=True,
            capture_output=True,
            text=True,
        ).stdout


OCC_OBSERVATION_CONTRACT_RELPATH = occ_observation_contract_relpath(
    OCC_OBSERVATION_EVIDENCE_TICKET
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_occ_observation_effect.handlers."
    "handler_occ_observation_effect"
)


def _install(
    handler: HandlerOccObservationEffect,
    fake: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_HANDLER_MODULE}._resolve_github_token", lambda: "tok")
    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json_array", fake.rest_json_array)
    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json", fake.rest_json)
    monkeypatch.setattr(handler, "_clone_default", fake.clone_default)
    monkeypatch.setattr(handler, "_clone_branch", fake.clone_branch)
    monkeypatch.setattr(handler, "_push", fake.push)
    # Never really sleep in tests -- the merge-state poll backoff is real
    # wall-clock time in production (OMN-15777 hardening).
    monkeypatch.setattr(handler, "_sleep_between_merge_state_polls", lambda: None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_different_identity_appends_to_existing_open_pr_no_second_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) With an open auto/occ-observation-* PR present, a DIFFERENT
    identity's observation appends to that branch — no second PR opens."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    first = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(2001, "a" * 40, 900001), mode="mutate"
        )
    )
    second = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(2002, "b" * 40, 900002), mode="mutate"
        )
    )

    assert first.appended_to_existing_pr is False
    assert first.superseded_by_open_pr is False
    assert second.appended_to_existing_pr is True
    assert second.superseded_by_open_pr is False, (
        "a genuinely new observation must still be WRITTEN, not silently dropped"
    )
    assert second.occ_pr_number == first.occ_pr_number, "must reuse the same PR"
    assert len(fake.created_titles) == 1, "exactly one PR ever opened"

    # Both records really landed on the SAME branch, as two more commits.
    assert len(fake.pushes) == 4, "2 pushes per observation x 2 observations"
    branches = {b for b, _ in fake.pushes}
    assert branches == {first.occ_branch}, "all pushes target the one shared branch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_open_pr_opens_a_fresh_one_existing_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) With no open observation PR, a fresh branch + PR is created —
    unchanged from pre-OMN-15777 behavior."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    result = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(2001, "a" * 40, 900001), mode="mutate"
        )
    )

    assert result.appended_to_existing_pr is False
    assert result.superseded_by_open_pr is False
    assert result.occ_branch == _branch_name(
        occ_observation_record_relpath(_record(2001, "a" * 40, 900001))
    )
    assert len(fake.created_titles) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_head_pr_is_ignored_by_the_reuse_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR whose head branch lives in a FORK (not the OCC repo itself) must
    never be treated as a reuse target, even if its branch name matches the
    ``auto/occ-observation-*`` prefix — cloning that branch name FROM the OCC
    repo (not the fork) would fail and block every subsequent observation
    write (CodeRabbit finding, OMN-15777)."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    # Seed a fork PR directly (not via fake.rest_json's POST path, which
    # always stamps the OCC repo) sharing the exact prefix a real observation
    # branch would use.
    fake.open_prs.append(
        {
            "number": 9999,
            "html_url": "https://github.com/OmniNode-ai/onex_change_control/pull/9999",
            "head": {
                "ref": "auto/occ-observation-forked-attempt",
                "repo": {"full_name": "some-attacker/onex_change_control"},
            },
        }
    )

    result = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(2001, "a" * 40, 900001), mode="mutate"
        )
    )

    assert result.appended_to_existing_pr is False, (
        "the fork-headed PR must not be selected as a reuse target"
    )
    assert result.occ_pr_number != 9999
    assert len(fake.created_titles) == 1, "a fresh, OCC-owned PR is opened instead"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_identity_dedupe_still_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) Same-identity dedupe (OMN-15300) is untouched by the widened reuse
    check: a second attempt at the SAME identity is still a pure no-op, never
    a write, even while a reuse target would otherwise be available."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    same_head = "c" * 40
    first = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(2001, same_head, 900001), mode="mutate"
        )
    )
    second = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(2001, same_head, 900002), mode="mutate"
        )
    )

    assert second.superseded_by_open_pr is True
    assert second.appended_to_existing_pr is False
    assert second.occ_pr_number == first.occ_pr_number
    assert len(fake.created_titles) == 1
    assert len(fake.pushes) == 2, "the superseded attempt must never push"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sequential_appends_onto_an_already_advanced_branch_do_not_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) A THIRD, different-identity observation appends onto a branch whose
    OMN-14888.yaml tail was already advanced by the SECOND observation — each
    append starts from the branch's live tip, so there is nothing to conflict
    with, and both prior entries survive byte-identical."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    r1, r2, r3 = (
        _record(3001, "a" * 40, 910001),
        _record(3002, "b" * 40, 910002),
        _record(3003, "c" * 40, 910003),
    )
    for r in (r1, r2, r3):
        result = await handler.handle(
            ModelOccObservationEffectRequest(record=r, mode="mutate")
        )
        assert result.occ_pr_number is not None

    assert len(fake.created_titles) == 1, "still exactly one PR after 3 observations"

    final_branch = fake.pushes[-1][0]
    contract_text = fake.contract_text_on(final_branch)
    parsed = yaml.safe_load(contract_text)
    ids = {item["id"] for item in parsed["dod_evidence"]}
    for r in (r1, r2, r3):
        assert occ_observation_evidence_item_id(r) in ids, (
            "every observation's evidence entry must survive on the final branch"
        )
    assert len(ids) == len(set(ids)), "no id collision across the three appends"


# -- OMN-15777 hardening: reuse selector must skip CONFLICTING candidates ----
#
# `_find_reusable_observation_pr` returned the FIRST/OLDEST open
# `auto/occ-observation-*` PR unconditionally, even when GitHub already
# reports that PR's merge state as CONFLICTING. On 2026-08-09 live OCC state
# showed every new observation funneling onto unmergeable OCC#6155 for exactly
# this reason. The fix skips a CONFLICTING candidate and keeps scanning for
# the oldest OPEN + MERGEABLE one; if every open candidate is CONFLICTING, no
# reuse target exists and a fresh PR is minted instead (see the docstring on
# `_find_reusable_observation_pr` for why: appending onto a branch GitHub
# cannot cleanly merge does not repair the conflict, it just deadlocks every
# future observation onto the same unmergeable PR).


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conflicting_reuse_candidate_is_skipped_fresh_pr_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With exactly one open observation PR, and it is CONFLICTING, the
    selector must not reuse it — a fresh, mergeable branch/PR is minted."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    conflicting_number = 6155
    fake.open_prs.append(
        {
            "number": conflicting_number,
            "html_url": (
                "https://github.com/OmniNode-ai/onex_change_control/pull/"
                f"{conflicting_number}"
            ),
            "head": {
                "ref": "auto/occ-observation-only-conflicting",
                "repo": {"full_name": "OmniNode-ai/onex_change_control"},
            },
        }
    )
    fake.mergeable_by_number[conflicting_number] = {
        "mergeable": False,
        "mergeable_state": "dirty",
    }

    result = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(5001, "f" * 40, 950001), mode="mutate"
        )
    )

    assert result.appended_to_existing_pr is False, (
        "a CONFLICTING candidate must never be treated as a reuse target"
    )
    assert result.superseded_by_open_pr is False
    assert result.occ_pr_number != conflicting_number
    assert len(fake.created_titles) == 1, (
        "a fresh PR is opened; the conflicting one is left untouched"
    )
    assert conflicting_number in fake.mergeable_probe_numbers, (
        "the selector must have actually probed the candidate's merge state"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conflicting_oldest_is_skipped_for_mergeable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With TWO open observation PRs — an older CONFLICTING one and a younger
    MERGEABLE one — the selector must skip the older conflicting PR and reuse
    the mergeable one, never mint a third PR."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    # Step 1: a genuine observation opens a real (mergeable-by-default) PR —
    # this becomes the reuse target the fix must select.
    first = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(4001, "d" * 40, 940001), mode="mutate"
        )
    )
    mergeable_number = first.occ_pr_number
    assert mergeable_number is not None

    # Step 2: seed an OLDER (listed first — `_find_reusable_observation_pr`
    # scans ascending/oldest-first) CONFLICTING PR ahead of the mergeable one.
    conflicting_number = 6100
    fake.open_prs.insert(
        0,
        {
            "number": conflicting_number,
            "html_url": (
                "https://github.com/OmniNode-ai/onex_change_control/pull/"
                f"{conflicting_number}"
            ),
            "head": {
                "ref": "auto/occ-observation-oldest-conflicting",
                "repo": {"full_name": "OmniNode-ai/onex_change_control"},
            },
        },
    )
    fake.mergeable_by_number[conflicting_number] = {
        "mergeable": False,
        "mergeable_state": "dirty",
    }

    # Step 3: a third, different-identity observation must skip the
    # conflicting oldest PR and append onto the mergeable one instead.
    third = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(4002, "e" * 40, 940002), mode="mutate"
        )
    )

    assert third.appended_to_existing_pr is True
    assert third.occ_pr_number == mergeable_number
    assert third.occ_pr_number != conflicting_number
    assert len(fake.created_titles) == 1, "no second/third PR is ever opened"
    assert conflicting_number in fake.mergeable_probe_numbers, (
        "the selector must have actually probed the older candidate's merge "
        "state before skipping it"
    )


# -- OMN-15777 hardening: UNRESOLVED mergeable state must not be treated as
# safe (CodeRabbit finding on PR #2030) ---------------------------------
#
# GitHub computes `mergeable`/`mergeable_state` asynchronously in the
# background. A single-PR GET made immediately after that PR was created or
# pushed to often returns `{"mergeable": null, "mergeable_state": "unknown"}`
# while the computation is still running. The original conflicting-skip fix
# only checked for `mergeable_state == "dirty"` / `mergeable is False` --
# "unknown" satisfied NEITHER condition, so it fell through as "not
# conflicting" and would have been selected as a reuse target GitHub had not
# actually cleared yet. The fix polls with bounded backoff and treats a
# still-unresolved result as non-reusable, never as "probably fine".


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unresolved_mergeable_state_never_settling_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate whose merge state stays UNKNOWN for the whole retry budget
    must be skipped -- a fresh PR is minted, exactly as for a genuinely
    CONFLICTING candidate."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    unresolved_number = 6300
    fake.open_prs.append(
        {
            "number": unresolved_number,
            "html_url": (
                "https://github.com/OmniNode-ai/onex_change_control/pull/"
                f"{unresolved_number}"
            ),
            "head": {
                "ref": "auto/occ-observation-never-resolves",
                "repo": {"full_name": "OmniNode-ai/onex_change_control"},
            },
        }
    )
    fake.mergeable_by_number[unresolved_number] = {
        "mergeable": None,
        "mergeable_state": "unknown",
    }

    result = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(6001, "1" * 40, 960001), mode="mutate"
        )
    )

    assert result.appended_to_existing_pr is False, (
        "an UNRESOLVED merge state must never be treated as safe to reuse"
    )
    assert result.occ_pr_number != unresolved_number
    assert len(fake.created_titles) == 1
    probe_count = fake.mergeable_probe_numbers.count(unresolved_number)
    assert probe_count == handler._MERGE_STATE_POLL_ATTEMPTS, (
        "the selector must retry the full poll budget before giving up, "
        f"not stop after the first unresolved response (probed {probe_count} "
        f"times)"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mergeable_state_that_resolves_clean_within_budget_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate that starts UNKNOWN but resolves to clean/mergeable
    partway through the retry budget IS reused -- the retry recovers it."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    first = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(6002, "2" * 40, 960002), mode="mutate"
        )
    )
    resolving_number = first.occ_pr_number
    assert resolving_number is not None
    # Resolves to clean on the 2nd of 3 poll attempts.
    fake.mergeable_sequence_by_number[resolving_number] = [
        {"mergeable": None, "mergeable_state": "unknown"},
        {"mergeable": True, "mergeable_state": "clean"},
    ]

    second = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(6003, "3" * 40, 960003), mode="mutate"
        )
    )

    assert second.appended_to_existing_pr is True
    assert second.occ_pr_number == resolving_number
    assert len(fake.created_titles) == 1, "the retry must recover -- no fresh PR"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mergeable_state_that_resolves_dirty_within_budget_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate that starts UNKNOWN and resolves to CONFLICTING/dirty
    within the retry budget is still skipped -- the retry doesn't mask a
    real conflict, it only guards against a premature "not conflicting"
    read."""
    seed = _seed_repo(tmp_path)
    fake = FakeGitHub(tmp_path, seed)
    handler = HandlerOccObservationEffect()
    _install(handler, fake, monkeypatch)

    resolving_conflicting_number = 6400
    fake.open_prs.append(
        {
            "number": resolving_conflicting_number,
            "html_url": (
                "https://github.com/OmniNode-ai/onex_change_control/pull/"
                f"{resolving_conflicting_number}"
            ),
            "head": {
                "ref": "auto/occ-observation-resolves-dirty",
                "repo": {"full_name": "OmniNode-ai/onex_change_control"},
            },
        }
    )
    fake.mergeable_sequence_by_number[resolving_conflicting_number] = [
        {"mergeable": None, "mergeable_state": "unknown"},
        {"mergeable": False, "mergeable_state": "dirty"},
    ]

    result = await handler.handle(
        ModelOccObservationEffectRequest(
            record=_record(6004, "4" * 40, 960004), mode="mutate"
        )
    )

    assert result.appended_to_existing_pr is False
    assert result.occ_pr_number != resolving_conflicting_number
    assert len(fake.created_titles) == 1
