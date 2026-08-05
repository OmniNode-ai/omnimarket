# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Focused tests for HandlerDodEvidenceGithubEffect (OMN-14400, RSD-1 of OMN-14398).

Covers the canonical EFFECT handler's ``.handle()`` boundary directly, and
proves ``EvidenceCollector``'s delegating wrapper methods
(``_lookup_pr_for_ticket``, ``_lookup_repo_for_ticket``,
``_fetch_pr_merge_state``, ``_fetch_pr_checks_green``) resolve to identical
results after the carve-out — the whole point of a behavior-identical
refactor. ``subprocess.run`` is patched at the handler module (where the gh
CLI invocations now live); since ``subprocess`` is a singleton module object,
this patches the same attribute the pre-existing
``test_omn_14207_live_pr_state_check.py::TestGhOutputParsing`` suite patches
via ``ec_mod.subprocess`` — both target sets exercise the same underlying
call.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind

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
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_REPO = "OmniNode-ai/omnibase_infra"
_PR = 2216


def _fake_completed(stdout: str, returncode: int = 0, stderr: str = "") -> object:
    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        argv = list(args[0]) if args else []
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _run


def _lines(*objs: dict[str, object]) -> str:
    """Join fixture objects as JSON-lines, matching what
    ``gh api --paginate --jq '....[]'`` actually emits on stdout (one JSON
    document per array element, NOT a single JSON array)."""
    return "\n".join(json.dumps(o) for o in objs)


def _routed_gh(
    *,
    view: str = "",
    view_rc: int = 0,
    protection: str = "",
    protection_rc: int = 0,
    rules: str = "[]",
    rules_rc: int = 0,
    suites: str = "",
    suites_rc: int = 0,
    runs: str = "",
    runs_rc: int = 0,
) -> object:
    """Route ``gh`` invocations for FETCH_PR_CHECKS_GREEN's 5-call sequence
    (``pr view`` -> classic branch protection -> branch rules -> check-suites
    -> check-runs) to the fixture matching each call's shape, keyed by
    distinctive substrings in the invocation argv rather than call order."""

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        argv = [str(a) for a in (list(args[0]) if args else [])]
        joined = " ".join(argv)
        if "view" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=view_rc, stdout=view, stderr=""
            )
        if "protection/required_status_checks" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=protection_rc, stdout=protection, stderr=""
            )
        if "rules/branches" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=rules_rc, stdout=rules, stderr=""
            )
        if "check-suites" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=suites_rc, stdout=suites, stderr=""
            )
        if "check-runs" in joined:
            return subprocess.CompletedProcess(
                args=argv, returncode=runs_rc, stdout=runs, stderr=""
            )
        raise AssertionError(
            f"unrouted gh invocation in FETCH_PR_CHECKS_GREEN test: {argv}"
        )

    return _run


# ---------------------------------------------------------------------------
# HandlerDodEvidenceGithubEffect.handle() — the canonical EFFECT boundary.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerDodEvidenceGithubEffectHandle:
    def test_handle_returns_effect_output_with_single_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed('{"mergedAt":"2026-07-01T00:00:00Z","state":"MERGED"}'),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE,
            repo=_REPO,
            pr_number=_PR,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)

        assert output.node_kind == EnumNodeKind.EFFECT
        assert output.correlation_id == command.correlation_id
        assert len(output.events) == 1
        result = output.events[0]
        assert result.correlation_id == command.correlation_id
        assert result.operation == EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE
        assert result.merged is True
        assert result.state == "MERGED"

    def test_unknown_operation_raises(self) -> None:
        # Constructing a command requires a valid enum member, so simulate an
        # unrecognized operation by bypassing validation via model_construct.
        command = ModelDodEvidenceGithubLookupCommand.model_construct(
            operation="not-a-real-operation",  # type: ignore[arg-type]
            correlation_id=ModelDodEvidenceGithubLookupCommand.model_fields[
                "correlation_id"
            ].default_factory(),
        )
        with pytest.raises(ValueError, match="Unknown operation"):
            HandlerDodEvidenceGithubEffect().handle(command)


# ---------------------------------------------------------------------------
# LOOKUP_PR_FOR_TICKET / LOOKUP_REPO_FOR_TICKET
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLookupOperations:
    def test_lookup_pr_for_ticket_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OMN-15382: LOOKUP_PR_FOR_TICKET now requires an explicit ``repo``
        # (never guesses one) and requests number+title+headRefName so it can
        # filter to an exact ticket-id-token match instead of trusting
        # ``gh``'s fuzzy full-text ranking + a blind ``.[0]`` take.
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"jonah/omn-13996-x"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == "2216"
        assert output.events[0].error_code is None

    def test_lookup_pr_for_ticket_missing_repo_fails_closed(self) -> None:
        """OMN-15382: no repo => fail closed WITHOUT ever calling gh."""
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_pr_for_ticket_ambiguous_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: 2+ exact-token candidates => fail closed, never the
        most recent."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"a"},'
                '{"number":2217,"title":"fix(OMN-13996): y",'
                '"headRefName":"b"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_AMBIGUOUS"

    def test_lookup_pr_for_ticket_mismatched_candidate_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: a fuzzy-matched but wrong-ticket candidate (the
        OMN-15382 incident shape — a similarly-worded PR for a different
        ticket ranked first) must not be silently trusted."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2454,"title":"fix(OMN-13995): unrelated",'
                '"headRefName":"someone/omn-13995-fix"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_pr_for_ticket_gh_invocation_passes_repo_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            argv = [str(a) for a in (list(args[0]) if args else [])]
            captured["argv"] = argv
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=(
                    '[{"number":2216,"title":"fix(OMN-13996): x","headRefName":"a"}]'
                ),
                stderr="",
            )

        monkeypatch.setattr(hd_mod.subprocess, "run", _run)
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        HandlerDodEvidenceGithubEffect().handle(command)
        assert "--repo" in captured["argv"]
        assert _REPO in captured["argv"]

    def test_lookup_pr_for_ticket_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("[]"))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-99999",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_pr_for_ticket_gh_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("", returncode=1))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "PR_LOOKUP_FAILED"

    def test_lookup_repo_for_ticket_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"a","headRepository":'
                '{"nameWithOwner":"OmniNode-ai/omnibase_infra"}}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == "OmniNode-ai/omnibase_infra"
        assert output.events[0].error_code is None

    def test_lookup_repo_for_ticket_no_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("[]"))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-99999",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "REPO_LOOKUP_FAILED"

    def test_lookup_repo_for_ticket_ambiguous_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":1,"title":"fix(OMN-13996): a","headRefName":"x",'
                '"headRepository":{"nameWithOwner":"OmniNode-ai/omnimarket"}},'
                '{"number":2,"title":"fix(OMN-13996): b","headRefName":"y",'
                '"headRepository":{"nameWithOwner":"OmniNode-ai/omnibase_core"}}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == ""
        assert output.events[0].error_code == "REPO_LOOKUP_AMBIGUOUS"

    def test_lookup_repo_for_ticket_two_prs_same_repo_is_not_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CodeRabbit (PR #1949): two merged PRs for one ticket in the SAME
        repo (e.g. a follow-up fix) is zero repo ambiguity — cardinality
        must be judged on the distinct repo set, not the PR-candidate
        count."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":1,"title":"fix(OMN-13996): a","headRefName":"x",'
                '"headRepository":{"nameWithOwner":"OmniNode-ai/omnimarket"}},'
                '{"number":2,"title":"fix(OMN-13996): b","headRefName":"y",'
                '"headRepository":{"nameWithOwner":"OmniNode-ai/omnimarket"}}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id="OMN-13996",
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == "OmniNode-ai/omnimarket"
        assert output.events[0].error_code is None

    def test_lookup_pr_for_ticket_matches_lowercased_branch_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CodeRabbit (PR #1949): branch names are conventionally lowercased
        (``jonah/omn-13996-x``) while ``ticket_id`` arrives uppercase — the
        ``headRefName`` match arm must be case-insensitive or it never fires
        in practice."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"a totally unrelated title",'
                '"headRefName":"jonah/omn-13996-fix-thing"}]'
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id="OMN-13996",
            repo=_REPO,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        assert output.events[0].text_value == "2216"
        assert output.events[0].error_code is None


# ---------------------------------------------------------------------------
# FETCH_PR_CHECKS_GREEN (OMN-15709 rewrite) — head-branch-scoped evidence.
# GitHub's Checks API is keyed by commit SHA, not by PR: a check-run
# attached to a DIFFERENT PR/branch sharing the same head SHA must never be
# able to redden (or whitewash) THIS PR's evidence. required_status_checks
# context names are read live from branch protection instead of trusted to
# ``gh pr checks --required``'s own filtering.
# ---------------------------------------------------------------------------

_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_SHA = "ed2f0084b59aed9ed91a8ebb68877c6f2cd77d1b"
_OCC_JONAH_BRANCH = "jonah/omn-15136-infra769-occ"
_OCC_CODEX_BRANCH = "codex/omn-15136-infra769-occ"
_OCC_REQUIRED_CONTEXTS = json.dumps(
    [
        "CI Summary",
        "required-check-skip-guard / check-skip-vectors",
        "verify / verify",
        "occ-preflight / eligibility",
    ]
)


def _occ_pr_view(branch: str) -> str:
    return json.dumps(
        {"headRefName": branch, "baseRefName": "dev", "headRefOid": _OCC_SHA}
    )


# Sanitized subset of the real 43-suite / 132-check-run rollup GitHub returns
# for OCC PR #5745 (jonah's own PR, MERGED) / #5749 (codex's sibling PR,
# CLOSED) sharing head SHA ``ed2f0084`` — live-verified 2026-08-05 (OMN-15709
# ruling R-b), CORRECTED 2026-08-05 (opus adversarial verify, wave-8 D2): an
# independent paginated pull of this SHA's 43 check-suites / 132 check-runs,
# each suite refetched individually via
# ``gh api repos/.../check-suites/{id}``, showed all 4 required contexts ran
# on jonah's OWN branch (``jonah/omn-15136-infra769-occ``) and all 4 were
# ``success`` there — suite ids 83069374087 (CI Summary), 83069370828
# (required-check-skip-guard / check-skip-vectors), 83069371620
# (verify / verify), 83069370125 (occ-preflight / eligibility). The prior
# fixture omitted jonah's own runs for 3 of those 4 contexts, which inverted
# the case under test (it exercised the foreign-only-exclusion path — D1's
# fail-open hole — instead of the real own-branch-all-green shape #5745
# actually has). ``check_suite.pull_requests[]`` was empty on all 43 real
# suites, so branch attribution here is via ``head_branch``, not that field.
# Codex's sibling branch (#5749) genuinely does carry its own real failures
# for ``verify / verify`` and ``occ-preflight / eligibility`` on this same
# SHA — those foreign suites/runs are retained below as a control: they must
# stay excluded from #5745's rollup (own-branch success wins) while still
# reddening #5749's own rollup (own-branch failure, see
# ``test_5749_stays_red_for_its_own_legitimate_failures``).
_OCC_SUITES = _lines(
    {"id": 83069374087, "head_branch": _OCC_JONAH_BRANCH},  # own CI Summary
    {"id": 83069370828, "head_branch": _OCC_JONAH_BRANCH},  # own skip-guard
    {"id": 83069371620, "head_branch": _OCC_JONAH_BRANCH},  # own verify
    {"id": 83069370125, "head_branch": _OCC_JONAH_BRANCH},  # own occ-preflight
    {"id": 83132908639, "head_branch": _OCC_CODEX_BRANCH},  # foreign CI Summary
    {"id": 83132908530, "head_branch": _OCC_CODEX_BRANCH},  # foreign skip-guard
    {"id": 83179811101, "head_branch": _OCC_CODEX_BRANCH},  # foreign skip-guard
    {"id": 83132908796, "head_branch": _OCC_CODEX_BRANCH},  # foreign verify (FAIL)
    {
        "id": 83132908739,
        "head_branch": _OCC_CODEX_BRANCH,
    },  # foreign occ-preflight (FAIL)
    {
        "id": 83179811806,
        "head_branch": _OCC_CODEX_BRANCH,
    },  # foreign occ-preflight (FAIL)
)
_OCC_RUNS = _lines(
    {
        "name": "CI Summary",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83069374087},
    },
    {
        "name": "required-check-skip-guard / check-skip-vectors",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83069370828},
    },
    {
        "name": "verify / verify",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83069371620},
    },
    {
        "name": "occ-preflight / eligibility",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83069370125},
    },
    {
        "name": "CI Summary",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83132908639},
    },
    {
        "name": "required-check-skip-guard / check-skip-vectors",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83132908530},
    },
    {
        "name": "required-check-skip-guard / check-skip-vectors",
        "status": "completed",
        "conclusion": "success",
        "check_suite": {"id": 83179811101},
    },
    {
        "name": "verify / verify",
        "status": "completed",
        "conclusion": "failure",
        "check_suite": {"id": 83132908796},
    },
    {
        "name": "occ-preflight / eligibility",
        "status": "completed",
        "conclusion": "failure",
        "check_suite": {"id": 83132908739},
    },
    {
        "name": "occ-preflight / eligibility",
        "status": "completed",
        "conclusion": "failure",
        "check_suite": {"id": 83179811806},
    },
)


@pytest.mark.unit
class TestFetchPrChecksGreenOccRegression:
    """AC3: reproduces the live OCC #5745/#5749 shape end to end."""

    def test_5745_reports_green_from_its_own_branch_check_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#5745 is jonah's own, MERGED PR. Its own branch produced a green
        check-run for all 4 required contexts (live-verified 2026-08-05,
        wave-8 D2 correction — see the ``_OCC_SUITES``/``_OCC_RUNS`` module
        comment). Codex's sibling branch (#5749) independently produced its
        own foreign runs for 3 of those names on the same head SHA, including
        2 outright FAILUREs for ``verify / verify`` and
        ``occ-preflight / eligibility``. Pre this fix, ``gh pr checks 5745``
        would report all of these (including the foreign FAILUREs) against
        #5745 and return NOT green — the exact defect OMN-15709 reports.
        Post-fix: the foreign runs are excluded from #5745's rollup by
        head-branch attribution, and #5745's own 4 required contexts are all
        green, so #5745 correctly reports GREEN for the true reason (its own
        branch's evidence), not merely because a foreign-only context was
        silently dropped from the count (the fail-open shape covered
        separately by
        ``TestFetchPrChecksGreenScoping::test_own_branch_all_green`` and its
        negative-control sibling
        ``test_required_context_produced_only_by_foreign_branch_fails_closed``)."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view(_OCC_JONAH_BRANCH),
                protection=_OCC_REQUIRED_CONTEXTS,
                suites=_OCC_SUITES,
                runs=_OCC_RUNS,
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_OCC_REPO,
            pr_number=5745,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = output.events[0]
        assert result.checks_green is True, result.detail
        assert _OCC_JONAH_BRANCH in (result.detail or "")

    def test_5749_stays_red_for_its_own_legitimate_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#5749 (codex's sibling PR) is a mirror-image control: its OWN
        branch produced the 2 real FAILUREs (verify/verify,
        occ-preflight/eligibility) — these must stay RED. The fix must not
        over-correct into never failing anything on a shared SHA."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view(_OCC_CODEX_BRANCH),
                protection=_OCC_REQUIRED_CONTEXTS,
                suites=_OCC_SUITES,
                runs=_OCC_RUNS,
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_OCC_REPO,
            pr_number=5749,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = output.events[0]
        assert result.checks_green is False
        assert "verify / verify" in (result.detail or "")
        assert "occ-preflight / eligibility" in (result.detail or "")

    def test_naive_unscoped_rollup_would_have_been_red_for_5745(self) -> None:
        """Documents the pre-fix defect directly against the same fixture
        data: aggregating ALL check-runs for the SHA with NO branch
        attribution (the old ``gh pr checks``-shaped behavior) marks
        ``verify / verify`` and ``occ-preflight / eligibility`` not-green —
        entirely from #5749's check-runs — which is exactly what made
        #5745's evidence permanently unreadable before this fix."""
        runs = [json.loads(line) for line in _OCC_RUNS.splitlines()]
        unscoped_not_green = {
            r["name"]
            for r in runs
            if r["conclusion"] not in ("success", "skipped", "neutral")
        }
        assert unscoped_not_green == {"verify / verify", "occ-preflight / eligibility"}


@pytest.mark.unit
class TestFetchPrChecksGreenScoping:
    _REQUIRED = json.dumps(["build"])

    def test_classic_checks_only_requirement_reports_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub's classic branch protection exposes modern required checks
        under ``checks[].context`` rather than the deprecated ``contexts``
        list; those names must still be enforced."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps(
                    {"contexts": [], "checks": [{"context": "build"}]}
                ),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    }
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is True, result.detail

    def test_ruleset_only_requirement_reports_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Active branch rulesets can define the required status checks even
        when classic branch protection carries no context names."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps({"contexts": [], "checks": []}),
                rules=json.dumps(
                    [
                        {
                            "type": "required_status_checks",
                            "parameters": {
                                "required_status_checks": [
                                    {"context": "build", "integration_id": 15368}
                                ]
                            },
                        }
                    ]
                ),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    }
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is True, result.detail

    def test_own_branch_all_green(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=self._REQUIRED,
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    }
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is True

    def test_own_branch_failure_stays_red_even_when_foreign_is_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real own-branch FAILURE must never be rescued by a foreign
        sibling's success — the mirror image of the OCC case, proving the
        fix is not simply 'always trust the friendliest result'."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=self._REQUIRED,
                suites=_lines(
                    {"id": 1, "head_branch": "mine"},
                    {"id": 2, "head_branch": "theirs"},
                ),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 2},
                    },
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False
        assert "build" in (result.detail or "")

    def test_ambiguous_suite_attribution_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: a check-run whose ``check_suite.id`` cannot be resolved
        against the check-suites listing (never appears there) is NOT
        silently excluded — attribution is ambiguous, so it counts against
        green even though it is nominally a FAILURE that might belong to a
        foreign branch."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=self._REQUIRED,
                suites=_lines(),  # empty: suite id 99 is unresolved
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 99},
                    }
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False

    def test_required_context_produced_only_by_foreign_branch_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A required context that NEVER appears on the PR's own branch, but
        does appear on a resolvable foreign branch, must fail closed (RED),
        never be silently dropped from the rollup — regardless of whether
        the foreign instance is green or red, and regardless of whether
        OTHER required contexts did land on the PR's own branch.

        This is the wave-8 adversarial-verify repro (D1, opus): with
        required=[build, verify] and own-branch build=success only, adding
        ONE foreign-branch verify=FAILURE run must NOT flip the result to
        GREEN — a foreign failure attaching to the SHA must never make a PR's
        evidence LOOSER than it was before that foreign PR/branch existed.
        Prior to the fix, the handler `continue`d past a foreign-only
        context without recording it as missing or not-green, so it vanished
        from both `missing` and `not_green` and the rollup returned
        checks_green=True with a detail string asserting both contexts were
        green — false, since `verify` never ran on `mine` at all."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps(["build", "verify"]),
                suites=_lines(
                    {"id": 1, "head_branch": "mine"},
                    {"id": 2, "head_branch": "theirs"},
                ),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "verify",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 2},
                    },
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False, result.detail
        assert "verify" in (result.detail or "")
        # The detail string must be truthful: it must not claim "verify" was
        # ever green for "mine" — it never ran there at all.
        assert "all 2 required context(s) green" not in (result.detail or "")

    def test_required_context_produced_only_by_foreign_success_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirror-image control of the case above: even a GREEN foreign-only
        run must not satisfy a required context on the PR's own branch — a
        foreign PASS must never rescue this PR's evidence either. The OCC
        #5745 shape (3 of 4 required contexts foreign-only) is the wave-8 D2
        correction: live data showed all 4 actually ran on jonah's own
        branch too (see the ``_OCC_SUITES``/``_OCC_RUNS`` fixture), so this
        synthetic case — rather than the OCC fixture — is what now covers
        the "foreign-only, foreign is green" shape."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps(["build", "lint"]),
                suites=_lines(
                    {"id": 1, "head_branch": "mine"},
                    {"id": 2, "head_branch": "theirs"},
                ),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 2},
                    },
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False, result.detail
        assert "lint" in (result.detail or "")

    def test_required_context_missing_entirely_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct from the foreign-only-exclusion case above: a required
        context with ZERO check-runs anywhere on the SHA (not even a
        foreign one) is genuinely missing and must fail closed, never be
        silently treated the same as 'excluded'."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps(["build", "never-ran"]),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    }
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False
        assert "never-ran" in (result.detail or "")

    def test_all_required_contexts_foreign_only_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If EVERY required context is only ever produced on a foreign
        branch (own branch produced no relevant/ambiguous CI signal at
        all), that is functionally the same as the pre-existing 'no status
        checks reported' fail-closed case, not a green pass by omission."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps(["build"]),
                suites=_lines({"id": 2, "head_branch": "theirs"}),
                runs=_lines(
                    {
                        "name": "build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 2},
                    }
                ),
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False

    def test_pr_view_unresolvable_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _routed_gh(view="", view_rc=1))
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False
        assert "head/base branch" in (result.detail or "")

    def test_pr_view_missing_fields_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(view=json.dumps({"headRefName": "mine"})),  # no base/sha
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False

    def test_required_status_checks_unresolvable_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E.g. the base branch has no branch protection at all (404)."""
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(view=_occ_pr_view("mine"), protection="", protection_rc=1),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False
        assert "required status checks" in (result.detail or "")

    def test_empty_required_contexts_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(view=_occ_pr_view("mine"), protection="[]"),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False

    def test_check_suites_unresolvable_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=self._REQUIRED,
                suites="",
                suites_rc=1,
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False
        assert "check-suites" in (result.detail or "")

    def test_check_runs_unresolvable_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=self._REQUIRED,
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs="",
                runs_rc=1,
            ),
        )
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False
        assert "check-runs" in (result.detail or "")

    def test_gh_pr_view_timeout_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            raise subprocess.TimeoutExpired(cmd="gh", timeout=30)

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=_REPO,
            pr_number=_PR,
        )
        result = HandlerDodEvidenceGithubEffect().handle(command).events[0]
        assert result.checks_green is False


# ---------------------------------------------------------------------------
# EvidenceCollector delegation — proves behavior-identical parity: the
# public wrapper methods on EvidenceCollector keep their exact pre-refactor
# signatures/return shapes after delegating to the new EFFECT handler.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvidenceCollectorDelegatesToEffectHandler:
    def test_lookup_pr_for_ticket_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # OMN-15382: PR lookup now REQUIRES a resolvable repo before it will
        # call gh at all; REPO env var is the simplest way to supply one from
        # outside an evidence-item context.
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.setenv("REPO", _REPO)
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x","headRefName":"a"}]'
            ),
        )
        collector = EvidenceCollector()
        assert collector._lookup_pr_for_ticket("OMN-13996") == "2216"

    def test_lookup_pr_for_ticket_no_repo_fails_closed_without_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: no REPO env var, no id-derived binding => fail closed
        WITHOUT ever calling gh (closes the root cause: the prior code ran an
        unscoped ``gh pr list`` that resolved whatever repo the process cwd's
        git remote pointed at)."""
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.delenv("REPO", raising=False)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("gh must not be invoked with no resolvable repo")

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        collector = EvidenceCollector()
        assert collector._lookup_pr_for_ticket("OMN-13996") == ""
        assert collector._last_pr_lookup_error is not None
        assert "PR_LOOKUP_FAILED" in collector._last_pr_lookup_error

    def test_lookup_pr_for_ticket_id_derived_binding_skips_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-15382: when the current evidence item's id follows the
        ``dod-<owner>-<repo>-pr-<number>`` autobind convention, both repo and
        PR number resolve from it directly — zero gh calls."""
        monkeypatch.delenv("PR_NUMBER", raising=False)
        monkeypatch.delenv("REPO", raising=False)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("gh must not be invoked for an id-derived binding")

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        collector = EvidenceCollector()
        collector._current_evidence_item_id = "dod-OmniNode-ai-omnibase_infra-pr-2536"
        assert collector._lookup_pr_for_ticket("OMN-13996") == "2536"

    def test_lookup_pr_for_ticket_env_short_circuit_skips_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PR_NUMBER", "4242")

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("gh must not be invoked when PR_NUMBER is set")

        monkeypatch.setattr(hd_mod.subprocess, "run", _boom)
        collector = EvidenceCollector()
        assert collector._lookup_pr_for_ticket("OMN-13996") == "4242"

    def test_lookup_repo_for_ticket_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REPO", raising=False)
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed(
                '[{"number":2216,"title":"fix(OMN-13996): x",'
                '"headRefName":"a","headRepository":'
                '{"nameWithOwner":"OmniNode-ai/omnibase_infra"}}]'
            ),
        )
        collector = EvidenceCollector()
        assert (
            collector._lookup_repo_for_ticket("OMN-13996")
            == "OmniNode-ai/omnibase_infra"
        )

    def test_fetch_pr_merge_state_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _fake_completed('{"mergedAt":null,"state":"OPEN"}'),
        )
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) == (False, "OPEN")

    def test_fetch_pr_merge_state_unresolvable_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hd_mod.subprocess, "run", _fake_completed("", returncode=1))
        collector = EvidenceCollector()
        assert collector._fetch_pr_merge_state(_REPO, _PR) is None

    def test_fetch_pr_checks_green_delegates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hd_mod.subprocess,
            "run",
            _routed_gh(
                view=_occ_pr_view("mine"),
                protection=json.dumps(["Lint", "Build"]),
                suites=_lines({"id": 1, "head_branch": "mine"}),
                runs=_lines(
                    {
                        "name": "Lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "check_suite": {"id": 1},
                    },
                    {
                        "name": "Build",
                        "status": "completed",
                        "conclusion": "success",
                        "check_suite": {"id": 1},
                    },
                ),
            ),
        )
        collector = EvidenceCollector()
        green, detail = collector._fetch_pr_checks_green(_REPO, _PR)
        assert green is False
        assert "Lint" in detail
