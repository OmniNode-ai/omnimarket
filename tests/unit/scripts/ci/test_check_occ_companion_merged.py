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
* missing Evidence-Source AND the producer reported ERROR on this head
                              → FAIL immediately, naming the reason (OMN-18069)
* malformed Evidence-Source    → FAIL
* dependency-bot author        → PASS (mirrors occ-preflight OMN-13762)
* non-PR event                 → PASS (gate not applicable)
* unresolvable PR number       → FAIL (fail closed)
* API errors                   → PENDING (retryable), never PASS
* ANY dead citation among many → FAIL (the first-line-only evasion this port closes)
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from scripts.ci.check_occ_companion_merged import (
    _ANY_LOCALE_SPACE_RE_FRAG,
    AUTOBIND_OUTCOME_CHECK_NAME,
    AUTOBIND_OUTCOME_MARKER_PREFIX,
    EVIDENCE_SOURCE_RE,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_PENDING,
    HAND_AUTHORING_REFERENCE,
    Verdict,
    _normalize_canonical_value,
    aggregate,
    canonical_binding_candidates,
    canonical_first_citation,
    evaluate_once,
    is_no_companion_required,
    is_permanent_decline,
    main,
    parse_evidence_sources,
    read_autobind_outcome,
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
        check_runs: dict[tuple[str, str], list[dict[str, object]] | None] | None = None,
    ) -> None:
        self._prs = prs or {}
        self._compare = compare or {}
        # OMN-18069. Default `[]` = "the producer posted no outcome", the
        # ordinary case; `None` = "the read itself failed", which must be a
        # distinct input because the gate may never treat one as the other.
        self._check_runs = check_runs or {}

    def pr_view(self, repo: str, number: str, fields: str) -> dict[str, object] | None:
        return self._prs.get((repo, str(number)))

    def compare_status(self, repo: str, base: str, head_sha: str) -> str | None:
        return self._compare.get((base, head_sha))

    def check_runs(self, repo: str, head_sha: str) -> list[dict[str, object]] | None:
        return self._check_runs.get((repo, head_sha), [])


def _product_pr(
    body: str,
    author: str = "product-pr-author",
    head_sha: str = "615219ec46868e2ebf09f8b35a9e6cfc6d743dea",
) -> dict[str, object]:
    return {"body": body, "author": {"login": author}, "headRefOid": head_sha}


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

# The LOCALE variant, and the residual that survived the first cut of the
# OMN-15475 fix. ``[[:space:]]`` is not a fixed class: under the hosted runner's
# default ``C.UTF-8`` (GNU grep 3.11 on ubuntu-24.04) it also covers U+2003 EM
# SPACE, so the arbiter binds the DEAD OCC#5487 here while a binder pinned to
# ``[ \t\v\f\r]`` sees only the live OCC#5548 and greens. Identical to the shape
# above; different codepoint.
EM_SPACE_DEAD_ABOVE_LIVE_BODY = (
    "Evidence-Source:\u2003OCC#5487\n\nEvidence-Source: OCC#5548\n"
)

# Locales the oracle drives. Each one's ``[[:space:]]`` is MEASURED off the
# platform's own grep rather than assumed; an uninstalled locale degrades to C,
# which is still a real (if duplicated) oracle run, so nothing is ever skipped.
ORACLE_LOCALES = ("C", "C.UTF-8", "en_US.UTF-8")

# Everything Python calls whitespace lives below U+3001; probing the whole range
# (rather than a hand-picked list) is what makes the derived class a measurement
# instead of another transcription. ``\n`` is excluded — it is the line
# separator, not an in-line space.
_PROBE_CODEPOINTS = tuple(cp for cp in range(0x01, 0x3001) if cp != 0x0A)


@functools.cache
def _platform_space_chars(locale: str) -> str:
    """The characters THIS platform's ``grep`` accepts for ``[[:space:]]``.

    Executes real ``grep`` over one probe line per codepoint. The class is
    locale- AND libc-dependent — 5 chars under ``LC_ALL=C``, 20 under glibc
    ``C.UTF-8``, and wider again under BSD ``en_US.UTF-8`` (which adds U+0085 /
    U+00A0 / U+202F) — so the gate cannot hardcode it and neither can this test.
    """

    probe = "\n".join(f"E{cp}:{chr(cp)}X" for cp in _PROBE_CODEPOINTS) + "\n"
    proc = subprocess.run(  # fixed argv, no shell, test-only
        ["grep", "-E", "^E[0-9]+:[[:space:]]+X"],
        input=probe.encode("utf-8"),
        capture_output=True,
        env={**os.environ, "LC_ALL": locale},
        check=False,
        timeout=60,
    )
    found: list[str] = []
    for line in proc.stdout.decode("utf-8", "replace").split("\n"):
        match = re.match(r"^E(\d+):", line)
        if match:
            found.append(chr(int(match.group(1))))
    return "".join(found)


def _canonical_shell_binding(body: str, *, locale: str = "C") -> str | None:
    """Run the REAL canonical pipeline from ``occ-preflight.yml`` via POSIX tools.

    This is the cross-implementation oracle: actual ``grep``/``head``/``sed``,
    not a Python transcription of them. The pipeline is byte-identical to the
    ``Resolve Evidence-Source`` step of ``omnibase_core``'s ``occ-preflight.yml``
    and ``receipt-gate.yml``.

    Bytes in, bytes out, deliberately. An earlier revision passed ``text=True``,
    which universal-newline-translates ``\\r`` to ``\\n`` in captured stdout —
    for the CR shapes that is a corruption of the very whitespace class under
    test, and it manufactured a spurious mismatch. The direction was fail-loud
    rather than a bypass, but an oracle that mangles its own subject cannot be
    trusted to certify the class.
    """

    script = (
        "grep -iE '^Evidence-Source:[[:space:]]+\\S' | head -1 | "
        "sed -E 's/^Evidence-Source:[[:space:]]*//; s/^[[:space:]]+//; "
        "s/[[:space:]]+$//'"
    )
    proc = subprocess.run(  # fixed argv, trusted shell snippet, test-only
        ["bash", "-c", script],
        input=body.encode("utf-8"),
        capture_output=True,
        env={**os.environ, "LC_ALL": locale},
        check=False,
        timeout=30,
    )
    out = proc.stdout.decode("utf-8").strip("\n")
    return out or None


# Bodies the oracle is driven over. Every whitespace class member the platform
# might disagree about gets a shape, ASCII and non-ASCII alike.
ORACLE_BODIES = [
    pytest.param("", id="empty"),
    pytest.param("no citation anywhere", id="no-citation"),
    pytest.param(FENCED_DEAD_ABOVE_LIVE_BODY, id="fenced-dead-above-live"),
    pytest.param(FORM_FEED_DEAD_ABOVE_LIVE_BODY, id="form-feed"),
    pytest.param(EM_SPACE_DEAD_ABOVE_LIVE_BODY, id="em-space-dead-above-live"),
    pytest.param("Evidence-Source:\vOCC#1\n", id="vertical-tab"),
    pytest.param("Evidence-Source:\tOCC#5506\n", id="tab"),
    pytest.param("Evidence-Source:   OCC#5506   \n", id="padded"),
    pytest.param("Evidence-Source: OCC#5506\r\nmore\r\n", id="crlf"),
    pytest.param("Evidence-Source: OCC#5506\rEvidence-Source: OCC#1\n", id="bare-cr"),
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
    # One shape per non-ASCII codepoint measured in a real grep space class on
    # at least one of the two hosts this fleet runs on. NONE of these were in
    # the first cut's 18 bodies, which is exactly why that cut shipped with the
    # hole still open. Written as escapes, never as literal invisible glyphs.
    pytest.param(
        "Evidence-Source:\u1680OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u1680-ogham",
    ),
    pytest.param(
        "Evidence-Source:\u2000OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u2000-en-quad",
    ),
    pytest.param(
        "Evidence-Source:\u2003OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u2003-em-space",
    ),
    pytest.param(
        "Evidence-Source:\u2028OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u2028-line-sep",
    ),
    pytest.param(
        "Evidence-Source:\u2029OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u2029-para-sep",
    ),
    pytest.param(
        "Evidence-Source:\u205fOCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u205f-medium-math",
    ),
    pytest.param(
        "Evidence-Source:\u3000OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u3000-ideographic",
    ),
    pytest.param(
        "Evidence-Source:\u0085OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u0085-nel",
    ),
    pytest.param(
        "Evidence-Source:\u00a0OCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u00a0-nbsp",
    ),
    pytest.param(
        "Evidence-Source:\u202fOCC#5487\n\nEvidence-Source: OCC#5548\n",
        id="u202f-narrow-nbsp",
    ),
    pytest.param("Evidence-Source:\u2003 OCC#5506 \u2003\n", id="exotic-padding"),
]


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

    def test_em_space_dead_citation_above_a_live_stamp_is_evaluated(self) -> None:
        """The residual the first cut of this fix left open (OMN-15475 round 2).

        ``[[:space:]]`` is not ``[ \\t\\v\\f\\r]``; that is only its ``LC_ALL=C``
        value. On the hosted runner (``C.UTF-8``, GNU grep 3.11, ubuntu-24.04)
        the class covers U+2003, so the arbiter binds the DEAD ``OCC#5487`` in
        this body. A binder pinned to the C class returned ``['OCC#5548']`` and
        the gate greened — the OMN-15475 defect surviving the OMN-15475 fix.
        """

        assert canonical_binding_candidates(EM_SPACE_DEAD_ABOVE_LIVE_BODY) == [
            "OCC#5487",
            "OCC#5548",
        ]
        assert parse_evidence_sources(EM_SPACE_DEAD_ABOVE_LIVE_BODY) == [
            "OCC#5487",
            "OCC#5548",
        ]

    def test_gate_fails_on_the_em_space_dead_citation(self) -> None:
        """End-to-end, and locale-independent: the exploit cannot green the gate."""

        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(EM_SPACE_DEAD_ABOVE_LIVE_BODY),
                (OCC_REPO, "5487"): _occ("CLOSED"),
                (OCC_REPO, "5548"): _occ("MERGED", "f0440822380a"),
            }
        )
        verdict = _evaluate(fetcher)
        assert verdict.code == EXIT_FAIL, verdict.reason
        assert "OCC#5487" in verdict.reason

    def test_ordinary_body_yields_exactly_one_candidate(self) -> None:
        """Fail-closed enumeration must not inflate the everyday path.

        A plain stamp is both the first candidate AND the guaranteed stop, so
        exactly one value is produced and no extra companion is demanded.
        """

        assert canonical_binding_candidates("Evidence-Source: OCC#5497\n") == [
            "OCC#5497"
        ]
        assert canonical_binding_candidates(
            "Evidence-Source: OCC#5497\nEvidence-Source: OCC#5487\n"
        ) == ["OCC#5497"]

    @pytest.mark.parametrize("locale", ORACLE_LOCALES)
    @pytest.mark.parametrize("body", ORACLE_BODIES)
    def test_arbiter_binding_is_always_inside_the_evaluated_set(
        self, body: str, locale: str
    ) -> None:
        """THE load-bearing invariant, and the one the first cut did not have.

        Whatever the arbiter binds — under any locale, on any libc — must be in
        the set this gate evaluates. Otherwise the gate can green on a body whose
        pinned citation is dead, which is the entire OMN-15475 defect.

        This holds without the gate knowing the arbiter's locale, because
        :func:`canonical_binding_candidates` enumerates every possible binding
        instead of reproducing one. Verified against REAL ``grep``/``head``/``sed``
        under each locale, not a transcription of them.
        """

        assert shutil.which("bash") is not None, "bash absent — cannot run the oracle"
        binding = _canonical_shell_binding(body, locale=locale)
        if binding is None:
            # The arbiter resolves nothing and fails the PR for a missing stamp;
            # there is no binding for this gate to be wrong about.
            return
        candidates = [
            _normalize_canonical_value(value)
            for value in canonical_binding_candidates(body)
        ]
        assert _normalize_canonical_value(binding) in candidates, (
            f"LC_ALL={locale}: arbiter binds {binding!r} but the gate evaluates "
            f"{candidates!r} — a dead pinned ref would green this gate"
        )

    @pytest.mark.parametrize("locale", ORACLE_LOCALES)
    @pytest.mark.parametrize("body", ORACLE_BODIES)
    def test_canonical_binder_matches_the_real_shell_pipeline(
        self, body: str, locale: str
    ) -> None:
        """Exact-reproduction oracle, per locale, with the class MEASURED.

        Unlike the hand-transcribed regression pin above, this executes the
        canonical pipeline itself, so a divergence in whitespace class, line
        splitting, ``head -1`` selection or ``sed`` trimming is caught rather
        than reproduced. The space class fed to the Python binder is derived by
        running the platform's own ``grep`` (see :func:`_platform_space_chars`) —
        hardcoding it is what produced the round-2 defect.

        ``bash``/``grep``/``sed`` are present on every hosted runner this repo
        uses and on the local dev hosts; a missing one is an environment defect,
        so this errors rather than skipping — a skip here would be a vacuous
        green.
        """

        assert shutil.which("bash") is not None, "bash absent — cannot run the oracle"
        space = _platform_space_chars(locale)
        assert canonical_first_citation(body, space_chars=space) == (
            _canonical_shell_binding(body, locale=locale)
        )

    @pytest.mark.parametrize("locale", ORACLE_LOCALES)
    def test_measured_space_class_is_covered_by_the_widest_class(
        self, locale: str
    ) -> None:
        """The containment the fail-closed enumeration rests on.

        :func:`canonical_binding_candidates` treats "not an ASCII printable" as a
        superset of every real ``[[:space:]]``, resting on POSIX's requirement
        that the ``space`` and ``graph`` classes are disjoint and that the
        portable character set's classification is fixed. If a platform ever
        violated that, the enumeration would stop being a superset and the
        wrong-binding hole would silently reopen.

        This assertion is not decorative — it is what caught the fact that
        Python's ``\\s`` is NOT such a superset: BSD grep under ``en_US.UTF-8``
        classifies U+200B ZERO WIDTH SPACE as ``[[:space:]]`` and Python does
        not, so a ``\\s``-based widest class would have left a live bypass on
        this very host.
        """

        space = _platform_space_chars(locale)
        assert space, f"LC_ALL={locale}: grep matched no space characters at all"
        uncovered = [
            hex(ord(char))
            for char in space
            if re.match(_ANY_LOCALE_SPACE_RE_FRAG, char) is None and char != "\n"
        ]
        assert uncovered == [], (
            f"LC_ALL={locale}: real [[:space:]] contains {uncovered}, which is "
            "an ASCII printable — POSIX says space and graph are disjoint, so "
            "the candidate enumeration is no longer a superset of the "
            "arbiter's selection"
        )

    @pytest.mark.parametrize("locale", ORACLE_LOCALES)
    def test_python_unicode_space_is_not_by_itself_a_safe_widest_class(
        self, locale: str
    ) -> None:
        """Records WHY the widest class is a POSIX complement, not an enumeration.

        Vacuity guard on the choice above: if this ever stops finding a
        divergence on every host, the enumerated class was fine after all and
        this test says so out loud rather than leaving a silent over-design.
        On macOS/BSD ``en_US.UTF-8`` the divergence is U+200B.
        """

        space = _platform_space_chars(locale)
        missed = [hex(ord(c)) for c in space if re.match(r"[^\S\n]", c) is None]
        # Not asserted non-empty: glibc's class IS inside Python's. The point is
        # that at least one host/locale in this matrix escapes it, and the
        # module must be correct on all of them.
        assert all(re.match(_ANY_LOCALE_SPACE_RE_FRAG, c) for c in space), missed

    def test_c_locale_class_is_the_narrowest_and_utf8_is_wider(self) -> None:
        """Records the measurement the module's fidelity note 2 cites.

        Not a tautology: it asserts the ORDERING the whole design rests on (the C
        class is contained in every other class, so a C-class match is a
        guaranteed match everywhere) directly against the platform's grep.
        """

        c_class = set(_platform_space_chars("C"))
        assert c_class == set(" \t\v\f\r"), sorted(hex(ord(c)) for c in c_class)
        for locale in ORACLE_LOCALES:
            assert c_class <= set(_platform_space_chars(locale)), (
                f"LC_ALL={locale} class does not contain the C class — the "
                "guaranteed-stop regex would no longer be guaranteed"
            )

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
    @pytest.fixture(autouse=True)
    def _pin_cli_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the CLI's argparse defaults out of the ambient environment."""
        for name in (
            "GH_REPO",
            "PR_NUMBER",
            "GITHUB_EVENT_NAME",
            "MERGE_GROUP_HEAD_REF",
            "OCC_REPO",
        ):
            monkeypatch.delenv(name, raising=False)

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


class TestAutobindOutcomeShortCircuit:
    """OMN-18069 — the gate asks the producer instead of waiting it out.

    Fixtures are the REAL 2026-09-09 records: omninode_infra#1266 at head
    ``615219ec…`` (correlation ``d856d7ff-2044-4e3b-af1a-6d14ae892743``,
    published to ``onex.cmd.omnimarket.occ-autobind.v1`` partition 0 offset
    4508), whose autobind was consumed and then failed with
    ``Could not parse the provided public key.`` — one of 37 identical
    failures that each cost this gate its full 1500-second deadline.
    """

    HEAD = "615219ec46868e2ebf09f8b35a9e6cfc6d743dea"
    REASON = "failed: Could not parse the provided public key."

    def _outcome_run(
        self,
        outcome: str,
        *,
        name: str = AUTOBIND_OUTCOME_CHECK_NAME,
        completed_at: str = "2026-09-09T04:20:29Z",
        reason: str | None = None,
    ) -> dict[str, object]:
        summary = (
            f"{AUTOBIND_OUTCOME_MARKER_PREFIX} {outcome} "
            f"repo=OmniNode-ai/omninode_infra pr=1266 "
            f"correlation_id=d856d7ff-2044-4e3b-af1a-6d14ae892743 "
            f"reason={reason if reason is not None else self.REASON}\n\n"
            "prose a human reads\n"
        )
        return {
            "name": name,
            "status": "completed",
            "completed_at": completed_at,
            "output": {"title": f"{outcome}: x", "summary": summary},
        }

    def _fetcher(self, runs: list[dict[str, object]] | None) -> FakeFetcher:
        return FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "no evidence yet", head_sha=self.HEAD
                )
            },
            check_runs={(PRODUCT_REPO, self.HEAD): runs},
        )

    def test_reported_error_fails_immediately_naming_the_reason(self) -> None:
        verdict = _evaluate(self._fetcher([self._outcome_run("ERROR")]))
        assert verdict.code == EXIT_FAIL
        assert "Could not parse the provided public key." in verdict.reason
        assert "will NOT appear" in verdict.reason

    def test_no_outcome_posted_still_polls(self) -> None:
        """The ordinary in-flight case is unchanged — this is additive."""
        assert _evaluate(self._fetcher([])).code == EXIT_PENDING

    def test_an_unreadable_check_run_list_never_fails_the_pr(self) -> None:
        """Fail-OPEN here on purpose: the evidence is written by another repo's
        runtime, and an outage there must not become an outage on this gate."""
        assert _evaluate(self._fetcher(None)).code == EXIT_PENDING

    def test_a_recoverable_declined_outcome_still_polls(self) -> None:
        """A RECOVERABLE decline may still resolve — another producer can be
        minting — so polling through it stays correct (OMN-18647)."""
        run = self._outcome_run(
            "DECLINED",
            reason=(
                "skip:LEASE_HELD — OmniNode-ai/omnimarket#2627@1346e01f companion "
                "already being minted by another producer (OMN-14793 / OMN-14783)"
            ),
        )
        assert _evaluate(self._fetcher([run])).code == EXIT_PENDING

    def test_a_draft_suppression_decline_still_polls(self) -> None:
        """The OMN-14741 F-17 suppressions resolve on their own: a draft PR
        marked ready re-fires the publisher. Failing here would fail a PR whose
        companion is merely not due yet."""
        run = self._outcome_run(
            "DECLINED",
            reason=(
                "skip:DRAFT — OmniNode-ai/omnimarket#2627 is not a mergeable "
                "product PR; OCC companion emission suppressed (OMN-14741 F-17)"
            ),
        )
        assert _evaluate(self._fetcher([run])).code == EXIT_PENDING

    def test_a_minted_outcome_still_polls_for_the_body_patch(self) -> None:
        assert _evaluate(self._fetcher([self._outcome_run("MINTED")])).code == (
            EXIT_PENDING
        )

    def test_a_check_run_with_another_name_is_ignored(self) -> None:
        runs = [self._outcome_run("ERROR", name="occ-autobind / mint status")]
        assert _evaluate(self._fetcher(runs)).code == EXIT_PENDING

    def test_the_newest_outcome_wins(self) -> None:
        """Offsets 4508 and 4510 are the same PR: two dispatches, two outcomes."""
        runs = [
            self._outcome_run("ERROR", completed_at="2026-09-09T04:20:29Z"),
            self._outcome_run(
                "MINTED",
                completed_at="2026-09-09T04:45:17Z",
                reason="authored OCC#8760",
            ),
        ]
        assert _evaluate(self._fetcher(runs)).code == EXIT_PENDING

    def test_an_incomplete_check_run_is_not_read(self) -> None:
        run = self._outcome_run("ERROR")
        run["status"] = "in_progress"
        assert _evaluate(self._fetcher([run])).code == EXIT_PENDING

    def test_a_present_evidence_source_bypasses_the_probe_entirely(self) -> None:
        """The short-circuit only ever replaces a would-be timeout."""
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "Evidence-Source: OCC#8760", head_sha=self.HEAD
                ),
                (OCC_REPO, "8760"): {
                    "state": "MERGED",
                    "mergeCommit": {"oid": "a" * 40},
                },
            },
            check_runs={(PRODUCT_REPO, self.HEAD): [self._outcome_run("ERROR")]},
        )
        assert _evaluate(fetcher).code == EXIT_PASS


class TestPermanentDeclineIsTerminal:
    """OMN-18647 — a permanent DECLINED ends the poll instead of outliving it.

    Fixtures are the REAL 2026-09-17 record: ``omnimarket#2627`` at head
    ``1346e01f…``, publisher run ``35283318630`` publishing event
    ``6c64f69e-7a4c-43a1-b200-3c193e043d61`` at 22:42:02Z, whose autobind was
    consumed and DECLINED 61 seconds later at 22:43:03Z. The gate read that
    check-run and discarded it, then failed at 23:07:35Z -- which is the poll
    start of 22:42:35Z plus the 1500-second deadline, to the second. Sibling
    PRs ``#2623`` and ``#2624`` sat the same way for over six hours each.
    """

    HEAD = "1346e01fd6f470c97c8fc3392d3c6ec28afda7d9"
    CORRELATION = "6c64f69e-7a4c-43a1-b200-3c193e043d61"
    NO_RED_REASON = (
        "skip:NO_RED_DERIVABLE_CHECK — OmniNode-ai/omnimarket#2627: no "
        "changed-file candidate is RED-derivable against the merge base; "
        "hand-authored evidence is required (OMN-15247)"
    )
    DEFER_REASON = (
        "skip:DEFER_HAND_AUTHORED — OmniNode-ai/omnimarket#2627: a "
        "hand-authored companion is already in contention (OMN-15247)"
    )

    def _fetcher(self, reason: str) -> FakeFetcher:
        summary = (
            f"{AUTOBIND_OUTCOME_MARKER_PREFIX} DECLINED "
            f"repo=OmniNode-ai/omnimarket pr=2627 "
            f"correlation_id={self.CORRELATION} reason={reason}\n\n"
            "OCC companion NOT verified on this head\n"
        )
        return FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "no evidence yet", head_sha=self.HEAD
                )
            },
            check_runs={
                (PRODUCT_REPO, self.HEAD): [
                    {
                        "name": AUTOBIND_OUTCOME_CHECK_NAME,
                        "status": "completed",
                        "completed_at": "2026-09-17T22:43:03Z",
                        "output": {"title": "DECLINED", "summary": summary},
                    }
                ]
            },
        )

    def test_no_red_derivable_fails_immediately_with_the_producers_words(
        self,
    ) -> None:
        verdict = _evaluate(self._fetcher(self.NO_RED_REASON))
        assert verdict.code == EXIT_FAIL
        assert "NO_RED_DERIVABLE_CHECK" in verdict.reason
        assert "hand-authored evidence is required" in verdict.reason

    def test_the_failure_names_the_hand_authoring_path_not_the_clock(self) -> None:
        """The pre-fix user-visible failure was 'stamp_absent -- poll deadline
        (1500s) reached', which describes the clock. The reason the author can
        act on must be the one they are shown."""
        verdict = _evaluate(self._fetcher(self.NO_RED_REASON))
        assert HAND_AUTHORING_REFERENCE in verdict.reason
        assert "poll deadline" not in verdict.reason
        assert "permanent refusal" in verdict.reason

    def test_defer_hand_authored_is_also_terminal(self) -> None:
        verdict = _evaluate(self._fetcher(self.DEFER_REASON))
        assert verdict.code == EXIT_FAIL
        assert "DEFER_HAND_AUTHORED" in verdict.reason

    def test_an_unreadable_check_run_list_still_never_fails_the_pr(self) -> None:
        """The fail-OPEN property must survive the new terminal case: another
        repo's runtime outage must not become an outage on this gate."""
        fetcher = FakeFetcher(
            prs={
                (PRODUCT_REPO, "1953"): _product_pr(
                    "no evidence yet", head_sha=self.HEAD
                )
            },
            check_runs={(PRODUCT_REPO, self.HEAD): None},
        )
        assert _evaluate(fetcher).code == EXIT_PENDING


class TestIsPermanentDecline:
    """The verdict is read off the reason TOKEN, never the prose after it."""

    def test_both_permanent_tokens_match(self) -> None:
        assert is_permanent_decline("skip:NO_RED_DERIVABLE_CHECK — anything")
        assert is_permanent_decline("skip:DEFER_HAND_AUTHORED — anything")

    def test_recoverable_tokens_do_not_match(self) -> None:
        assert not is_permanent_decline("skip:LEASE_HELD — another producer")
        assert not is_permanent_decline("skip:DRAFT — not a mergeable product PR")

    def test_leading_whitespace_is_tolerated(self) -> None:
        assert is_permanent_decline("  skip:NO_RED_DERIVABLE_CHECK — x")

    def test_an_empty_reason_is_not_permanent(self) -> None:
        """A decline with no reason recorded is ambiguous, and ambiguity keeps
        the old behaviour: poll, do not fail the PR."""
        assert not is_permanent_decline("")

    def test_the_token_must_lead_not_merely_appear(self) -> None:
        """Prose quoting the token — a reason that merely MENTIONS the refusal,
        e.g. while explaining why it did not apply — must not be read as one."""
        assert not is_permanent_decline(
            "skip:LEASE_HELD — held while skip:NO_RED_DERIVABLE_CHECK was evaluated"
        )


class TestReadAutobindOutcome:
    def test_marker_line_is_parsed_off_the_summary(self) -> None:
        summary = (
            f"{AUTOBIND_OUTCOME_MARKER_PREFIX} ERROR repo=r pr=1 "
            "correlation_id=c reason=boom happened\n\nprose\n"
        )
        parsed = read_autobind_outcome(
            [
                {
                    "name": AUTOBIND_OUTCOME_CHECK_NAME,
                    "status": "completed",
                    "completed_at": "2026-09-09T00:00:00Z",
                    "output": {"summary": summary},
                }
            ]
        )
        assert parsed == ("ERROR", "boom happened")

    def test_a_summary_with_no_marker_yields_none(self) -> None:
        assert (
            read_autobind_outcome(
                [
                    {
                        "name": AUTOBIND_OUTCOME_CHECK_NAME,
                        "status": "completed",
                        "output": {"summary": "just prose"},
                    }
                ]
            )
            is None
        )

    def test_an_empty_list_yields_none(self) -> None:
        assert read_autobind_outcome([]) is None

    def test_non_dict_entries_are_skipped(self) -> None:
        assert read_autobind_outcome(["nonsense", 3]) is None  # type: ignore[list-item]


class TestDependencyPinOnlyExemption:
    """OMN-18848 — a dependency-pin-only diff needs no companion, and the
    exemption is DERIVED rather than asserted.

    Fixture is the real wedged record: ``omnimarket#2685`` at head
    ``4db59ba8…``, a post-release bump to 0.4.133 whose entire diff is
    ``pyproject.toml`` (+1/-1) and ``uv.lock`` (+1/-1). Its autobind DECLINED
    with ``skip:NO_RED_DERIVABLE_CHECK`` and, since OMN-18647, this gate failed
    it. Twelve such PRs (2651-2686) were open and unmergeable at the time this
    test was written, and every bump that HAD merged (2646, 2644, 2641, 2632,
    2610, 2605, 2595) got there only via a hand-authored companion, one each.

    The four fail-closed fences below are the point of the class. The exemption
    is only safe because it cannot be reached any way except the producer
    classifying THIS head's diff.
    """

    HEAD = "4db59ba8c73e78cc605e8a3f0291009d5566cc76"
    OTHER_HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    CORRELATION = "fdf45a4c-c361-4453-a2e2-9804625ad2cd"
    PIN_ONLY_REASON = (
        "skip:DEPENDENCY_PIN_ONLY — OmniNode-ai/omnimarket#2685: no companion "
        "required: dependency-pin-only diff (version/dependency-pin keys only: "
        "project.version); no behavioural claim exists to falsify (OMN-18848)"
    )
    NO_RED_REASON = (
        "skip:NO_RED_DERIVABLE_CHECK — OmniNode-ai/omnimarket#2685: no "
        "changed-file candidate is RED-derivable against the merge base; "
        "hand-authored evidence is required (OMN-15247)"
    )

    def _outcome_run(self, reason: str) -> dict[str, object]:
        summary = (
            f"{AUTOBIND_OUTCOME_MARKER_PREFIX} DECLINED "
            f"repo=OmniNode-ai/omnimarket pr=2685 "
            f"correlation_id={self.CORRELATION} reason={reason}\n\n"
            "prose a human reads\n"
        )
        return {
            "name": AUTOBIND_OUTCOME_CHECK_NAME,
            "status": "completed",
            "completed_at": "2026-09-19T15:48:53Z",
            "output": {"title": "DECLINED", "summary": summary},
        }

    def _fetcher(
        self,
        *,
        body: str = "no evidence yet",
        outcome_sha: str | None = HEAD,
        reason: str = PIN_ONLY_REASON,
    ) -> FakeFetcher:
        check_runs: dict[tuple[str, str], list[dict[str, object]] | None] = {
            (PRODUCT_REPO, self.HEAD): []
        }
        if outcome_sha is not None:
            check_runs[(PRODUCT_REPO, outcome_sha)] = [self._outcome_run(reason)]
        return FakeFetcher(
            prs={(PRODUCT_REPO, "1953"): _product_pr(body, head_sha=self.HEAD)},
            check_runs=check_runs,
        )

    def test_pin_only_on_the_current_head_passes(self) -> None:
        """AC1 — the wedged shape, unwedged."""
        verdict = _evaluate(self._fetcher())
        assert verdict.code == EXIT_PASS
        assert "needs no OCC companion" in verdict.reason
        assert "dependency-pin-only" in verdict.reason

    def test_the_pass_says_it_is_bound_to_this_head(self) -> None:
        """An exemption a reader cannot tell the scope of is one the next lane
        will over-apply. The verdict states the binding in its own words."""
        verdict = _evaluate(self._fetcher())
        assert "bound to this head SHA" in verdict.reason
        assert "new commit re-opens the gate" in verdict.reason

    def test_a_stale_outcome_under_another_sha_does_not_pass(self) -> None:
        """AC4 — the outcome is fetched for the PR's CURRENT head. An outcome
        recorded against an earlier commit must not carry forward: the whole
        point of deriving the verdict from the diff is that a new diff gets a
        new verdict."""
        verdict = _evaluate(self._fetcher(outcome_sha=self.OTHER_HEAD))
        assert verdict.code != EXIT_PASS

    def test_a_missing_outcome_does_not_pass(self) -> None:
        """AC5 — absence of an outcome is not an exemption."""
        verdict = _evaluate(self._fetcher(outcome_sha=None))
        assert verdict.code != EXIT_PASS

    def test_the_token_in_the_pr_body_does_not_pass(self) -> None:
        """AC6 — this is the rule-15 fence. These gates parse PR bodies by
        substring elsewhere, so the one thing this exemption must never be is
        a phrase an author can type. Spelled in a body with no matching
        check-run, it buys nothing."""
        verdict = _evaluate(
            self._fetcher(
                body=f"This PR is fine: {self.PIN_ONLY_REASON}",
                outcome_sha=None,
            )
        )
        assert verdict.code != EXIT_PASS

    def test_a_no_red_derivable_decline_still_fails(self) -> None:
        """The OMN-18647 refusal is untouched: widening the DECLINED verdict
        must not turn the general case into a pass."""
        verdict = _evaluate(self._fetcher(reason=self.NO_RED_REASON))
        assert verdict.code == EXIT_FAIL
        assert "NO_RED_DERIVABLE_CHECK" in verdict.reason

    def test_the_predicate_matches_the_token_not_the_prose(self) -> None:
        """Rewording the human half of the message may never change a verdict,
        and a reason that merely MENTIONS the token mid-sentence is not one."""
        assert is_no_companion_required(self.PIN_ONLY_REASON)
        assert not is_no_companion_required(self.NO_RED_REASON)
        assert not is_no_companion_required(
            "skip:LEASE_HELD — mentions skip:DEPENDENCY_PIN_ONLY in passing"
        )
