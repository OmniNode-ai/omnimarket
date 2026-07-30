# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fan-out surface: one vocabulary, proven to fire, in every repo (OMN-15484).

OMN-15483 shipped the hold gate in omnimarket only, while every incident in its
table happened in ``onex_change_control`` or ``omnibase_infra``. This module
covers the surface that closes that gap without vendoring: the reusable workflow
``.github/workflows/merge-hold-gate-reusable.yml`` plus the two scripts it runs
in the *calling* repo's CI.

What is deliberately NOT asserted here: that the adopting repos are wired. That
lives in each adopting repo, driven against ITS OWN ``CI Summary`` producer,
because a strict slot in omnimarket's poller proves nothing about OCC's
needs-based aggregator. Cross-repo, the seam is the reusable workflow's input
contract, and each caller's wiring test asserts its half of it.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import textwrap
from typing import Any

import pytest
import yaml

from scripts.ci.check_hold_gate_selftest import (
    CLEAR_VECTOR,
    HELD_VECTOR,
    check_context_name,
)
from scripts.ci.check_hold_gate_selftest import run as selftest_run
from scripts.ci.check_pr_hold_marker import (
    CANONICAL_HOLD_MODULE,
    load_canonical_hold_module,
)
from scripts.ci.check_single_hold_vocabulary import (
    EXIT_OFFENDER,
    EXIT_OK,
    canonical_name_fragments,
    canonical_skeletons,
    literal_skeleton,
    redeclared_tokens,
    split_alternation,
)
from scripts.ci.check_single_hold_vocabulary import run as scan_run

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_REUSABLE_WORKFLOW = (
    _REPO_ROOT / ".github" / "workflows" / "merge-hold-gate-reusable.yml"
)
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_CALLER_JOB_KEY = "merge-hold-gate"
_SCANNER = _REPO_ROOT / "scripts" / "ci" / "check_single_hold_vocabulary.py"


def _write_module(root: pathlib.Path, relative: str, body: str) -> pathlib.Path:
    """Write a python module into a synthetic repo tree."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC1 — one vocabulary across repo boundaries
# ---------------------------------------------------------------------------


class TestSingleVocabularyScanner:
    """The AC1 falsifier, which runs against the CALLING repo's checkout."""

    def test_a_clean_tree_passes(self, tmp_path: pathlib.Path) -> None:
        _write_module(
            tmp_path,
            "src/pkg/ordinary.py",
            """
            import re
            SEMVER_RE = re.compile(r"^v?\\d+\\.\\d+\\.\\d+$")
            """,
        )
        code, report = scan_run(tree_root=tmp_path, scan_roots=["src", "scripts"])
        assert code == EXIT_OK, report

    def test_a_respelled_vendored_copy_is_caught(self, tmp_path: pathlib.Path) -> None:
        """The failure mode a byte-compare or a sync test misses.

        A vendored copy never arrives as an exact duplicate — it arrives
        respelled, which is precisely how OMN-15483 round 1 ended up with two
        divergent definitions inside ONE repo. Detection is by literal skeleton,
        so separator and metacharacter noise does not launder it.
        """
        _write_module(
            tmp_path,
            "src/pkg/vendored.py",
            """
            import re
            _LOCAL = re.compile(r"do[ _-]*NOT[ _-]*merge|work[ ]*in[ ]*progress")
            """,
        )
        code, report = scan_run(tree_root=tmp_path, scan_roots=["src"])
        assert code == EXIT_OFFENDER
        assert "donotmerge" in report
        assert "vendored.py" in report

    def test_an_empty_but_canonically_named_copy_is_caught(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Name rule: a placeholder today is a divergent vocabulary tomorrow."""
        _write_module(
            tmp_path,
            "src/pkg/named.py",
            """
            import re
            _DO_NOT_MERGE_RE = re.compile(r"")
            """,
        )
        code, report = scan_run(tree_root=tmp_path, scan_roots=["src"])
        assert code == EXIT_OFFENDER
        assert "DO_NOT_MERGE" in report

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^\[draft\]\s*",  # a draft-PR title parser
            r"\bWIP\b",  # a status-label matcher
            r"\bDNM\b",  # an abbreviation elsewhere in a codebase
        ],
    )
    def test_a_single_short_token_is_not_a_false_positive(
        self, tmp_path: pathlib.Path, pattern: str
    ) -> None:
        """False positives are worse than a missing gate here.

        ``draft``/``WIP``/``DNM`` are ordinary words. Flagging an innocent regex
        would wedge an adopting repo on a check it cannot satisfy, which teaches
        people to disable the check — the failure mode that ends enforcement
        everywhere. A real re-declaration is recognised by the distinctive
        multi-word tokens or by carrying several at once.
        """
        _write_module(
            tmp_path,
            "src/pkg/innocent.py",
            f"""
            import re
            SOMETHING_RE = re.compile(r"{pattern}")
            """,
        )
        code, report = scan_run(tree_root=tmp_path, scan_roots=["src"])
        assert code == EXIT_OK, report

    def test_two_short_tokens_together_are_a_redeclaration(
        self, tmp_path: pathlib.Path
    ) -> None:
        """One short token is a coincidence; two is a vocabulary."""
        _write_module(
            tmp_path,
            "src/pkg/sneaky.py",
            """
            import re
            _P = re.compile(r"\\bWIP\\b|\\bDNM\\b")
            """,
        )
        code, _ = scan_run(tree_root=tmp_path, scan_roots=["src"])
        assert code == EXIT_OFFENDER

    def test_the_canonical_module_itself_is_exempt_by_relative_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        """omnimarket calling the gate on itself must not flag its own source.

        The caller checkout and the vocabulary checkout are two directories
        holding the same file, so an absolute-path exemption alone would have
        the gate refuse the very repo that defines the vocabulary. Caught here
        rather than in a red CI run on the fan-out PR.
        """
        canonical_relative = "src/omnimarket/merge_control/hold_marker.py"
        _write_module(
            tmp_path,
            canonical_relative,
            CANONICAL_HOLD_MODULE.read_text(encoding="utf-8"),
        )
        code, report = scan_run(tree_root=tmp_path, scan_roots=["src"])
        assert code == EXIT_OK, report

    def test_an_unparseable_file_fails_but_is_not_called_a_violation(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Fail-closed, and diagnosable.

        A file the scanner cannot parse is not a file it cleared, so the check
        must fail. But the overwhelmingly likely cause is an interpreter older
        than the source, and reporting that as "a second hold vocabulary is
        declared" sends the reader hunting for a copy that does not exist.
        """
        _write_module(tmp_path, "src/pkg/broken.py", "def f( :\n")
        code, report = scan_run(tree_root=tmp_path, scan_roots=["src"])
        assert code == EXIT_OFFENDER
        assert "could not CLEAR" in report
        assert "NOT a vocabulary violation" in report
        assert "interpreter" in report

    def test_scanner_declares_no_vocabulary_of_its_own(self) -> None:
        """The detector must not become the second copy it exists to forbid.

        Every token it matches on is derived from the canonical module at run
        time. If this file ever grows its own token list, the fleet has two
        vocabularies again — one enforcing, one detecting, free to diverge.

        Scoped to executable string literals on purpose. Prose — comments and
        docstrings — must be free to name the tokens, because explaining what
        the detector looks for is how the next reader understands it; the first
        cut of this test scanned raw file text and failed on its own
        explanatory comment. A hardcoded token list would be *data*, and data
        is what this asserts against.
        """
        canonical = load_canonical_hold_module()
        tree = ast.parse(_SCANNER.read_text(encoding="utf-8"), filename=str(_SCANNER))

        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]

        for skeleton in canonical_skeletons(canonical.HOLD_MARKER_RE.pattern):
            if len(skeleton) < 8:
                continue
            for literal in literals:
                assert skeleton not in literal_skeleton(literal), (
                    f"the AC1 scanner hardcodes the canonical token {skeleton!r} "
                    f"in the string literal {literal!r} — it must derive tokens "
                    "from hold_marker.py, not carry them"
                )

    def test_tokens_are_derived_from_the_canonical_pattern(self) -> None:
        """Change the vocabulary and the scanner's targets change with it."""
        derived = canonical_skeletons(r"alpha[\s_-]?beta|\bGAMMA\b")
        assert set(derived) == {"alphabeta", "gamma"}

    def test_top_level_alternation_split_respects_groups_and_classes(self) -> None:
        """``|`` inside a class or group belongs to that construct."""
        assert split_alternation(r"a[b|c]d|(e|f)g|h") == [r"a[b|c]d", r"(e|f)g", "h"]

    def test_name_fragments_come_from_the_module_exports(self) -> None:
        assert set(canonical_name_fragments(["HOLD_MARKER_RE", "DO_NOT_MERGE_RE"])) == {
            "HOLD_MARKER",
            "DO_NOT_MERGE",
        }

    def test_redeclaration_predicate_is_explicit(self) -> None:
        skeletons = ("verificationhold", "workinprogress", "donotmerge", "wip")
        assert redeclared_tokens("donotmerge", skeletons) == ("donotmerge",)
        assert redeclared_tokens("wip", skeletons) == ()
        assert redeclared_tokens("", skeletons) == ()

    def test_cli_exits_nonzero_on_an_offender(self, tmp_path: pathlib.Path) -> None:
        """The CLI the workflow actually invokes, not just the function."""
        _write_module(
            tmp_path,
            "src/pkg/vendored.py",
            """
            import re
            _P = re.compile(r"do not merge|work in progress")
            """,
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci.check_single_hold_vocabulary",
                "--tree",
                str(tmp_path),
                "--roots",
                "src",
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == EXIT_OFFENDER, completed.stdout
        assert "::error::" in completed.stderr


# ---------------------------------------------------------------------------
# AC3 — the gate demonstrably fires, in the adopting repo's own CI
# ---------------------------------------------------------------------------


class TestSelfProof:
    """A green check proves the job ran, not that the gate can say no."""

    def test_the_selftest_passes_against_the_shipped_gate(self) -> None:
        code, report = selftest_run()
        assert code == 0, report
        assert "fires and demonstrably clears" in report

    def test_the_probe_vectors_are_validated_before_use(self) -> None:
        """A vector that stopped being a hold token must fail, not pass quietly.

        This is the difference between a self-test and a decoration. If the
        vocabulary narrows and nobody updates the vector, the gate correctly
        exits 0 on it and a naive self-test records success while asserting
        nothing at all.
        """
        code, report = selftest_run(held_vector="an ordinary title with no marker")
        assert code == 1
        assert "went vacuous" in report

    def test_an_over_matching_vocabulary_is_caught_by_the_clear_vector(self) -> None:
        """Stuck-closed is a repo outage, not a safe default."""
        code, report = selftest_run(clear_vector=HELD_VECTOR)
        assert code == 1
        assert "negative direction proves nothing" in report

    def test_a_missing_gate_cli_fails_closed(self, tmp_path: pathlib.Path) -> None:
        code, report = selftest_run(gate_script=tmp_path / "absent.py")
        assert code == 1
        assert "fail-closed" in report

    def test_a_missing_vocabulary_fails_closed(self, tmp_path: pathlib.Path) -> None:
        code, report = selftest_run(module_path=tmp_path / "absent.py")
        assert code == 1
        assert "fail-closed" in report

    def test_the_clear_vector_is_a_realistic_fanout_title(self) -> None:
        """AC5, from the other end: the PR that installs the gate is not held."""
        canonical = load_canonical_hold_module()
        assert canonical.match_hold_token(CLEAR_VECTOR) is None


# ---------------------------------------------------------------------------
# AC5 — the gate does not hold its own fan-out
# ---------------------------------------------------------------------------


class TestContextNameGuard:
    def test_a_self_holding_context_name_is_refused(self) -> None:
        """The exact mistake made once already, now mechanised across repos.

        The first draft of the omnimarket gate was named "Verification Hold
        Gate", and ``verification hold`` is a token in the vocabulary it
        enforces. In the fan-out the caller supplies half the context name from
        a different repository, so the check has to travel with the workflow.
        """
        canonical = load_canonical_hold_module()
        failure = check_context_name(canonical, "Verification Hold Gate / evaluate")
        assert failure is not None
        assert "Verification Hold" in failure

    def test_the_shipped_context_name_is_clean(self) -> None:
        canonical = load_canonical_hold_module()
        assert check_context_name(canonical, "merge-hold-gate / evaluate") is None

    def test_no_context_name_supplied_is_not_an_error(self) -> None:
        canonical = load_canonical_hold_module()
        assert check_context_name(canonical, None) is None


# ---------------------------------------------------------------------------
# AC4 / AC6 — the workflow contract the adopting repos bind to
# ---------------------------------------------------------------------------


class TestReusableWorkflowContract:
    """Anything an adopting repo depends on is pinned here.

    These are the fields OCC and omnibase_infra write into their ``ci.yml``.
    Changing one without changing the callers is a silent cross-repo break —
    the seam-mismatch class that is PAIR_INCOMPATIBLE even when both repos'
    CI is green.
    """

    @staticmethod
    def _workflow() -> dict[str, Any]:
        return dict(yaml.safe_load(_REUSABLE_WORKFLOW.read_text(encoding="utf-8")))

    @staticmethod
    def _job() -> dict[str, Any]:
        return dict(TestReusableWorkflowContract._workflow()["jobs"]["evaluate"])

    def test_it_is_callable(self) -> None:
        # `on:` is parsed by PyYAML 1.1 rules as the boolean True.
        triggers = self._workflow()[True]
        assert "workflow_call" in triggers

    def test_the_inner_job_id_is_the_registered_context_suffix(self) -> None:
        """Callers register ``<their job> / evaluate``; renaming breaks them all.

        The check-run context of a reusable call is
        ``<caller job> / <inner job>``, so this id is a cross-repo published
        name, not an internal detail.
        """
        jobs = self._workflow()["jobs"]
        assert list(jobs) == ["evaluate"], (
            "the reusable workflow must expose exactly one job named `evaluate` "
            "— every caller's CI Summary registration is keyed on it"
        )
        assert "name" not in jobs["evaluate"], (
            "a `name:` on the inner job would override `evaluate` in the "
            "composed context and unwire every caller's strict registration"
        )

    def test_the_input_contract_is_stable(self) -> None:
        inputs = self._workflow()[True]["workflow_call"]["inputs"]
        assert set(inputs) == {"vocabulary_ref", "scan_roots", "context_name"}
        assert inputs["context_name"]["required"] is True, (
            "context_name carries the AC5 check; making it optional would let a "
            "caller silently skip the self-holding-name guard"
        )
        assert inputs["vocabulary_ref"]["default"] == "dev"

    def test_the_job_runs_on_a_github_hosted_runner(self) -> None:
        """No self-hosted/LAN dependency: the gate must render a verdict always."""
        assert self._job()["runs-on"] == "ubuntu-latest"

    def test_the_job_is_unconditional(self) -> None:
        job = self._job()
        assert "needs" not in job
        assert "if" not in job

    def test_all_four_verdicts_are_wired(self) -> None:
        """AC1 scan, AC5+AC3 self-proof, and the real evaluation all run."""
        runs = " ".join(
            str(step.get("run", "")) for step in self._job()["steps"] if "run" in step
        )
        assert "check_single_hold_vocabulary" in runs
        assert "check_hold_gate_selftest" in runs
        assert "check_pr_hold_marker.py" in runs

    def test_pr_surfaces_are_passed_as_env_not_interpolated_into_a_script(
        self,
    ) -> None:
        """A PR title is attacker-controlled on a fork; as env it is data."""
        env = self._job()["env"]
        assert "github.event.pull_request.title" in str(env["PR_TITLE"])
        assert "pull_request.labels" in str(env["PR_LABELS_JSON"])
        assert "github.event_name" in str(env["GITHUB_EVENT_NAME"])
        assert "github.repository" in str(env["GH_REPO"])
        runs = " ".join(
            str(step.get("run", "")) for step in self._job()["steps"] if "run" in step
        )
        assert "github.event" not in runs, (
            "no step may interpolate event data into a shell body — pass it "
            "through `env:` so it can never be evaluated as script"
        )

    def test_the_vocabulary_checkout_targets_omnimarket(self) -> None:
        steps = self._job()["steps"]
        checkout = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout")
            and step.get("with", {}).get("repository")
        )
        assert checkout["with"]["repository"] == "OmniNode-ai/omnimarket"
        assert checkout["with"]["path"] == "vocabulary"
        assert checkout["with"]["persist-credentials"] is False


class TestOmnimarketIsItsOwnFirstCaller:
    """A reusable surface the defining repo does not call is unproven."""

    @staticmethod
    def _caller_job() -> dict[str, Any]:
        workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        assert _CALLER_JOB_KEY in jobs, f"{_CALLER_JOB_KEY} is not a job in ci.yml"
        return dict(jobs[_CALLER_JOB_KEY])

    def test_ci_calls_the_reusable_workflow_locally(self) -> None:
        """Local `uses:` resolves at the PR head — it proves THIS diff."""
        assert (
            self._caller_job()["uses"]
            == "./.github/workflows/merge-hold-gate-reusable.yml"
        )

    def test_the_caller_is_unconditional(self) -> None:
        job = self._caller_job()
        assert "needs" not in job
        assert "if" not in job

    def test_the_declared_context_matches_the_job_key_and_inner_job(self) -> None:
        """The AC5 seam: what the caller declares must be what CI produces.

        ``context_name`` is a string the caller writes by hand, and the guard it
        feeds is only as good as that string being true. This asserts it equals
        the context GitHub will actually mint for this call.
        """
        declared = self._caller_job()["with"]["context_name"]
        assert declared == f"{_CALLER_JOB_KEY} / evaluate"

    def test_the_declared_context_is_not_itself_a_hold_token(self) -> None:
        canonical = load_canonical_hold_module()
        declared = self._caller_job()["with"]["context_name"]
        assert check_context_name(canonical, declared) is None

    def test_the_proof_call_pins_the_pr_under_review(self) -> None:
        """Not `dev`: the point is to execute the surface this PR adds."""
        assert "github.sha" in str(self._caller_job()["with"]["vocabulary_ref"])
