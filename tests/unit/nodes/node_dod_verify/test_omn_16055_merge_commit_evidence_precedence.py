# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16055 — merge-commit check-run evidence precedence (seam fix).

THE SEAM
--------
Producer side (``omnibase_core/.github/workflows/ci.yml``): ``CI Summary`` is
a required branch-protection context on ``dev`` that ALSO fires on ``push``.
Its ``push``-event run is *not* a merge gate — nothing can be blocked by it,
because the merge already happened. On ``omnibase_core`` that push run is
non-green by construction: ``Contract Compliance Check``'s DoD ``check_value``s
are PR-scoped, so its empty-``PR_NUMBER`` branch ``exit 1``s on ``push``
(deliberate — an ``if:``-driven ``skipped`` would count as GOOD in the gate's
own completeness anchor, which is strictly worse), and separately the
``push`` runs are routinely cancelled mid-flight under runner-fleet pressure,
which the same gate scores as a default-deny sweep failure.

Consumer side (``FETCH_PR_CHECKS_GREEN``, this module's subject): OMN-15817
shape 2b added the squash merge commit as an ADDITIVE evidence source, then
evaluated ``any(not green)`` over ``[head_winner, merge_winner]``. Because the
merge commit of every ``omnibase_core`` PR carries that structurally-red push
run, ``CI Summary``'s ``merge_winner`` is permanently red — so ``::pr-live-state``
fails for EVERY merged core PR regardless of the PR's own state, and no honest
Done-flip is possible.

THE MATCHED CONTRACT (define-and-match, OMN-14208)
--------------------------------------------------
Field: GitHub check-run ``(name, head_sha, status, conclusion)`` tuples for
names in the base branch's ``required_status_checks`` context set.

* The **head-SHA winner is the merge-gate artifact.** It is the exact object
  branch protection consulted to unlock the merge button, so when it exists it
  is authoritative — in BOTH directions (a red head winner still fails closed,
  unchanged).
* The **merge-commit winner is sole-observer evidence.** It stays fully
  load-bearing — including fail-closed on a red conclusion — whenever the head
  SHA produced no own/unattributable run for that context. That is exactly the
  OMN-15817 shape-2b case (push-only umbrella contexts; deleted source branch)
  and it is untouched here.
* Only one cell changes: head winner present AND green, merge winner red. That
  combination is post-merge *branch* telemetry, not evidence about whether this
  PR passed its gate — and it is never dropped silently, it is named in the
  result ``detail``.

The cross-boundary regression test below drives the REAL probe against an
unedited live capture of BOTH sides of the seam (``omnibase_core`` PR #1557,
merged 2026-08-14) — not two independent unit suites.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
)

_FIXTURE_PATH = (
    pathlib.Path(__file__).parent / "fixtures" / "omn_16055_core_pr_1557_live.json"
)
_FIXTURE_REPO = "OmniNode-ai/omnibase_core"
_FIXTURE_PR = 1557


def _load_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text())
    return data


def _jsonl(objs: list[dict[str, Any]]) -> str:
    """Render fixture rows the way ``gh api --paginate --jq '...[]'`` does:
    one JSON document per line, NOT a single JSON array."""
    return "\n".join(json.dumps(o) for o in objs)


def _replay_live_capture(fixture: dict[str, Any]) -> Any:
    """Route every ``gh`` invocation ``FETCH_PR_CHECKS_GREEN`` makes to the
    corresponding unedited payload recorded from the live GitHub API.

    Nothing here is synthesised: the branch-protection context set, the head
    SHA's check-suites/check-runs, and the merge commit's check-runs are the
    exact bytes the probe would have received on 2026-08-14.
    """
    pr_view = fixture["pr_view"]
    head_sha = str(pr_view["headRefOid"])
    merge_sha = str(pr_view["mergeCommit"]["oid"])

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in (list(args[0]) if args else [])]  # type: ignore[arg-type]
        joined = " ".join(argv)

        def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=stdout, stderr=""
            )

        if "view" in argv:
            return _ok(json.dumps(pr_view))
        if "protection/required_status_checks" in joined:
            return _ok(json.dumps(fixture["required_status_checks"]))
        if "rules/branches" in joined:
            # Live readback: omnibase_core@dev carries no ruleset-sourced
            # required checks; classic protection is the whole set.
            return _ok("[]")
        if "check-suites" in joined:
            return _ok(_jsonl(fixture["head_check_suites"]))
        if "check-runs" in joined:
            if f"commits/{merge_sha}/check-runs" in joined:
                return _ok(_jsonl(fixture["merge_check_runs"]))
            if f"commits/{head_sha}/check-runs" in joined:
                return _ok(_jsonl(fixture["head_check_runs"]))
            raise AssertionError(f"check-runs call for an unexpected SHA: {joined}")
        raise AssertionError(f"unrouted gh invocation: {argv}")

    return _run


def _probe(repo: str = _FIXTURE_REPO, pr_number: int = _FIXTURE_PR) -> Any:
    command = ModelDodEvidenceGithubLookupCommand(
        operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
        repo=repo,
        pr_number=pr_number,
    )
    return HandlerDodEvidenceGithubEffect().handle(command).events[0]


def _is_green(run: dict[str, Any]) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") in (
        "success",
        "skipped",
        "neutral",
    )


def _winner(runs: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [r for r in runs if r.get("name") == name]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda r: (
            1 if r.get("completed_at") else 0,
            str(r.get("completed_at") or ""),
            int(r.get("id") or 0),
        ),
    )[-1]


@pytest.mark.unit
class TestOmn16055LiveSeamCapture:
    """Cross-boundary regression: the real probe over a real merged-PR capture."""

    def test_fixture_still_exhibits_the_seam(self) -> None:
        """Guard on the fixture itself — this suite proves nothing if the
        capture stops containing the head-green/merge-red combination.

        If ``omnibase_core``'s dev-push CI is ever repaired and this assertion
        starts failing, that is the signal to RE-RECORD the capture from a PR
        that still exercises the seam (or retire this suite deliberately) —
        not to quietly delete the assertion.
        """
        fixture = _load_fixture()
        required = set(fixture["required_status_checks"]["contexts"])
        head = fixture["head_check_runs"]
        merge = fixture["merge_check_runs"]

        head_green_merge_red = sorted(
            name
            for name in required
            if (hw := _winner(head, name)) is not None
            and _is_green(hw)
            and (mw := _winner(merge, name)) is not None
            and not _is_green(mw)
        )
        head_red = sorted(
            name
            for name in required
            if (hw := _winner(head, name)) is not None and not _is_green(hw)
        )

        # The defect class: contexts green on the merge-gate artifact, red on
        # the post-merge push commit.
        assert head_green_merge_red == [
            "CI Summary",
            "occ-preflight / eligibility",
        ], head_green_merge_red
        # ...and nothing genuinely red on the head SHA, so any not-green
        # verdict from the probe can only come from merge-commit evidence.
        assert head_red == [], head_red

    def test_merged_pr_green_on_head_resolves_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE OMN-16055 RED TEST.

        All 37 required contexts on ``omnibase_core@dev`` are green on PR
        #1557's head SHA ``83b6804e35``. Two of them are red on the squash
        merge commit ``355b4e0326`` purely because the dev-push run is
        structurally non-green there. Before this fix the probe returned
        ``checks_green=False`` with ``required context(s) not green``, which
        deterministically blocked the Done-flip for a PR that passed every
        gate it was actually gated on.
        """
        monkeypatch.setattr(
            hd_mod.subprocess, "run", _replay_live_capture(_load_fixture())
        )
        result = _probe()
        assert result.checks_green is True, result.detail

    def test_push_run_reds_are_reported_not_silently_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-silence invariant: demoting merge-commit reds to non-load-bearing
        must not make them invisible. A dev branch whose push CI is red forever
        is a real problem — it is just not *this ticket's* problem, so it is
        surfaced in the receipt detail rather than gating the flip."""
        monkeypatch.setattr(
            hd_mod.subprocess, "run", _replay_live_capture(_load_fixture())
        )
        result = _probe()
        detail = result.detail or ""
        # Green verdict...
        assert result.checks_green is True, detail
        # ...that still names both demoted contexts and says why.
        assert "CI Summary" in detail, detail
        assert "occ-preflight / eligibility" in detail, detail
        assert "merge commit" in detail, detail
        assert "355b4e032660" in detail, detail


@pytest.mark.unit
class TestOmn16055PrecedenceCells:
    """Every cell of the (head winner x merge winner) truth table.

    Only ONE cell changes: head=green, merge=red. The synthetic fixtures below
    reuse the live capture's shape but override a single required context, so
    each cell is isolated.
    """

    @staticmethod
    def _capture_with(
        *,
        head_conclusion: str | None,
        merge_conclusion: str | None,
        name: str = "CI Summary",
    ) -> Any:
        fixture = _load_fixture()
        # Reduce branch protection to the single context under test so the
        # verdict is attributable to exactly this cell.
        fixture["required_status_checks"] = {"contexts": [name], "strict": False}
        suite_id = int(fixture["head_check_suites"][0]["id"])
        fixture["head_check_suites"] = [
            {"id": suite_id, "head_branch": fixture["pr_view"]["headRefName"]}
        ]
        fixture["head_check_runs"] = (
            []
            if head_conclusion is None
            else [
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": head_conclusion,
                    "check_suite": {"id": suite_id},
                    "id": 1,
                    "completed_at": "2026-08-14T08:00:00Z",
                }
            ]
        )
        fixture["merge_check_runs"] = (
            []
            if merge_conclusion is None
            else [
                {
                    "name": name,
                    "status": "completed",
                    "conclusion": merge_conclusion,
                    "id": 2,
                    "completed_at": "2026-08-14T08:30:00Z",
                }
            ]
        )
        return _replay_live_capture(fixture)

    def test_head_green_merge_red_resolves_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE ONLY CHANGED CELL. The merge-gate artifact is green; the
        post-merge push run is not evidence about this PR's gate."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion="success", merge_conclusion="failure"),
        )
        result = _probe()
        assert result.checks_green is True, result.detail

    def test_head_green_merge_green_resolves_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion="success", merge_conclusion="success"),
        )
        result = _probe()
        assert result.checks_green is True, result.detail

    def test_head_red_merge_green_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Head authority runs in BOTH directions: a green merge-commit run
        must never rehabilitate a context that failed its actual merge gate."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion="failure", merge_conclusion="success"),
        )
        result = _probe()
        assert result.checks_green is False, result.detail
        assert "CI Summary" in (result.detail or "")

    def test_head_red_merge_red_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion="failure", merge_conclusion="failure"),
        )
        result = _probe()
        assert result.checks_green is False, result.detail

    def test_head_absent_merge_red_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15817 shape 2b, UNCHANGED: when the merge commit is the only
        observer of a required context, its red conclusion is fully
        load-bearing and fails closed."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion=None, merge_conclusion="failure"),
        )
        result = _probe()
        assert result.checks_green is False, result.detail
        assert "CI Summary" in (result.detail or "")

    def test_head_absent_merge_green_resolves_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15817 shape 2b, UNCHANGED: sole-observer green resolves green."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion=None, merge_conclusion="success"),
        )
        result = _probe()
        assert result.checks_green is True, result.detail

    def test_head_absent_merge_absent_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero observable evidence anywhere remains fail-closed."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            self._capture_with(head_conclusion=None, merge_conclusion=None),
        )
        result = _probe()
        assert result.checks_green is False, result.detail

    def test_head_pending_merge_red_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A head winner that is present but NOT green (still running) does not
        earn precedence — precedence is granted only by a *green* head winner,
        so this falls through to the unchanged additive evaluation."""
        fixture = _load_fixture()
        name = "CI Summary"
        fixture["required_status_checks"] = {"contexts": [name], "strict": False}
        suite_id = int(fixture["head_check_suites"][0]["id"])
        fixture["head_check_suites"] = [
            {"id": suite_id, "head_branch": fixture["pr_view"]["headRefName"]}
        ]
        fixture["head_check_runs"] = [
            {
                "name": name,
                "status": "in_progress",
                "conclusion": None,
                "check_suite": {"id": suite_id},
                "id": 1,
                "completed_at": None,
            }
        ]
        fixture["merge_check_runs"] = [
            {
                "name": name,
                "status": "completed",
                "conclusion": "failure",
                "id": 2,
                "completed_at": "2026-08-14T08:30:00Z",
            }
        ]
        monkeypatch.setattr(hd_mod.subprocess, "run", _replay_live_capture(fixture))
        result = _probe()
        assert result.checks_green is False, result.detail
