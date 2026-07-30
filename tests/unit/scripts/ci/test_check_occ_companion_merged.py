# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verdict tests for the occ-companion-merged STRICT gate (OMN-15427 port of OMN-15214).

The gate makes a dead OCC evidence citation unreachable at omnimarket's merge
boundary: the product PR's required ``CI Summary`` context cannot go green until
EVERY cited companion is MERGED (or every cited SHA is already an ancestor of an
OCC durable branch). The live gap this closes: ``omnimarket#1953`` cited
``OCC#5487``, a companion CLOSED without merging, and nothing in omnimarket CI
noticed.

Pinned verdict table:

* companion MERGED             → PASS
* companion OPEN               → PENDING (poll; deadline converts to FAIL)
* companion CLOSED unmerged    → FAIL immediately (the #1953 state)
* SHA ancestor of dev/main     → PASS
* SHA not an ancestor          → FAIL (OMN-15216 strandable pre-merge pin)
* missing Evidence-Source      → PENDING (autobind mint may be in flight)
* malformed Evidence-Source    → FAIL
* dependency-bot author        → PASS (mirrors occ-preflight OMN-13762)
* non-PR event                 → PASS (gate not applicable)
* unresolvable PR number       → FAIL (fail closed)
* API errors                   → PENDING (retryable), never PASS
* ANY dead citation among many → FAIL (the first-line-only evasion this port closes)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci.check_occ_companion_merged import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_PENDING,
    Verdict,
    aggregate,
    evaluate_once,
    main,
    parse_evidence_sources,
    resolve_pr_number,
)

pytestmark = pytest.mark.unit

PRODUCT_REPO = "OmniNode-ai/omnimarket"
OCC_REPO = "OmniNode-ai/onex_change_control"


class FakeFetcher:
    """Deterministic stand-in for GhFetcher (``None`` == API failure)."""

    def __init__(
        self,
        *,
        prs: dict[tuple[str, str], dict[str, object] | None] | None = None,
        compare: dict[tuple[str, str], str | None] | None = None,
    ) -> None:
        self._prs = prs or {}
        self._compare = compare or {}

    def pr_view(self, repo: str, number: str, fields: str) -> dict[str, object] | None:
        return self._prs.get((repo, str(number)))

    def compare_status(self, repo: str, base: str, head_sha: str) -> str | None:
        return self._compare.get((base, head_sha))


def _product_pr(body: str, author: str = "product-pr-author") -> dict[str, object]:
    return {"body": body, "author": {"login": author}}


def _evaluate(fetcher: FakeFetcher, **kwargs: Any) -> Verdict:
    defaults: dict[str, Any] = {
        "event_name": "pull_request",
        "repo": PRODUCT_REPO,
        "pr_number": "1953",
        "occ_repo": OCC_REPO,
    }
    defaults.update(kwargs)
    return evaluate_once(fetcher, **defaults)  # type: ignore[arg-type]


def _occ(state: str, oid: str = "") -> dict[str, object]:
    return {"state": state, "mergeCommit": {"oid": oid} if oid else None}


def _pr_view_stub(
    *,
    default: dict[str, object] | None = None,
    by_number: dict[str, dict[str, object] | None] | None = None,
) -> Callable[[object, str, str, str], dict[str, object] | None]:
    """A ``GhFetcher.pr_view`` replacement for ``monkeypatch.setattr``."""

    def _pr_view(
        _self: object, _repo: str, number: str, _fields: str
    ) -> dict[str, object] | None:
        if by_number is not None:
            return by_number[str(number)]
        return default

    return _pr_view


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class TestEvidenceSourceParsing:
    def test_every_citation_is_returned_in_order(self) -> None:
        body = "intro\nevidence-source:  OCC#5487 \nEvidence-Source: OCC#5497\n"
        assert parse_evidence_sources(body) == ["OCC#5487", "OCC#5497"]

    def test_duplicates_collapse_case_insensitively(self) -> None:
        body = "Evidence-Source: OCC#5497\nevidence-source: occ#5497\n"
        assert parse_evidence_sources(body) == ["OCC#5497"]

    def test_absent_returns_empty(self) -> None:
        assert parse_evidence_sources("no evidence here") == []
        assert parse_evidence_sources("") == []

    def test_bulleted_and_bold_citations_are_matched(self) -> None:
        body = "- Evidence-Source: OCC#1\n**Evidence-Source**: OCC#2\n"
        assert parse_evidence_sources(body) == ["OCC#1", "OCC#2"]

    def test_inline_mention_is_not_a_citation(self) -> None:
        # A prose reference is documentation, not a trailer.
        body = "the body carried `Evidence-Source: OCC#5487`, which was dead"
        assert parse_evidence_sources(body) == []

    def test_fenced_block_is_not_a_citation(self) -> None:
        body = (
            "Proof:\n\n```\nEvidence-Source: OCC#5487\n```\n\n"
            "Evidence-Source: OCC#5497\n"
        )
        assert parse_evidence_sources(body) == ["OCC#5497"]

    def test_tilde_fence_is_also_excluded(self) -> None:
        body = "~~~\nEvidence-Source: OCC#5487\n~~~\n"
        assert parse_evidence_sources(body) == []

    def test_unclosed_fence_swallows_the_rest_and_fails_closed_upstream(self) -> None:
        # An unterminated fence hides everything after it. That yields ZERO
        # citations, which evaluate_once turns into PENDING → FAIL at the
        # deadline — never a green.
        body = "```\nEvidence-Source: OCC#5497\n"
        assert parse_evidence_sources(body) == []


class TestPrNumberResolution:
    def test_pull_request_number_passthrough(self) -> None:
        assert resolve_pr_number("pull_request", "1953", "") == "1953"

    def test_merge_group_head_ref_parse(self) -> None:
        ref = "refs/heads/gh-readonly-queue/dev/pr-456-0123abc"
        assert resolve_pr_number("merge_group", "", ref) == "456"

    def test_unresolvable_returns_empty(self) -> None:
        assert resolve_pr_number("merge_group", "", "refs/heads/whatever") == ""


# --------------------------------------------------------------------------- #
# Companion-PR citations
# --------------------------------------------------------------------------- #


class TestCompanionPrVerdicts:
    def test_merged_companion_passes(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: OCC#5497"),
                (OCC_REPO, "5497"): _occ("MERGED", "159f036e26b4ba4fc107e96c665"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_PASS, verdict.reason
        assert "MERGED" in verdict.reason

    def test_open_companion_is_pending_not_pass(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: OCC#5497"),
                (OCC_REPO, "5497"): _occ("OPEN"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_PENDING, verdict.reason

    def test_closed_unmerged_companion_fails_immediately(self) -> None:
        # THE omnimarket#1953 shape: OCC#5487 was CLOSED without merging.
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: OCC#5487"),
                (OCC_REPO, "5487"): _occ("CLOSED"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_FAIL, verdict.reason
        assert "OCC#5487" in verdict.reason
        assert "without merging" in verdict.reason

    def test_companion_fetch_error_is_pending_never_pass(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: OCC#5497"),
                (OCC_REPO, "5497"): None,
            }
        )
        assert _evaluate(fetcher).code == EXIT_PENDING

    def test_unknown_state_string_fails_closed(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: OCC#5487"),
                (OCC_REPO, "5487"): {"state": "", "mergeCommit": None},
            }
        )
        assert _evaluate(fetcher).code == EXIT_FAIL


# --------------------------------------------------------------------------- #
# Multi-citation aggregation — the evasion this port closes
# --------------------------------------------------------------------------- #


class TestMultipleCitations:
    def test_one_dead_citation_among_merged_ones_fails(self) -> None:
        # First-line-only parsing (the omniclaude port's shape) would have
        # greened this: OCC#5497 is merged and appears first.
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "Evidence-Source: OCC#5497\nEvidence-Source: OCC#5487\n"
                ),
                (OCC_REPO, "5497"): _occ("MERGED", "159f036e"),
                (OCC_REPO, "5487"): _occ("CLOSED"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_FAIL, verdict.reason
        assert "OCC#5487" in verdict.reason

    def test_all_merged_citations_pass(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "Evidence-Source: OCC#5497\nEvidence-Source: OCC#5506\n"
                ),
                (OCC_REPO, "5497"): _occ("MERGED", "159f036e"),
                (OCC_REPO, "5506"): _occ("MERGED", "99537d17"),
            }
        )
        assert _evaluate(fetcher).code == EXIT_PASS

    def test_fail_beats_pending(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "Evidence-Source: OCC#5497\nEvidence-Source: OCC#5487\n"
                ),
                (OCC_REPO, "5497"): _occ("OPEN"),
                (OCC_REPO, "5487"): _occ("CLOSED"),
            }
        )
        assert _evaluate(fetcher).code == EXIT_FAIL

    def test_aggregate_of_empty_set_fails_closed(self) -> None:
        assert aggregate([]).code == EXIT_FAIL

    def test_aggregate_precedence(self) -> None:
        assert aggregate([Verdict(EXIT_PASS, "a")]).code == EXIT_PASS
        assert (
            aggregate([Verdict(EXIT_PASS, "a"), Verdict(EXIT_PENDING, "b")]).code
            == EXIT_PENDING
        )
        assert (
            aggregate([Verdict(EXIT_PENDING, "b"), Verdict(EXIT_FAIL, "c")]).code
            == EXIT_FAIL
        )


# --------------------------------------------------------------------------- #
# SHA citations
# --------------------------------------------------------------------------- #


class TestShaVerdicts:
    def test_sha_ancestor_of_dev_passes(self) -> None:
        sha = "159f036e26b4ba4fc107e96c6655d72e3adb5c2b"
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr(f"Evidence-Source: {sha}")},
            compare={("dev", sha): "behind"},
        )
        assert _evaluate(fetcher).code == EXIT_PASS

    def test_sha_identical_to_main_passes(self) -> None:
        sha = "99537d176de84cdafde85d9ff6b8ef36423c6700"
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr(f"Evidence-Source: {sha}")},
            compare={("dev", sha): "diverged", ("main", sha): "identical"},
        )
        assert _evaluate(fetcher).code == EXIT_PASS

    def test_non_ancestor_sha_fails_terminally(self) -> None:
        # OCC is squash-only: a feature-branch head SHA can never become an
        # ancestor of dev/main, so this must never be PENDING.
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr(f"Evidence-Source: {sha}")},
            compare={("dev", sha): "diverged", ("main", sha): "ahead"},
        )
        assert _evaluate(fetcher).code == EXIT_FAIL

    def test_compare_api_error_is_pending(self) -> None:
        sha = "abc1234"
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr(f"Evidence-Source: {sha}")},
            compare={("dev", sha): None, ("main", sha): None},
        )
        assert _evaluate(fetcher).code == EXIT_PENDING


# --------------------------------------------------------------------------- #
# Applicability / body states
# --------------------------------------------------------------------------- #


class TestApplicability:
    def test_missing_evidence_source_is_pending(self) -> None:
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr("no trailer yet")}
        )
        assert _evaluate(fetcher).code == EXIT_PENDING

    def test_malformed_evidence_source_fails(self) -> None:
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: not-a-ref")}
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_FAIL
        assert "neither" in verdict.reason

    def test_dependency_bot_author_passes(self) -> None:
        fetcher = FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr("bump", author="dependabot[bot]")}
        )
        assert _evaluate(fetcher).code == EXIT_PASS

    def test_non_gating_event_passes(self) -> None:
        assert _evaluate(FakeFetcher(), event_name="push").code == EXIT_PASS
        assert (
            _evaluate(FakeFetcher(), event_name="workflow_dispatch").code == EXIT_PASS
        )

    def test_merge_group_event_is_enforced(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr("Evidence-Source: OCC#5487"),
                (OCC_REPO, "5487"): _occ("CLOSED"),
            }
        )
        assert _evaluate(fetcher, event_name="merge_group").code == EXIT_FAIL

    def test_unresolvable_pr_number_fails_closed(self) -> None:
        assert _evaluate(FakeFetcher(), pr_number="").code == EXIT_FAIL

    def test_product_pr_fetch_error_is_pending(self) -> None:
        fetcher = FakeFetcher(prs={(PRODUCT_REPO, "1953"): None})
        assert _evaluate(fetcher).code == EXIT_PENDING

    def test_override_bypasses_body_read(self) -> None:
        fetcher = FakeFetcher(prs={(OCC_REPO, "5487"): _occ("CLOSED")})
        verdict = _evaluate(fetcher, evidence_source_override=["OCC#5487"])
        assert verdict.code == EXIT_FAIL


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_once_returns_verdict_code_without_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str]] = []

        def fake_pr_view(
            self: object, repo: str, number: str, fields: str
        ) -> dict[str, object] | None:
            calls.append((repo, number))
            return _occ("CLOSED")

        monkeypatch.setattr(
            "scripts.ci.check_occ_companion_merged.GhFetcher.pr_view", fake_pr_view
        )
        code = main(
            [
                "--repo",
                PRODUCT_REPO,
                "--pr-number",
                "1953",
                "--evidence-source",
                "OCC#5487",
                "--once",
            ]
        )
        assert code == EXIT_FAIL
        assert calls == [(OCC_REPO, "5487")]

    def test_once_pending_is_reported_as_pending_not_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.ci.check_occ_companion_merged.GhFetcher.pr_view",
            _pr_view_stub(default=_occ("OPEN")),
        )
        code = main(
            [
                "--repo",
                PRODUCT_REPO,
                "--pr-number",
                "1953",
                "--evidence-source",
                "OCC#5497",
                "--once",
            ]
        )
        assert code == EXIT_PENDING

    def test_poll_deadline_converts_pending_to_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.ci.check_occ_companion_merged.GhFetcher.pr_view",
            _pr_view_stub(default=_occ("OPEN")),
        )
        monkeypatch.setattr(
            "scripts.ci.check_occ_companion_merged.time.sleep", lambda _s: None
        )
        code = main(
            [
                "--repo",
                PRODUCT_REPO,
                "--pr-number",
                "1953",
                "--evidence-source",
                "OCC#5497",
                "--deadline-seconds",
                "0",
                "--poll-interval-seconds",
                "0",
            ]
        )
        assert code == EXIT_FAIL

    def test_repeatable_evidence_source_flag_checks_every_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "scripts.ci.check_occ_companion_merged.GhFetcher.pr_view",
            _pr_view_stub(
                by_number={
                    "5497": _occ("MERGED", "159f036e"),
                    "5487": _occ("CLOSED"),
                }
            ),
        )
        code = main(
            [
                "--repo",
                PRODUCT_REPO,
                "--pr-number",
                "1953",
                "--evidence-source",
                "OCC#5497",
                "--evidence-source",
                "OCC#5487",
                "--once",
            ]
        )
        assert code == EXIT_FAIL
