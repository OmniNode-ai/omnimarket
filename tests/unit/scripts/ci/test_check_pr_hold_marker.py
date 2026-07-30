# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the Merge Hold Gate (OMN-15483, controller half).

What this gate is for
---------------------
Round 1 of OMN-15483 taught the merge NODE to honor the hold marker. That bound
one consumer and left the one that performed every merge in the ticket's
incident table — the foreground Codex merge controller — completely unbound,
because it is a session driving ``gh pr merge`` and contains no omnimarket code.
This gate binds every consumer at once by making a held PR fail a required
status check, so it can never be required-green for anybody.

What each class proves
----------------------
``TestHeldPrIsRefused``
    The gate goes RED on a held title and on a held label, and names the matched
    token so the failure is actionable rather than a bare non-zero exit.

``TestUnheldPrPasses``
    The regression control, run against the REAL live title of the PR that adds
    this gate. If the gate held ordinary PRs it would wedge the fleet; that is a
    worse failure than the one being fixed.

``TestFailClosed``
    Criterion 2 on the CI path — an unreadable hold state and an unloadable
    vocabulary both REFUSE. A gate that cannot see the marker must never score
    "clear"; that blindness is the whole bug.

``TestSingleSourceVocabulary``
    Criterion 1 on the CI path, proven by MUTATION rather than by assertion.
    The gate is pointed at a mutated copy of the canonical module and its
    verdict follows the mutation — which is only possible if it is genuinely
    reading that module and not a vendored pattern of its own.

``TestSourcePrecedence``
    Live PR state beats the (possibly stale) event payload, so setting a hold
    takes effect on re-run and clearing one releases the PR (criterion 4).
    A live-fetch failure degrades to the payload rather than to a hard error.

``TestSingleSourceUrlAuthority``
    The same single-source rule applied to the GitHub base URL. The gate carries
    no URL of its own; it resolves ``github.rest_url`` out of the one endpoint
    authority every other GitHub caller in this repo reads, and the proof is by
    MUTATION (repoint the authority → the request follows) plus an equality
    assertion against the typed accessor, not by inspection.

``TestCiWiring``
    The job actually exists in ci.yml, is unconditional, and invokes this
    script. A gate that no workflow step executes is advisory (CLAUDE.md rule 5)
    — this is the anti-shelfware anchor.

Fixtures here deliberately contain literal hold tokens; they live in a test file
on purpose, because the marker must never appear in this PR's own title or
labels — that would hold the PR that adds the hold gate.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import urllib.error
from types import TracebackType
from typing import Any

import pytest
import yaml

from scripts.ci.check_pr_hold_marker import (
    CANONICAL_HOLD_MODULE,
    EXIT_CLEAR,
    EXIT_HELD,
    URL_AUTHORITY_FILE,
    CanonicalVocabularyUnavailableError,
    UrlAuthorityUnavailableError,
    evaluate_pr,
    load_canonical_hold_module,
    parse_labels_json,
    resolve_github_rest_url,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_GATE_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_pr_hold_marker.py"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_JOB_KEY = "pr-hold-check"
_JOB_NAME = "Merge Hold Gate (OMN-15483)"

# The REAL title of omnimarket#1972 — the PR that adds this gate. Using the live
# title rather than an invented "clean title" fixture is the point: the gate is
# proven not to hold the very PR shipping it.
_LIVE_PR_1972_TITLE = (
    "fix(OMN-15483): the merge path honors the hold marker that already ships"
)

_HELD_TITLE = "[WS4 PARITY PROBE - DO NOT MERGE] gateway probe"
_HELD_LABEL = "verification-hold"

# A scheme followed by at least one character that can begin a host. The
# trailing class deliberately excludes ``/`` so ``file:///tmp/x`` (no authority)
# and the bare scheme probe ``"https://"`` the gate uses for validation are not
# matches, while ``https://api.github.com`` is.
_HOST_BEARING_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/\"']")


def _host_bearing_string_literals(source: str) -> list[tuple[int, str]]:
    """Every string constant in ``source`` that embeds a ``scheme://host``.

    This reads **only** ``ast`` nodes. Comments are not in the AST, so every
    ``# url-authority-ok:`` suppression annotation is structurally invisible to
    it — the falsifier cannot be annotated away, by construction rather than by
    a rule someone has to remember. Docstrings are ``ast.Constant`` nodes like
    any other string, so a URL in a docstring *body* is caught too.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for match in _HOST_BEARING_URL_RE.finditer(node.value):
            found.append((node.lineno, node.value[match.start() : match.start() + 60]))
    return found


class _FakeResponse:
    """Minimal context-manager response for the injected HTTP opener."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _live_opener(*, title: str | None, labels: list[str] | None):
    """Build an opener returning a PR payload with the given title/labels."""

    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if labels is not None:
        payload["labels"] = [{"name": name} for name in labels]

    def _open(_request: Any) -> _FakeResponse:
        return _FakeResponse(payload)

    return _open


def _failing_opener(_request: Any) -> _FakeResponse:
    raise urllib.error.URLError("network is unreachable")


def _recording_opener(sink: list[str]):
    """An opener that records the full request URL and returns a clean PR."""

    def _open(request: Any) -> _FakeResponse:
        sink.append(request.full_url)
        return _FakeResponse({"title": _LIVE_PR_1972_TITLE, "labels": []})

    return _open


def _write_authority(path: pathlib.Path, rest_url: str) -> pathlib.Path:
    """Write a minimal endpoint-authority file with the given ``github.rest_url``."""
    path.write_text(
        "# a comment line the reader must skip\n"
        "linear:\n"
        '  graphql_url: "https://decoy.invalid/graphql"\n'
        "github:\n"
        f'  rest_url: "{rest_url}"\n'
        '  graphql_url: "https://decoy.invalid/graphql"\n',
        encoding="utf-8",
    )
    return path


def _pr_env(**overrides: str) -> dict[str, str]:
    """A pull_request event environment with NO live-fetch credentials.

    Omitting the token is what forces the payload path, so a test that means to
    exercise the payload cannot accidentally be answered by a live fetch.
    """
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "PR_TITLE": _LIVE_PR_1972_TITLE,
        "PR_LABELS_JSON": "[]",
    }
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# RED — a held PR is refused
# ---------------------------------------------------------------------------


class TestHeldPrIsRefused:
    """A held PR fails the gate, so it can never be required-green."""

    def test_held_title_fails_and_names_the_token(self) -> None:
        """RED vector. The exit code blocks; the report names WHY."""
        code, report = evaluate_pr(_pr_env(PR_TITLE=_HELD_TITLE))

        assert code == EXIT_HELD, report
        assert "HELD" in report
        # The matched token is surfaced verbatim — a bare non-zero exit would
        # leave an operator guessing which surface tripped it.
        assert "DO NOT MERGE" in report.upper()
        assert "title" in report

    def test_held_label_fails_even_with_a_clean_title(self) -> None:
        """The label surface is independently load-bearing.

        This is the surface the NODE path cannot yet see (``PrRecord`` carries
        no labels), so on CI it is the one that actually works today.
        """
        env = _pr_env(PR_LABELS_JSON=json.dumps([{"name": _HELD_LABEL}]))
        code, report = evaluate_pr(env)

        assert code == EXIT_HELD, report
        assert "label" in report
        assert _HELD_LABEL in report

    @pytest.mark.parametrize(
        "title",
        [
            "chore: DO-NOT-MERGE probe",
            "chore: DONOTMERGE probe",
            "chore: WORK IN PROGRESS probe",
            "[WIP] chore: probe",
            "chore: DNM probe",
            "chore: verification-hold probe",
        ],
    )
    def test_every_vocabulary_token_holds_the_pr(self, title: str) -> None:
        """The CI path honors the FULL vocabulary, not a subset of it.

        A gate that recognised only some spellings would be a third divergent
        vocabulary in behaviour even while sharing the regex object.
        """
        code, _ = evaluate_pr(_pr_env(PR_TITLE=title))
        assert code == EXIT_HELD, title


# ---------------------------------------------------------------------------
# GREEN — the regression control, against the live PR title
# ---------------------------------------------------------------------------


class TestUnheldPrPasses:
    """Ordinary PRs are untouched. Over-blocking would be worse than the bug."""

    def test_live_pr_1972_title_passes(self) -> None:
        """GREEN vector, using the REAL title of the PR that adds this gate.

        Self-referential on purpose: if the gate held its own PR, the mechanism
        could never land.
        """
        code, report = evaluate_pr(_pr_env())

        assert code == EXIT_CLEAR, report
        assert "PASS" in report

    @pytest.mark.parametrize(
        "title",
        [
            "feat(OMN-1234): add merge queue support",
            "fix: do not drop the envelope on retry",
            "docs: describe the workflow in progress tracking doc",
            "chore: swipe left on stale branches",
        ],
    )
    def test_ordinary_titles_pass(self, title: str) -> None:
        """No over-blocking on titles that merely contain adjacent words."""
        code, report = evaluate_pr(_pr_env(PR_TITLE=title))
        assert code == EXIT_CLEAR, report

    def test_non_pr_event_is_not_applicable(self) -> None:
        """ "Held" is a PR-scoped predicate; ``push`` has no PR to evaluate.

        Not a bypass: code reaches a protected branch through a PR, and that PR
        was gated here. Exiting 0 (rather than skipping) keeps the job PRESENT
        and COMPLETED for the CI Summary completeness anchor, which treats an
        absent strict gate as PENDING and a skipped one as failure.
        """
        code, report = evaluate_pr(
            {"GITHUB_EVENT_NAME": "push", "PR_TITLE": _HELD_TITLE}
        )
        assert code == EXIT_CLEAR, report
        assert "not applicable" in report


# ---------------------------------------------------------------------------
# Criterion 2 on the CI path — fail closed
# ---------------------------------------------------------------------------


class TestFailClosed:
    """An unreadable hold state refuses. It never decays to "clear"."""

    def test_no_observable_surface_is_refused(self) -> None:
        """Neither title nor labels observed → INDETERMINATE → RED.

        This is the criterion-2 falsifier on the CI path: a probe that cannot
        see the marker is exactly the blindness OMN-15483 exists to close, so
        scoring it "clear" would rebuild the bug inside the fix.
        """
        env = {"GITHUB_EVENT_NAME": "pull_request"}
        code, report = evaluate_pr(env)

        assert code == EXIT_HELD, report
        assert "UNREADABLE" in report

    def test_empty_title_is_unobserved_not_clear(self) -> None:
        """An empty title means the read failed — a real PR always has one."""
        env = {"GITHUB_EVENT_NAME": "pull_request", "PR_TITLE": ""}
        code, report = evaluate_pr(env)
        assert code == EXIT_HELD, report

    def test_missing_canonical_module_fails_closed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Deleting the vocabulary turns the gate RED, not vacuously green.

        The dangerous failure mode for any single-source gate: remove the thing
        it reads and it silently starts passing everything. Here it refuses.
        """
        code, report = evaluate_pr(
            _pr_env(PR_TITLE=_HELD_TITLE), module_path=tmp_path / "absent.py"
        )

        assert code == EXIT_HELD, report
        assert "fail-closed" in report

    def test_unloadable_canonical_module_raises_the_typed_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A corrupt vocabulary module is a typed failure, not a stray crash."""
        broken = tmp_path / "hold_marker.py"
        broken.write_text("this is not valid python(", encoding="utf-8")

        with pytest.raises(CanonicalVocabularyUnavailableError):
            load_canonical_hold_module(broken)

    def test_module_without_the_expected_surface_is_refused(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A module that imports but lacks the API must not be trusted."""
        hollow = tmp_path / "hold_marker.py"
        hollow.write_text("X = 1\n", encoding="utf-8")

        with pytest.raises(CanonicalVocabularyUnavailableError):
            load_canonical_hold_module(hollow)


# ---------------------------------------------------------------------------
# Criterion 1 on the CI path — one vocabulary, proven by mutation
# ---------------------------------------------------------------------------


class TestSingleSourceVocabulary:
    """The gate reads THE canonical module — proven by mutating it."""

    def test_gate_script_declares_no_regex_of_its_own(self) -> None:
        """Structural falsifier: the gate contains zero ``re.compile`` calls.

        A vendored pattern would pass every behavioural test in this file while
        silently reintroducing the divergent-vocabulary bug on a third surface.
        """
        tree = ast.parse(_GATE_SCRIPT.read_text(encoding="utf-8"))
        compiles = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
        ]
        assert compiles == [], (
            "the CI hold gate must not declare its own pattern — it loads the "
            "canonical vocabulary from merge_control/hold_marker.py"
        )

    def test_gate_loads_the_real_in_repo_module(self) -> None:
        """The default path resolves to the shipped canonical module."""
        assert CANONICAL_HOLD_MODULE.is_file()
        assert CANONICAL_HOLD_MODULE == (
            _REPO_ROOT / "src" / "omnimarket" / "merge_control" / "hold_marker.py"
        )
        module = load_canonical_hold_module()
        # Same object identity as what the node path imports, by pattern.
        from omnimarket.merge_control.hold_marker import HOLD_MARKER_RE

        assert module.HOLD_MARKER_RE.pattern == HOLD_MARKER_RE.pattern
        assert module.HOLD_MARKER_RE.flags == HOLD_MARKER_RE.flags

    def test_narrowing_the_canonical_vocabulary_releases_the_gate(
        self, tmp_path: pathlib.Path
    ) -> None:
        """MUTATION PROOF, direction 1: drop a token → the gate stops holding.

        This is the executable equivalent of a vendored-copy sync test, and it
        is stronger: a sync test proves two copies are equal *at test time*,
        whereas this proves there is only one copy, because changing it changes
        the gate's verdict.
        """
        source = CANONICAL_HOLD_MODULE.read_text(encoding="utf-8")
        mutated = source.replace(
            r'r"do[\s_-]?not[\s_-]?merge"', 'r"a-token-that-never-appears"'
        )
        assert mutated != source, "mutation vector did not apply — test is vacuous"

        mutant = tmp_path / "hold_marker.py"
        mutant.write_text(mutated, encoding="utf-8")

        # Same held title, mutated vocabulary → now CLEAR.
        code, report = evaluate_pr(_pr_env(PR_TITLE=_HELD_TITLE), module_path=mutant)
        assert code == EXIT_CLEAR, report

        # And RED again against the real module, same input.
        code_real, _ = evaluate_pr(_pr_env(PR_TITLE=_HELD_TITLE))
        assert code_real == EXIT_HELD

    def test_widening_the_canonical_vocabulary_holds_the_gate(
        self, tmp_path: pathlib.Path
    ) -> None:
        """MUTATION PROOF, direction 2: add a token → the gate starts holding.

        Both directions matter. Direction 1 alone would also pass if the gate
        were reading nothing at all and defaulting to clear.
        """
        novel_title = "chore: zzz-novel-hold-token probe"
        # Baseline: the real vocabulary does not hold this title.
        baseline, _ = evaluate_pr(_pr_env(PR_TITLE=novel_title))
        assert baseline == EXIT_CLEAR

        source = CANONICAL_HOLD_MODULE.read_text(encoding="utf-8")
        mutated = source.replace(
            r'r"do[\s_-]?not[\s_-]?merge"',
            r'r"do[\s_-]?not[\s_-]?merge|zzz-novel-hold-token"',
        )
        assert mutated != source, "mutation vector did not apply — test is vacuous"

        mutant = tmp_path / "hold_marker.py"
        mutant.write_text(mutated, encoding="utf-8")

        code, report = evaluate_pr(_pr_env(PR_TITLE=novel_title), module_path=mutant)
        assert code == EXIT_HELD, report


# ---------------------------------------------------------------------------
# Source precedence — live beats stale payload; failure degrades safely
# ---------------------------------------------------------------------------


class TestSourcePrecedence:
    """Live PR state wins when readable; the payload is the fallback."""

    def test_live_hold_beats_a_clean_stale_payload(self) -> None:
        """Setting a hold after the last push takes effect on re-run.

        ``ci.yml``'s ``pull_request`` trigger does not fire on ``labeled`` or
        ``edited``, so the payload a re-run replays can be older than the hold.
        Reading live state is what makes the hold usable at all.
        """
        env = _pr_env(GH_TOKEN="t", GH_REPO="o/r", PR_NUMBER="1972")
        code, report = evaluate_pr(
            env, opener=_live_opener(title=_HELD_TITLE, labels=[])
        )

        assert code == EXIT_HELD, report
        assert "live" in report

    def test_clearing_the_hold_releases_the_pr(self) -> None:
        """Criterion 4 on the CI path: the gate discriminates, not blocks.

        Stale payload still carries the marker; live state is clean → PASS.
        Without live precedence, a cleared hold would need a fresh push to
        release, and the gate would be a one-way door.
        """
        env = _pr_env(
            PR_TITLE=_HELD_TITLE,
            PR_LABELS_JSON=json.dumps([{"name": _HELD_LABEL}]),
            GH_TOKEN="t",
            GH_REPO="o/r",
            PR_NUMBER="1972",
        )
        code, report = evaluate_pr(
            env, opener=_live_opener(title=_LIVE_PR_1972_TITLE, labels=[])
        )

        assert code == EXIT_CLEAR, report

    def test_live_fetch_failure_degrades_to_the_payload(self) -> None:
        """No new API dependency: a dead API must not wedge every PR.

        The gate still renders a real verdict from the ``github`` context.
        """
        env = _pr_env(
            PR_TITLE=_HELD_TITLE, GH_TOKEN="t", GH_REPO="o/r", PR_NUMBER="1972"
        )
        code, report = evaluate_pr(env, opener=_failing_opener)

        assert code == EXIT_HELD, report
        assert "payload" in report
        assert "live PR fetch failed" in report

    def test_live_failure_with_no_payload_is_still_refused(self) -> None:
        """Both surfaces unreadable → still fail closed, never clear."""
        env = {
            "GITHUB_EVENT_NAME": "pull_request",
            "GH_TOKEN": "t",
            "GH_REPO": "o/r",
            "PR_NUMBER": "1972",
        }
        code, report = evaluate_pr(env, opener=_failing_opener)
        assert code == EXIT_HELD, report
        assert "UNREADABLE" in report


class TestLabelParsing:
    """``()`` (no labels) and ``None`` (unobserved) must stay distinguishable."""

    def test_empty_array_is_observed_with_no_labels(self) -> None:
        assert parse_labels_json("[]") == ()

    def test_absent_is_unobserved(self) -> None:
        assert parse_labels_json(None) is None
        assert parse_labels_json("   ") is None

    def test_malformed_json_is_unobserved_not_empty(self) -> None:
        """Garbage must not be read as "this PR has no labels"."""
        assert parse_labels_json("{not json") is None
        assert parse_labels_json('{"labels": []}') is None

    def test_label_objects_and_bare_strings_both_parse(self) -> None:
        assert parse_labels_json('[{"name": "a"}, "b"]') == ("a", "b")


# ---------------------------------------------------------------------------
# One URL authority — the same single-source rule, applied to the base URL
# ---------------------------------------------------------------------------


class TestSingleSourceUrlAuthority:
    """The gate resolves the GitHub base URL; it does not carry one.

    ``configs/service_endpoints.yaml`` is the repo's single declared authority
    for external base URLs (OMN-12806) and the ``URL Authority Gate`` enforces
    it. A CI script that hardcodes ``https://api.github.com`` — annotated or not
    — is a second copy of a value that already has exactly one home, which is
    the same divergence this ticket removed from the hold vocabulary.
    """

    def test_gate_script_declares_no_url_of_its_own(self) -> None:
        """Structural falsifier, sibling of the no-``re.compile`` one.

        An annotated literal would pass every behavioural test in this file
        while silently pinning a host the authority no longer declares.

        This runs the **real** `URL Authority Gate` scanner rather than a local
        approximation of it, for the same reason the gate loads the real hold
        vocabulary: a re-implementation drifts. A first cut of this test
        exempted whole docstrings, went green, and the CI gate still failed on a
        URL inside a docstring *body* — the local copy was more lenient than the
        thing it claimed to predict. Importing `scan_source` removes that gap by
        construction.
        """
        from omnibase_core.validation.validator_url_authority import scan_source

        path = "scripts/ci/check_pr_hold_marker.py"
        violations = scan_source(
            "omnimarket", path, _GATE_SCRIPT.read_text(encoding="utf-8")
        )
        assert violations == [], (
            "the CI hold gate must not declare its own endpoint — it resolves "
            "github.rest_url from configs/service_endpoints.yaml "
            f"(URL Authority Gate violations: "
            f"{[(v.rule, v.line, v.snippet) for v in violations]})"
        )

    def test_gate_script_carries_no_host_literal_even_annotated(self) -> None:
        """Strict falsifier: zero host-bearing literals, annotation or not.

        This runs **alongside** ``test_gate_script_declares_no_url_of_its_own``
        rather than instead of it, because the two assert different properties
        and each is weak exactly where the other is strong:

        * the delegating one proves this file passes the gate the repo actually
          enforces, and cannot drift more lenient than that gate;
        * this one proves the file contains no host literal *at all*, which the
          delegating one cannot, because ``scan_source`` honours
          ``# url-authority-ok:`` by design.

        Keeping only the delegating one made the shipped falsifier weaker than
        the merged claim about it — an annotated ``https://api.github.com``
        passed the whole suite while re-pinning a host the authority may no
        longer declare. That is the second copy this gate exists to forbid; an
        annotation is a reason, not a resolution.
        """
        hits = _host_bearing_string_literals(_GATE_SCRIPT.read_text(encoding="utf-8"))
        assert hits == [], (
            "the CI hold gate must carry no host-bearing URL literal — it "
            "resolves github.rest_url from configs/service_endpoints.yaml. A "
            "'# url-authority-ok:' annotation does not exempt it here: "
            f"{hits}"
        )

    def test_the_strict_falsifier_fires_on_an_annotated_literal(self) -> None:
        """MUTATION PROOF: the annotation does not save the offender.

        The vector is the exact regression this test exists to catch — the
        literal the round-3 fix removed, put back with the suppression comment
        the gate's scanner honours. A falsifier that never goes red on its own
        subject matter is decorative, so the red is asserted, not assumed.
        """
        offender = (
            '_GITHUB_API_BASE = "https://api.github.com"'
            "  # url-authority-ok: reintroduced by a future edit\n"
        )
        hits = _host_bearing_string_literals(offender)

        assert [line for line, _ in hits] == [1]
        assert hits[0][1].startswith("https://api.github.com")

    def test_the_strict_falsifier_sees_into_docstring_bodies(self) -> None:
        """The vector that defeated the round-3 local copy.

        That first cut exempted whole docstrings, went green, and the real CI
        gate still failed on a URL inside a docstring *body*. Docstrings are
        ordinary ``ast.Constant`` nodes here, so there is no exemption to get
        wrong.
        """
        module = '"""Module doc.\n\nTalks to https://api.github.com directly.\n"""\n'
        hits = _host_bearing_string_literals(module)

        assert len(hits) == 1
        assert hits[0][1].startswith("https://api.github.com")

    @pytest.mark.parametrize(
        "benign",
        [
            'PREFIX = "https://"',  # the gate's own scheme validation probe
            'PATH = "repos/{owner}/{repo}/pulls/{number}"',
            'KEY = "github.rest_url"',
            'FILE = "configs/service_endpoints.yaml"',
            'LOCAL = "file:///tmp/authority.yaml"',  # scheme, but no authority
        ],
    )
    def test_the_strict_falsifier_ignores_scheme_only_and_pathlike_strings(
        self, benign: str
    ) -> None:
        """Negative vector: it must not be a false-positive generator.

        A structural check that fires on ``"https://"`` — which the gate uses at
        line 288 to validate the resolved value — would be deleted by the next
        author rather than obeyed, and the property would be lost for real.
        """
        assert _host_bearing_string_literals(benign) == []

    def test_both_url_falsifiers_are_present(self) -> None:
        """The pair is the mechanism; either alone is a known-weak check.

        Asserted rather than left to review convention: deleting the delegating
        test re-opens the drift gap round 3b closed, and deleting the strict one
        re-opens the annotation gap this ticket closed. Both deletions are
        silent under every other test in this file.
        """
        for name in (
            "test_gate_script_declares_no_url_of_its_own",
            "test_gate_script_carries_no_host_literal_even_annotated",
        ):
            assert callable(getattr(TestSingleSourceUrlAuthority, name, None)), name

    def test_default_authority_path_is_the_shipped_authority_file(self) -> None:
        assert URL_AUTHORITY_FILE.is_file()
        assert URL_AUTHORITY_FILE == (
            _REPO_ROOT / "src" / "omnimarket" / "configs" / "service_endpoints.yaml"
        )

    def test_gate_resolves_the_same_url_as_the_typed_accessor(self) -> None:
        """The stdlib reader and the PyYAML accessor must agree, exactly.

        The gate cannot import the accessor (PyYAML is absent from the hosted
        runner's system Python and this job runs a bare ``python3`` on purpose),
        so the parse is separate while the *value* stays single-homed. This
        assertion is what makes that claim falsifiable: change the authority's
        shape and this goes RED rather than the gate silently drifting.
        """
        from omnimarket.config.service_endpoints import GITHUB_REST_URL

        assert resolve_github_rest_url() == GITHUB_REST_URL

    def test_live_fetch_targets_the_resolved_authority_url(self) -> None:
        """The resolved value is what the request actually goes to."""
        seen: list[str] = []
        env = _pr_env(GH_TOKEN="t", GH_REPO="o/r", PR_NUMBER="1973")

        code, report = evaluate_pr(env, opener=_recording_opener(seen))

        assert code == EXIT_CLEAR, report
        assert seen == [f"{resolve_github_rest_url()}/repos/o/r/pulls/1973"]

    def test_repointing_the_authority_repoints_the_gate(
        self, tmp_path: pathlib.Path
    ) -> None:
        """MUTATION PROOF: the URL genuinely comes from the authority.

        Direction matters the same way it does for the vocabulary — an
        assertion that the gate "uses the authority" would also pass if it were
        hardcoding the identical string.
        """
        authority = _write_authority(
            tmp_path / "service_endpoints.yaml", "https://ghe.example.com/api/v3"
        )
        seen: list[str] = []
        env = _pr_env(GH_TOKEN="t", GH_REPO="o/r", PR_NUMBER="1973")

        code, report = evaluate_pr(
            env, opener=_recording_opener(seen), authority_path=authority
        )

        assert code == EXIT_CLEAR, report
        assert seen == ["https://ghe.example.com/api/v3/repos/o/r/pulls/1973"]

    def test_quoted_authority_value_allows_inline_comment(
        self, tmp_path: pathlib.Path
    ) -> None:
        authority = tmp_path / "service_endpoints.yaml"
        authority.write_text(
            'github:\n  rest_url: "https://api.github.com" # prod\n',
            encoding="utf-8",
        )

        assert resolve_github_rest_url(authority) == "https://api.github.com"

    @pytest.mark.parametrize(
        ("body", "expected_fragment"),
        [
            pytest.param("linear:\n  graphql_url: x\n", "declares no", id="no-section"),
            pytest.param("github:\n  graphql_url: x\n", "declares no", id="no-key"),
            pytest.param('github:\n  rest_url: ""\n', "not an absolute", id="empty"),
            pytest.param(
                "github:\n  rest_url: api.github.example\n",
                "not an absolute",
                id="not-absolute",
            ),
        ],
    )
    def test_a_broken_authority_raises_rather_than_defaulting(
        self, tmp_path: pathlib.Path, body: str, expected_fragment: str
    ) -> None:
        """No silent default. The whole point is that there is no fallback host."""
        authority = tmp_path / "service_endpoints.yaml"
        authority.write_text(body, encoding="utf-8")

        with pytest.raises(UrlAuthorityUnavailableError) as excinfo:
            resolve_github_rest_url(authority)
        assert expected_fragment in str(excinfo.value)

    def test_missing_authority_raises(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(UrlAuthorityUnavailableError):
            resolve_github_rest_url(tmp_path / "absent.yaml")

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param(
                '  rest_url: "https://api.github.com" # prod',
                "https://api.github.com",
                id="quoted-then-comment",
            ),
            pytest.param(
                "  rest_url: https://api.github.com # prod",
                "https://api.github.com",
                id="bare-then-comment",
            ),
            pytest.param(
                "  rest_url: 'https://api.github.com'   ",
                "https://api.github.com",
                id="single-quoted-trailing-space",
            ),
            pytest.param(
                '  rest_url: "https://api.github.com/x#frag"',
                "https://api.github.com/x#frag",
                id="hash-inside-quotes-is-not-a-comment",
            ),
            pytest.param(
                "  rest_url: https://api.github.com/x#frag",
                "https://api.github.com/x#frag",
                id="hash-without-preceding-space-is-not-a-comment",
            ),
        ],
    )
    def test_trailing_comments_are_stripped_not_absorbed(
        self, tmp_path: pathlib.Path, line: str, expected: str
    ) -> None:
        """A trailing comment must not end up inside the host.

        `value.strip().strip("\\"'")` alone leaves `https://api.github.com" # prod`,
        which still passes an `https://` prefix check — a corrupted value returned
        instead of the promised error. Caught in review on this PR; fixed with a
        quote-aware reader and pinned here in both directions.
        """
        authority = tmp_path / "service_endpoints.yaml"
        authority.write_text(f"github:\n{line}\n", encoding="utf-8")

        assert resolve_github_rest_url(authority) == expected

    @pytest.mark.parametrize(
        ("line", "fragment"),
        [
            pytest.param(
                '  rest_url: "https://api.github.com',
                "unterminated",
                id="unterminated-quote",
            ),
            pytest.param(
                '  rest_url: "https://api.github.com" garbage',
                "trailing content",
                id="trailing-junk-is-not-ignored",
            ),
        ],
    )
    def test_malformed_scalars_raise_rather_than_degrade(
        self, tmp_path: pathlib.Path, line: str, fragment: str
    ) -> None:
        """Refuse, per the docstring's promise — never return a partial value."""
        authority = tmp_path / "service_endpoints.yaml"
        authority.write_text(f"github:\n{line}\n", encoding="utf-8")

        with pytest.raises(UrlAuthorityUnavailableError) as excinfo:
            resolve_github_rest_url(authority)
        assert fragment in str(excinfo.value)

    def test_unresolvable_authority_fails_the_gate_closed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Fail-closed, exactly like an unloadable vocabulary.

        A gate that cannot resolve its own read surface must refuse, not fall
        back to a literal and not score the PR clear.
        """
        env = _pr_env(GH_TOKEN="t", GH_REPO="o/r", PR_NUMBER="1973")

        code, report = evaluate_pr(
            env,
            opener=_recording_opener([]),
            authority_path=tmp_path / "absent.yaml",
        )

        assert code == EXIT_HELD, report
        assert "fail-closed" in report
        assert "endpoint authority" in report

    def test_no_authority_read_when_no_live_fetch_is_warranted(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The payload-only path must not need the authority at all.

        Without credentials there is no request to address, so a broken
        authority must not turn an otherwise-answerable PR red.
        """
        code, report = evaluate_pr(
            _pr_env(PR_TITLE=_HELD_TITLE), authority_path=tmp_path / "absent.yaml"
        )

        assert code == EXIT_HELD, report
        assert "HELD against landing" in report


# ---------------------------------------------------------------------------
# Anti-shelfware — the gate is actually wired
# ---------------------------------------------------------------------------


class TestCiWiring:
    """A detector no CI step executes is advisory (CLAUDE.md rule 5)."""

    @staticmethod
    def _job() -> dict[str, Any]:
        workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        assert _JOB_KEY in jobs, f"{_JOB_KEY} is not a job in ci.yml"
        return dict(jobs[_JOB_KEY])

    def test_job_exists_with_the_registered_display_name(self) -> None:
        """The display name IS the CI Summary key — a rename silently unwires."""
        assert self._job()["name"] == _JOB_NAME

    def test_job_is_unconditional(self) -> None:
        """No ``needs``/``if``: an upstream failure must not skip the hold gate.

        On ``a56e3819`` a failing ``occ-preflight`` cascade-skipped Tests,
        typecheck, Contract Compliance and the whole E2E lane. A hold gate that
        an unrelated upstream can skip is not a gate.
        """
        job = self._job()
        assert "needs" not in job, "the hold gate must not be cascade-skippable"
        assert "if" not in job, "the hold gate must not be conditional"

    def test_job_invokes_this_script(self) -> None:
        """The wiring points at the script these tests exercise."""
        runs = [
            str(step.get("run", "")) for step in self._job()["steps"] if "run" in step
        ]
        assert any("scripts/ci/check_pr_hold_marker.py" in run for run in runs), runs

    def test_job_reads_both_surfaces_from_the_github_context(self) -> None:
        """Title AND labels are supplied, so neither is silently unobserved."""
        env = self._job()["env"]
        assert "github.event.pull_request.title" in str(env["PR_TITLE"])
        assert "pull_request.labels" in str(env["PR_LABELS_JSON"])
        assert "github.event_name" in str(env["GITHUB_EVENT_NAME"])

    def test_the_gate_name_is_not_itself_a_hold_token(self) -> None:
        """The gate must not hold a PR for merely naming the gate.

        Caught live while drafting this PR: the first display name was
        "Verification Hold Gate", and ``verification hold`` is a token in the
        vocabulary the gate enforces. Nothing failed — the name only reaches
        the title surface when a human writes it there — but the fan-out is
        exactly that case. A PR titled "fan out <gate name> to onex_change_control"
        would have been refused by the gate it was installing, which reads as a
        bug in the gate rather than as the gate working.

        The name was changed to one the vocabulary does not match. This test is
        the mechanism for that decision rather than a note in a PR body, so a
        future rename back into the vocabulary fails here instead of surfacing
        as a confusing self-hold months later.
        """
        from omnimarket.merge_control.hold_marker import HOLD_MARKER_RE

        match = HOLD_MARKER_RE.search(_JOB_NAME)
        offending = match.group(0) if match else ""
        assert match is None, (
            f"the gate's display name {_JOB_NAME!r} contains hold token "
            f"{offending!r} — any PR title mentioning the gate would be held "
            "by the gate itself"
        )
        # And the same for a realistic fan-out PR title carrying that name.
        fanout_title = f"feat(OMN-0000): fan out {_JOB_NAME} to onex_change_control"
        assert HOLD_MARKER_RE.search(fanout_title) is None
