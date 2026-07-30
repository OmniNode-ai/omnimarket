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

import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci.check_occ_companion_merged import (
    EVIDENCE_SOURCE_RE,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_PENDING,
    Verdict,
    aggregate,
    canonical_first_citation,
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

    def test_bulleted_and_bold_forms_are_not_citations(self) -> None:
        # Seam match with occ-preflight/receipt-gate, which extract with
        # `grep -iE '^Evidence-Source:[[:space:]]+\S'` — column 0, no bullet and
        # no bold tolerance. A bulleted/bolded line is prose here for the same
        # reason it is prose there; treating it as a live citation made this the
        # only repo on the fleet whose citation set disagreed with the gate that
        # actually requires the stamp.
        body = (
            "- Evidence-Source: OCC#1\n"
            "**Evidence-Source**: OCC#2\n"
            "  Evidence-Source: OCC#3\n"
            "- **Evidence-Source**: OCC#5487 (superseded)\n"
        )
        assert parse_evidence_sources(body) == []

    def test_column0_regex_regression_pin_hand_transcribed_from_canonical(
        self,
    ) -> None:
        """Regression pin on THIS module's own column-0 regex. NOT a cross-surface
        oracle — see :class:`TestCanonicalBindingParity` for the real one.

        Honesty note (OMN-15475 AC4). A prior revision of this test was titled
        ``test_citation_set_matches_the_canonical_grep`` and documented as a
        "parity oracle" that keeps this parser and the canonical shell extractor
        "from drifting apart silently". It does no such thing:

        1. It compares :data:`EVIDENCE_SOURCE_RE` against a HARDCODED Python
           literal of the same character class, so the two match identical line
           sets by construction.
        2. It never reads ``occ-preflight.yml`` / ``receipt-gate.yml`` — those
           live in ``omnibase_core`` and omnimarket's callers pin ``@main``
           (unpinned), so canonical can change with this test still green.
        3. It was already blind to a LIVE divergence: canonical
           ``[[:space:]]`` ⊋ Python ``[ \\t]``.

        What it actually is, and all it claims: a pin against accidental
        widening of our own regex (bullets, bold, indentation, blockquotes,
        inline mentions), hand-transcribed from
        ``omnibase_core/.github/workflows/occ-preflight.yml`` at
        ``fd2d2942`` (``grep -iE '^Evidence-Source:[[:space:]]+\\S' | head -1``),
        read 2026-07-30.
        """

        canonical = re.compile(
            r"^Evidence-Source:[ \t]+\S", re.IGNORECASE | re.MULTILINE
        )
        body = (
            "Evidence-Ticket: OMN-15427\n"
            "- Evidence-Source: OCC#1\n"
            "**Evidence-Source**: OCC#2\n"
            "  Evidence-Source: OCC#3\n"
            "> Evidence-Source: OCC#4\n"
            "see `Evidence-Source: OCC#5` inline\n"
            "Evidence-Source: OCC#5497\n"
            "evidence-source:\tOCC#5506\n"
        )
        canonical_lines = {line for line in body.splitlines() if canonical.match(line)}
        parsed_lines = {
            line for line in body.splitlines() if EVIDENCE_SOURCE_RE.match(line)
        }
        assert parsed_lines == canonical_lines
        # And the values this gate will actually evaluate.
        assert parse_evidence_sources(body) == ["OCC#5497", "OCC#5506"]

    def test_inline_mention_is_not_a_citation(self) -> None:
        # A prose reference is documentation, not a trailer.
        body = "the body carried `Evidence-Source: OCC#5487`, which was dead"
        assert parse_evidence_sources(body) == []

    def test_fenced_citation_below_the_real_stamp_stays_documentation(self) -> None:
        # The arbiter binds head -1 = the real column-0 stamp, so the fenced
        # example below it is bound by nobody and stays excluded. This is the
        # documentation case fence-stripping exists to serve, and it survives
        # the OMN-15475 fix intact.
        body = (
            "Evidence-Source: OCC#5497\n\nProof of the dead one:\n\n"
            "```\nEvidence-Source: OCC#5487\n```\n"
        )
        assert parse_evidence_sources(body) == ["OCC#5497"]

    def test_indented_fenced_example_is_invisible_to_both_surfaces(self) -> None:
        # The documented safe idiom: indent the example by one space. Neither
        # the canonical grep (anchored ^Evidence-Source: at column 0) nor this
        # module's regex sees it, fence or no fence.
        body = (
            "Proof:\n\n```\n Evidence-Source: OCC#5487\n```\n\n"
            "Evidence-Source: OCC#5497\n"
        )
        assert canonical_first_citation(body) == "OCC#5497"
        assert parse_evidence_sources(body) == ["OCC#5497"]

    def test_tilde_fence_only_body_still_yields_the_canonical_binding(self) -> None:
        # Previously asserted []. The arbiter binds this line (it greps the raw
        # body), so dropping it hid the ONLY thing occ-preflight would pin.
        body = "~~~\nEvidence-Source: OCC#5487\n~~~\n"
        assert canonical_first_citation(body) == "OCC#5487"
        assert parse_evidence_sources(body) == ["OCC#5487"]

    def test_unclosed_fence_no_longer_swallows_the_canonical_binding(self) -> None:
        # An unterminated fence hides everything after it from the REPORTING
        # set. Previously that yielded ZERO citations; the arbiter still bound
        # OCC#5497, so the gate was evaluating a different world than the
        # surface it arbitrates for.
        body = "```\nEvidence-Source: OCC#5497\n"
        assert parse_evidence_sources(body) == ["OCC#5497"]


# --------------------------------------------------------------------------- #
# OMN-15475: parse like the arbiter binds
# --------------------------------------------------------------------------- #

# The literal body shape from OMN-15475: a fenced CLOSED-unmerged citation
# sitting ABOVE the real MERGED stamp. occ-preflight/receipt-gate grep the raw
# body and take head -1, so they bind OCC#5487 (dead) and pin its branch head.
FENCED_DEAD_ABOVE_LIVE_BODY = (
    "Example of a dead citation:\n"
    "\n"
    "```\n"
    "Evidence-Source: OCC#5487\n"
    "```\n"
    "\n"
    "Evidence-Source: OCC#5548\n"
)

# The whitespace-class variant: POSIX [[:space:]] includes \v \f \r; Python's
# [ \t] does not. A form feed between the trailer and the value is a citation to
# the arbiter and was not one here. It is also a line break to
# ``str.splitlines()`` but NOT to ``grep``, which splits on \n only.
FORM_FEED_DEAD_ABOVE_LIVE_BODY = (
    "Evidence-Source:\x0cOCC#5487\n\nEvidence-Source: OCC#5548\n"
)


def _canonical_shell_binding(body: str) -> str | None:
    """Run the REAL canonical pipeline from ``occ-preflight.yml`` via POSIX tools.

    This is the cross-implementation oracle: actual ``grep``/``head``/``sed``,
    not a Python transcription of them. The pipeline is byte-identical to the
    ``Resolve Evidence-Source`` step of ``omnibase_core``'s ``occ-preflight.yml``
    and ``receipt-gate.yml``.
    """

    script = (
        "grep -iE '^Evidence-Source:[[:space:]]+\\S' | head -1 | "
        "sed -E 's/^Evidence-Source:[[:space:]]*//; s/^[[:space:]]+//; "
        "s/[[:space:]]+$//'"
    )
    proc = subprocess.run(  # fixed argv, trusted shell snippet, test-only
        ["bash", "-c", script],
        input=body,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    out = proc.stdout.strip("\n")
    return out or None


class TestCanonicalBindingParity:
    """OMN-15475. The gate must evaluate the citation the arbiter actually binds.

    RED-before/GREEN-after against the committed gate: every assertion in
    :meth:`test_fenced_dead_citation_above_a_live_stamp_is_evaluated`,
    :meth:`test_form_feed_citation_is_evaluated` and
    :meth:`test_gate_fails_on_the_fenced_dead_citation` fails on
    ``origin/dev``'s revision, which returned ``['OCC#5548']`` for both bodies
    and greened.
    """

    def test_fenced_dead_citation_above_a_live_stamp_is_evaluated(self) -> None:
        assert canonical_first_citation(FENCED_DEAD_ABOVE_LIVE_BODY) == "OCC#5487", (
            "the arbiter binds the fenced dead ref — the gate must see it too"
        )
        assert parse_evidence_sources(FENCED_DEAD_ABOVE_LIVE_BODY) == [
            "OCC#5487",
            "OCC#5548",
        ]

    def test_form_feed_citation_is_evaluated(self) -> None:
        assert canonical_first_citation(FORM_FEED_DEAD_ABOVE_LIVE_BODY) == "OCC#5487"
        assert parse_evidence_sources(FORM_FEED_DEAD_ABOVE_LIVE_BODY) == [
            "OCC#5487",
            "OCC#5548",
        ]

    def test_gate_fails_on_the_fenced_dead_citation(self) -> None:
        """End-to-end: the exact OMN-15427 incident class, through the gate."""

        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(FENCED_DEAD_ABOVE_LIVE_BODY),
                (OCC_REPO, "5487"): _occ("CLOSED"),
                (OCC_REPO, "5548"): _occ("MERGED", "f0440822380a"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_FAIL, verdict.reason
        assert "OCC#5487" in verdict.reason

    def test_gate_fails_on_the_form_feed_dead_citation(self) -> None:
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(FORM_FEED_DEAD_ABOVE_LIVE_BODY),
                (OCC_REPO, "5487"): _occ("CLOSED"),
                (OCC_REPO, "5548"): _occ("MERGED", "f0440822380a"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_FAIL, verdict.reason
        assert "OCC#5487" in verdict.reason

    def test_all_merged_fenced_and_live_still_passes(self) -> None:
        """Control: the fix must not turn every documenting body red."""

        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(FENCED_DEAD_ABOVE_LIVE_BODY),
                (OCC_REPO, "5487"): _occ("MERGED", "aaaaaaaaaaaa"),
                (OCC_REPO, "5548"): _occ("MERGED", "f0440822380a"),
            }
        )
        assert _evaluate(fetcher).code == EXIT_PASS

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("", id="empty"),
            pytest.param("no citation anywhere", id="no-citation"),
            pytest.param(FENCED_DEAD_ABOVE_LIVE_BODY, id="fenced-dead-above-live"),
            pytest.param(FORM_FEED_DEAD_ABOVE_LIVE_BODY, id="form-feed"),
            pytest.param("Evidence-Source:\vOCC#1\n", id="vertical-tab"),
            pytest.param("Evidence-Source:\tOCC#5506\n", id="tab"),
            pytest.param("Evidence-Source:   OCC#5506   \n", id="padded"),
            pytest.param("Evidence-Source: OCC#5506\r\nmore\r\n", id="crlf"),
            pytest.param("- Evidence-Source: OCC#1\n", id="bullet"),
            pytest.param("**Evidence-Source**: OCC#2\n", id="bold"),
            pytest.param("  Evidence-Source: OCC#3\n", id="indented"),
            pytest.param("> Evidence-Source: OCC#4\n", id="blockquote"),
            pytest.param("see `Evidence-Source: OCC#5` inline\n", id="inline"),
            pytest.param("Evidence-Source:\n", id="trailer-with-no-value"),
            pytest.param("Evidence-Source:\nOCC#5487\n", id="value-on-next-line"),
            pytest.param(
                "evidence-source: OCC#5487\nEvidence-Source: OCC#5548\n",
                id="lowercase-first",
            ),
            pytest.param("~~~\nEvidence-Source: OCC#5487\n~~~\n", id="tilde-fence"),
            pytest.param("```\nEvidence-Source: OCC#5497\n", id="unclosed-fence"),
        ],
    )
    def test_canonical_binder_matches_the_real_shell_pipeline(self, body: str) -> None:
        """Cross-implementation oracle: our Python binder vs actual POSIX tools.

        Unlike the hand-transcribed regression pin above, this executes the
        canonical pipeline itself, so a divergence in whitespace class, line
        splitting, ``head -1`` selection or ``sed`` trimming is caught rather
        than reproduced. ``bash``/``grep``/``sed`` are present on every hosted
        runner this repo uses (ubuntu-latest) and on the local dev hosts; a
        missing one is an environment defect, so this test errors rather than
        skipping — a skip here would be a vacuous green.
        """

        assert shutil.which("bash") is not None, "bash absent — cannot run the oracle"
        assert canonical_first_citation(body) == _canonical_shell_binding(body)

    def test_canonical_sed_is_case_sensitive_while_its_grep_is_not(self) -> None:
        """Quirk found BY the oracle, pinned so it is recorded rather than lost.

        ``grep -iE`` SELECTS a lowercase ``evidence-source:`` line; the
        following ``sed -E 's/^Evidence-Source:...//'`` is case-SENSITIVE and
        does not strip it, so the arbiter's resolved value is the whole line and
        it hard-fails the PR ("not a valid OCC#<number> or hex SHA"). No dead
        ref can hide in this shape — the arbiter pins nothing — so this gate
        re-strips the trailer and evaluates the companion that was meant.
        """

        body = "evidence-source: OCC#5487\nEvidence-Source: OCC#5548\n"
        assert _canonical_shell_binding(body) == "evidence-source: OCC#5487"
        assert canonical_first_citation(body) == "evidence-source: OCC#5487"
        assert parse_evidence_sources(body) == ["OCC#5487", "OCC#5548"]


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
