# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit the 24 committed task bundles for the delegation ladder (OMN-18300).

The bundles are committed, not generated at run time. This script exists so the
generation is auditable and so a reviewer can see exactly how each prompt was
assembled from its fixture, not so the prompts are rebuilt before each run --
rebuilding them would defeat the point of a fixed input bundle.

Run: ``python benchmarks/delegation_ladder/build_bundles.py``

LAYOUT
    bundles/     the 24+ committed task bundles: the exact fed text, and the
                 held answer, which is never fed
    contracts/   the task-complexity rubric: every threshold, weight and band
    fixtures/    subjects, mutants, pre-fix sources, focused tests, real traces,
                 the redacted ledger rows and the workflow catalogue
    results/     result tables and the raw responses behind them
    tests/       behaviour tests for the scorers and the rubric, each with a
                 negative control

ADDING A TASK
    1. Put the fixture in fixtures/, drawn from a real repository or the ledger.
       Ledger rows go through build_ledger_snapshot.py, which redacts them.
    2. For a unit-test task, add a mutant and prove by execution that it behaves
       differently from the subject.
    3. For a patch task, prove the focused test fails against the pre-fix source
       and passes against the real post-fix source.
    4. Add the spec below, rebuild, and run tests/. They assert the rung shape,
       the fixture references, that every coverage target is reachable in its own
       fed rows, that no diagnosis task can be passed by echoing its own trace,
       and that no complexity threshold has drifted out of the contract.

WHY THE BUNDLES ARE COMMITTED RATHER THAN BUILT AT RUN TIME
    A fixed input bundle that is rebuilt before each run is not fixed. The
    ledger grows; a rebuild silently changes what R2 is measuring, and results
    from before and after the rebuild are not comparable even though they carry
    the same task ids.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from benchmarks.delegation_ladder.models import (
    EnumRung,
    EnumScorerKind,
    ModelTaskBundle,
)
from benchmarks.delegation_ladder.scorers import (
    git_text,
    materialise_tree,
    run_tree_test,
)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BUNDLES = HERE / "bundles"

# The instruction every prompt carries. It forbids the reasoning trace
# explicitly, which is what makes a leaked trace a measurable defect
# (OMN-18278) rather than a stylistic preference.
NO_TRACE = (
    "Respond with the answer only. Do not show your reasoning, do not narrate "
    "your steps, and do not preface the answer with anything."
)


# Dependent reasoning steps, declared per rung under the counting rule stated in
# contracts/task_complexity_rubric.v1.yaml. Restating one fed fact is 1; reading
# fed rows and emitting a derived claim is 2; reading code, deriving a behaviour
# and expressing it as an artifact is 3; reading code, localising a defect,
# deriving the corrected behaviour and expressing it as a patch is 4.
DECLARED_STEPS: dict[str, int] = {
    "R1": 1,
    "R2": 2,
    "R3": 2,
    "R3b": 2,
    "R4": 3,
    "R5": 3,
    "R6": 4,
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _ledger_slice(task_id: str) -> str:
    text = _fixture("ledger_rows_snapshot.txt")
    marker = f"===== {task_id} ("
    start = text.index(marker)
    start = text.index("\n", start) + 1
    nxt = text.find("\n=====", start)
    return text[start : nxt if nxt != -1 else len(text)].strip()


# ==========================================================================
# R1 -- single-fact rewrite
# ==========================================================================


def r1() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []

    note = (
        "Mint a service-account token against auth.omninode.ai using client "
        "onex-verify, then call the verify endpoint with the returned bearer."
    )
    tasks.append(
        {
            "task_id": "R1-01",
            "title": "correct the issuer host in a runbook note",
            "task_type": "document",
            "provenance": (
                "The live defect recorded on OMN-18292: a runbook told operators to "
                "mint against the bare issuer host, which returns 401 "
                "unauthorized_client; the dev-system cluster's own issuer host is the "
                "dev-prefixed one. Same defect class as OMN-16504."
            ),
            "prompt": (
                "Rewrite the runbook note below so it names the dev-system cluster's "
                "own issuer host instead of the bare host. The bare host returns HTTP "
                "401 unauthorized_client; the correct host for this cluster is "
                "dev.auth.omninode.ai. Change nothing else: the client id and the "
                "steps stay exactly as they are.\n\n"
                f"NOTE:\n{note}\n\n" + NO_TRACE
            ),
            "scorer": "grounding_rubric",
            "scorer_config": {
                "source_text": note + " dev.auth.omninode.ai onex-verify",
                "must_contain": ["dev.auth.omninode.ai", "onex-verify"],
                "must_not_contain": ["against auth.omninode.ai"],
                "max_words": 60,
            },
        }
    )

    title = "local model makes up numbers on big inputs"
    tasks.append(
        {
            "task_id": "R1-02",
            "title": "rewrite a ticket title to carry its identifier",
            "task_type": "document",
            "provenance": (
                "OMN-18297, minted 2026-09-13 from dogfooding round 2: the local "
                "backend fabricated pull-request numbers absent from an 18K-token "
                "input while getting every ticket number right."
            ),
            "prompt": (
                "Rewrite this ticket title so it is suitable for the tracker. The "
                "title must contain the identifier OMN-18297, must describe the "
                "defect in neutral engineering language, and must stay on one line "
                "of at most twelve words.\n\n"
                f"CURRENT TITLE: {title}\n\n"
                "The defect: the local delegation backend invents repository "
                "identifiers that do not occur in its input once the input grows "
                "past roughly eighteen thousand tokens.\n\n" + NO_TRACE
            ),
            "scorer": "grounding_rubric",
            "scorer_config": {
                "source_text": "OMN-18297 " + title + " local delegation backend input",
                "must_contain": ["OMN-18297"],
                "must_not_contain": ["makes up"],
                "max_words": 16,
            },
        }
    )

    stale = (
        "Before every push, the governed impacted-test selector runs the repository's "
        "local test selection, and the merge gate is that local run plus CI."
    )
    tasks.append(
        {
            "task_id": "R1-03",
            "title": "correct a stale doctrine sentence about the pre-push test leg",
            "task_type": "document",
            "provenance": (
                "The pre-push governed test leg was retired repo by repo; the final "
                "one landed as omnibase_core#1678 under OMN-18176 on 2026-09-11. The "
                "sentence below was true before that and is false after it."
            ),
            "prompt": (
                "The sentence below is now false. As of omnibase_core#1678 the "
                "pre-push governed test leg is retired: pre-push runs check-only "
                "policy, type and lint hooks and no local test selection, and the "
                "merge gate is CI alone. Rewrite the sentence so it states the "
                "current arrangement and cites omnibase_core#1678. Two sentences at "
                "most.\n\n"
                f"SENTENCE:\n{stale}\n\n" + NO_TRACE
            ),
            "scorer": "grounding_rubric",
            "scorer_config": {
                "source_text": (
                    stale
                    + " omnibase_core#1678 pre-push check-only policy type lint hooks "
                    "no local test selection merge gate CI alone retired"
                ),
                "must_contain": ["omnibase_core#1678", "ci"],
                "must_not_contain": ["local test selection, and the merge gate"],
                "max_words": 70,
            },
        }
    )

    wrong = (
        "The mutable dev lane runs under compose project omnibase-infra and exposes "
        "its main port on 18085 and its effects port on 18086."
    )
    tasks.append(
        {
            "task_id": "R1-04",
            "title": "correct the port numbers in a lane-map sentence",
            "task_type": "document",
            "provenance": (
                "The generated runtime lane map: the dev lane is compose project "
                "omnibase-infra on main port 8085 and effects port 8086; 18085/18086 "
                "belong to the stability-test lane, which is a different lane with a "
                "different mutation boundary."
            ),
            "prompt": (
                "The sentence below states the wrong ports. The dev lane's main port "
                "is 8085 and its effects port is 8086. The ports it currently names "
                "belong to the stability-test lane, which is a separate lane. Correct "
                "the sentence. Keep the compose project name exactly as it is.\n\n"
                f"SENTENCE:\n{wrong}\n\n" + NO_TRACE
            ),
            "scorer": "grounding_rubric",
            "scorer_config": {
                "source_text": wrong + " 8085 8086 stability-test dev lane",
                "must_contain": ["8085", "8086", "omnibase-infra"],
                "must_not_contain": ["on 18085", "18086."],
                "max_words": 60,
            },
        }
    )
    return tasks


# ==========================================================================
# R2 -- multi-source summary over real ledger rows
# ==========================================================================

_R2_SPECS: tuple[tuple[str, str, str, list[str], float], ...] = (
    (
        "R2-01",
        "what landed, from twelve ledger rows",
        "Summarise what these fleet-coordination ledger rows say landed. One bullet "
        "per distinct piece of work, at most eight bullets. Cite the ticket "
        "identifier for every bullet. Use only identifiers that appear in the rows "
        "below; if you are unsure of an identifier, leave it out rather than "
        "guessing at one.",
        [],
        0.5,
    ),
    (
        "R2-02",
        "open blockers, from fifteen ledger rows",
        "From these fleet-coordination ledger rows, list every piece of work that is "
        "recorded as blocked, held, or not yet done, and say what each is waiting "
        "on. Cite the ticket identifier for each. Use only identifiers that appear "
        "in the rows below; if you are unsure of an identifier, leave it out rather "
        "than guessing at one.",
        [],
        0.4,
    ),
    (
        "R2-03",
        "one line per lane, from twenty ledger rows",
        "These fleet-coordination ledger rows come from several concurrent work "
        "lanes. Give one line per lane naming the lane and what it did. Use only "
        "lane names and identifiers that appear in the rows below; if you are "
        "unsure of one, leave it out rather than guessing at it.",
        [],
        0.4,
    ),
    (
        "R2-04",
        "ticket identifiers touched, from twenty-five ledger rows",
        "These fleet-coordination ledger rows are the recent history of several "
        "concurrent work lanes. List every ticket identifier that appears, grouped "
        "by the lane that touched it. Do not include any identifier that does not "
        "appear in the rows below.",
        [],
        0.4,
    ),
)


def r2() -> list[dict[str, object]]:
    import re

    tasks: list[dict[str, object]] = []
    for task_id, title, instruction, _seed, threshold in _R2_SPECS:
        rows = _ledger_slice(task_id)
        # The coverage target is derived from the fed text, not hand-picked: the
        # ticket identifiers that occur at least twice are the ones the rows are
        # actually about, and a summary that omits all of them has not summarised.
        counts: dict[str, int] = {}
        for token in re.findall(r"\bOMN-\d+\b", rows):
            counts[token] = counts.get(token, 0) + 1
        required = sorted(t for t, c in counts.items() if c >= 2)[:10]
        tasks.append(
            {
                "task_id": task_id,
                "title": title,
                "task_type": "document",
                "provenance": (
                    "Real rows from the fleet coordination ledger, 2026-09-11 to "
                    "2026-09-13, committed verbatim at "
                    "fixtures/ledger_rows_snapshot.txt with local clone paths and lab "
                    "host addresses replaced by placeholders."
                ),
                "prompt": f"{instruction}\n\nROWS:\n{rows}\n\n{NO_TRACE}",
                "scorer": "grounding_coverage",
                "scorer_config": {
                    "source_text": rows,
                    "required_ids": required,
                    "coverage_threshold": threshold,
                },
            }
        )
    return tasks


# ==========================================================================
# R3 -- code reading question and answer
# ==========================================================================

_R3_SPECS: tuple[tuple[str, str, str, str, str, list[str], list[str]], ...] = (
    (
        "R3-01",
        "case-insensitive conflict detection returns the normalised form",
        "r4_02_subject.py",
        "detect_add_remove_conflicts",
        'What exactly does detect_add_remove_conflicts(["Foo"], ["foo"], '
        '"handlers") return? Give the exact Python value.',
        ["['foo']"],
        ["['Foo']", "[]"],
    ),
    (
        "R3-02",
        "a single-segment import path is rejected with a specific message",
        "r4_03_subject.py",
        "validate_import_path_format",
        'What exactly does validate_import_path_format("singlemodule") return? '
        "Give the exact Python tuple, including the full message string.",
        [
            "False",
            "Import path must include module and class (at least 2 segments)",
        ],
        ["True, None", "Import path cannot be empty"],
    ),
    (
        "R3-03",
        "an inclusive lower bound and an exclusive upper bound",
        "r3_03_subject.py",
        "_satisfies_specifier",
        'What does _satisfies_specifier("2.0.0", ">=1.0.0,<2.0.0") return, and '
        "why? Answer with the exact boolean and one sentence of reason.",
        ["False"],
        [],
    ),
    (
        "R3-04",
        "backslash continuation folds lines and keeps the opening line number",
        "r3_04_subject.py",
        "_iter_logical_lines",
        "The function below is given the four-line string whose lines are, in "
        "order: a, then b followed by a single backslash, then c, then d. What "
        "exactly does it return? Give the exact Python list of tuples.",
        ["(1, 'a')", "(2, 'b c')", "(4, 'd')"],
        ["(3,"],
    ),
)


def r3() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for task_id, title, fixture, symbol, question, accepted, forbidden in _R3_SPECS:
        source = _fixture(fixture)
        tasks.append(
            {
                "task_id": task_id,
                "title": title,
                "task_type": "review",
                "provenance": (
                    f"{symbol}, extracted verbatim from the workspace repositories; "
                    "the held answer was confirmed by executing the real function "
                    "against the installed package before the task was written."
                ),
                "prompt": (
                    f"Read this module and answer the question about it.\n\n"
                    f"```python\n{source}```\n\n"
                    f"QUESTION: {question}\n\n{NO_TRACE}"
                ),
                "scorer": "exact_match",
                "scorer_config": {"accepted": accepted, "forbidden": forbidden},
            }
        )
    return tasks


# ==========================================================================
# R4 -- write a unit test, graded by running it
# ==========================================================================

_R4_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("R4-01", "is_valid_onex_name", "r4_01", "the empty-string case"),
    (
        "R4-02",
        "detect_add_remove_conflicts",
        "r4_02",
        "the default case-insensitive comparison",
    ),
    (
        "R4-03",
        "validate_import_path_format",
        "r4_03",
        "the minimum-segment-count rule",
    ),
    ("R4-04", "extract_imports", "r4_04", "the full dotted path, not the top level"),
)


def r4() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for task_id, symbol, stem, emphasis in _R4_SPECS:
        source = _fixture(f"{stem}_subject.py")
        tasks.append(
            {
                "task_id": task_id,
                "title": f"write a unit test for {symbol}",
                "task_type": "test",
                "provenance": (
                    f"{symbol}, extracted verbatim from the workspace repositories. "
                    "The mutant is a single deliberate edit whose behavioural "
                    "difference was confirmed by execution before the task was written."
                ),
                "prompt": (
                    "Write a pytest test module for the function below.\n\n"
                    "Rules the grader enforces:\n"
                    "- The module under test is importable as `subject`. Import the "
                    f"function with `from subject import {symbol}`.\n"
                    "- Use plain pytest: test functions named `test_*`, plain "
                    "`assert`. No fixtures, no classes, no mocking, no imports "
                    "beyond `subject` and the standard library.\n"
                    "- Your tests will be run twice: once against this function, "
                    "where they must all pass, and once against a copy containing a "
                    f"single deliberate defect around {emphasis}, where at least one "
                    "of them must fail. A test suite that passes both is worthless.\n"
                    "- Output one fenced Python code block and nothing else.\n\n"
                    f"```python\n{source}```\n\n{NO_TRACE}"
                ),
                "scorer": "unit_test_execution",
                "scorer_config": {
                    "subject_file": f"{stem}_subject.py",
                    "mutant_file": f"{stem}_mutant.py",
                    "timeout_s": 90,
                },
                "fixture_files": {
                    f"{stem}_subject.py": "subject",
                    f"{stem}_mutant.py": "mutant",
                },
            }
        )
    return tasks


# ==========================================================================
# R5 -- diagnose a failing test from its trace and its source
# ==========================================================================

_R5_SPECS: tuple[tuple[str, str, str, list[str], list[str], list[str]], ...] = (
    (
        "R5-01",
        "r4_01",
        "is_valid_onex_name",
        ["is_valid_onex_name"],
        ["*$", "asterisk", "zero or more", "if false", "quantifier", "always false"],
        ["detect_add_remove_conflicts"],
    ),
    (
        "R5-02",
        "r4_02",
        "detect_add_remove_conflicts",
        ["detect_add_remove_conflicts", "case_sensitive"],
        ["true", "default"],
        ["is_valid_onex_name"],
    ),
    (
        "R5-03",
        "r4_03",
        "validate_import_path_format",
        ["validate_import_path_format"],
        ["len(parts) < 1", "< 1", "less than 1", "fewer than 1", "1 instead of 2"],
        ["is_valid_python_identifier is wrong"],
    ),
    (
        "R5-04",
        "r4_04",
        "extract_imports",
        ["extract_imports"],
        ['split(".")[0]', "split", "top-level", "top level", "truncat"],
        ["syntaxerror handling is wrong"],
    ),
)


def r5() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for task_id, stem, symbol, must_name, any_of, decoys in _R5_SPECS:
        source = _fixture(f"{stem}_mutant.py")
        trace = _fixture(f"r5_{task_id.split('-')[1]}_trace.txt")
        tasks.append(
            {
                "task_id": task_id,
                "title": f"diagnose the failing test for {symbol}",
                "task_type": "reasoning",
                "provenance": (
                    "The trace is genuine pytest output, produced by running the "
                    f"committed focused test against the committed defective copy of "
                    f"{symbol}. The held cause is the single edited line."
                ),
                "prompt": (
                    "This test suite fails. Name the root cause: the function at "
                    "fault and the specific line that is wrong, and say in one "
                    "sentence what the line does wrong. Do not propose a fix and do "
                    "not rewrite the code.\n\n"
                    f"SOURCE (subject.py):\n```python\n{source}```\n\n"
                    f"PYTEST OUTPUT:\n```\n{trace}```\n\n{NO_TRACE}"
                ),
                "scorer": "diagnosis_location",
                "scorer_config": {
                    "must_name": must_name,
                    "any_of": any_of,
                    "must_not_name": decoys,
                },
            }
        )
    return tasks


# ==========================================================================
# R6 -- produce a patch for a known-fixed bug
# ==========================================================================

_R6_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "R6-01",
        "r6_01",
        "_is_occ_repo",
        "omnibase_core 28b8a33a360d6ca05a0a916a498c6e50ac07caee (OMN-16353), 16 changed lines",
    ),
    (
        "R6-02",
        "r6_02",
        "extract_content",
        "omnimarket bc664339bc2b008335747446a07cfba430eb6467 (OMN-12643), 6 changed lines",
    ),
    (
        "R6-03",
        "r6_03",
        "HandlerProjectionDelegation.handle",
        "omnimarket 60b188edf272f3ef568e6fb5e30ed50b5c4eb150 (OMN-14855), 7 changed lines",
    ),
    (
        "R6-04",
        "r6_04",
        "LongLivedTerminalCorrelator",
        "omnibase_infra d703ca7fdf4ee97974a1f7e75730344660679bcf (OMN-13118), 18 changed lines",
    ),
)


def r6() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for task_id, stem, symbol, origin in _R6_SPECS:
        prelude = _fixture(f"{stem}_prelude.py")
        prefix = _fixture(f"{stem}_prefix.py")
        test = _fixture(f"{stem}_test.py")
        trace = _fixture(f"{stem}_trace.txt")
        tasks.append(
            {
                "task_id": task_id,
                "title": f"fix the defect in {symbol}",
                "task_type": "code_generation",
                "provenance": (
                    f"Reconstructed pre-fix state of a real merged fix: {origin}. The "
                    "focused test is confirmed to fail against this pre-fix source and "
                    "pass against the real post-fix source."
                ),
                "prompt": (
                    "The module below has a defect. Fix it.\n\n"
                    "Rules the grader enforces:\n"
                    "- Output the complete replacement for everything below the "
                    "imports, as one fenced Python code block, and nothing else. The "
                    "import block shown first is kept as is and prepended to your "
                    "code; do not repeat it.\n"
                    "- Any new module-level name your fix needs must appear in your "
                    "code block.\n"
                    "- Your code will be run against the focused test shown, which "
                    "must pass in full.\n"
                    "- Change only what the defect requires.\n\n"
                    f"IMPORTS (kept, do not repeat):\n```python\n{prelude}```\n\n"
                    f"SOURCE UNDER REPAIR:\n```python\n{prefix}```\n\n"
                    f"FOCUSED TEST (test_focused.py):\n```python\n{test}```\n\n"
                    f"CURRENT FAILURE:\n```\n{trace}```\n\n{NO_TRACE}"
                ),
                "scorer": "patch_apply_and_test",
                "scorer_config": {
                    "prelude_file": f"{stem}_prelude.py",
                    "prefix_body_file": f"{stem}_prefix.py",
                    "focused_test_file": f"{stem}_test.py",
                    "timeout_s": 120,
                },
                "fixture_files": {
                    f"{stem}_prelude.py": "prelude",
                    f"{stem}_prefix.py": "prefix_body",
                    f"{stem}_test.py": "focused_test",
                },
            }
        )
    return tasks


# ==========================================================================
# R3b -- choose a workflow from the catalogue and fill its parameters
# ==========================================================================
#
# This rung exists because the execution-surface proposal puts parameterised
# mechanical workflows ahead of a free-running agent. If that is the shape, then
# the model's job is workflow SELECTION plus PARAMETER FILLING, and that job has
# to be measured like any other rung rather than assumed easy. Each task carries
# a plausible wrong choice, because a rung whose every task has one obvious
# answer measures nothing.

_R3B_SPECS: tuple[tuple[str, str, str, list[str], list[str]], ...] = (
    (
        "R3b-01",
        "run one failing test file",
        "A change to src/omnibase_core/validation/validator_occ_merge_eligibility.py "
        "is suspected of breaking the test file "
        "tests/unit/validation/test_occ_merge_eligibility.py in the omnibase_core "
        "repository. Run just that test file and report the result. Nothing has "
        "been recorded as failing anywhere else yet, and there is no patch to "
        "apply.",
        [
            "run_focused_tests",
            "omnibase_core",
            "tests/unit/validation/test_occ_merge_eligibility.py",
        ],
        ["reproduce_failing_run", "apply_patch_and_test", "verify_ticket_evidence"],
    ),
    (
        "R3b-02",
        "start from a recorded failing run, not from a guess",
        "Continuous-integration run 34748103920 in the omnibase_infra repository "
        "failed. Nobody knows which test failed or why. Find out, by reproducing "
        "what that run did.",
        ["reproduce_failing_run", "34748103920", "omnibase_infra"],
        ["run_focused_tests", "select_impacted_tests", "read_and_answer"],
    ),
    (
        "R3b-03",
        "a question about code, with nothing to run",
        "Someone asks what the function _satisfies_specifier returns when it is "
        "given the version string 2.0.0 against the specifier that requires at "
        "least 1.0.0 and less than 2.0.0. The function lives in "
        "scripts/check_dockerfile_pins.py in the omnibase_infra repository. No "
        "test is failing and nothing needs to be executed.",
        [
            "read_and_answer",
            "scripts/check_dockerfile_pins.py",
            "omnibase_infra",
        ],
        ["run_focused_tests", "apply_patch_and_test", "reproduce_failing_run"],
    ),
    (
        "R3b-04",
        "a patch in hand, and one named test to prove it",
        "A unified diff against src/omnimarket/cost/usage_normalizer.py in the "
        "omnimarket repository is ready. It must be applied and then proven by "
        "the test file tests/unit/cost/test_usage_normalizer_omn13621.py. The "
        "diff is in hand; it has not been committed and there is no recorded run "
        "for it.",
        [
            "apply_patch_and_test",
            "omnimarket",
            "tests/unit/cost/test_usage_normalizer_omn13621.py",
        ],
        ["run_focused_tests", "reproduce_failing_run", "select_impacted_tests"],
    ),
)


def r3b() -> list[dict[str, object]]:
    catalogue = _fixture("workflow_catalogue.txt")
    tasks: list[dict[str, object]] = []
    for task_id, title, situation, accepted, forbidden in _R3B_SPECS:
        tasks.append(
            {
                "task_id": task_id,
                "title": title,
                "task_type": "review",
                "provenance": (
                    "The catalogue is the one proposed in the delegated "
                    "execution-surface plan; every workflow in it is packaging of a "
                    "surface that already exists in the workspace. The situations "
                    "are real repository paths and a real recorded run id."
                ),
                "prompt": (
                    "Choose exactly one workflow from the catalogue below and fill "
                    "in its parameters for the situation described.\n\n"
                    "Answer with the workflow name on the first line, then a JSON "
                    "object of its parameters. Choose nothing that the situation "
                    "does not supply the inputs for.\n\n"
                    f"{catalogue}\n"
                    f"SITUATION:\n{situation}\n\n" + NO_TRACE
                ),
                "scorer": "exact_match",
                "scorer_config": {"accepted": accepted, "forbidden": forbidden},
            }
        )
    return tasks


# ==========================================================================
# Mined tasks (OMN-20844): real merged work from the public repositories
# ==========================================================================
#
# Four tasks a rung cannot separate delegation rungs: with 28 tasks every
# rung's interval overlapped every other's. The tasks below are not written for
# the benchmark. They are drawn from the merge history and the test suites of
# the public repositories:
#
#   document       a merged change's own description (the squash commit message)
#                  to rewrite, and runs of merged changes to summarise
#   review         the result of a call that a repository test asserts, and the
#                  one line a merged fix changed
#   test           a module with an existing test suite, plus a single edit that
#                  the existing tests are proven to catch
#   reasoning      a genuine pytest failure, from a merged fix's pre-fix code
#                  or from such a caught edit
#   code_generation the pre-fix block of a merged change, whose own test fails
#                  before the change and passes after it
#
# Every mined task is validated by EXECUTION before it is written, the same way
# the hand-built rungs are: the control must fail, the real code must pass, and
# a diagnosis cannot be passed by echoing its own trace. Sources are inline in
# each bundle (scorer_config), so a mined task is one self-contained JSON file
# and adds no code file to the tree.
#
# Run: ``OMNI_HOME=<registry root> python benchmarks/delegation_ladder/build_bundles.py mine``
# Sources are read from the registry's canonical clones at the pinned shas
# written to fixtures/mined_sources.json.

MINED_REPOS = (
    "omnimarket",
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omniclaude",
    "omniintelligence",
    "omnimemory",
    "omnibase_compat",
)
MINED_SOURCES = FIXTURES / "mined_sources.json"
_INTERNAL_REPO = "omnibase" + "_internal"
PERSON_NAMES_RELATIVE = ("..", _INTERNAL_REPO, "vocabularies", "person_names.txt")

# What a public benchmark bundle must not carry from the workspace it was mined
# in. A candidate that matches any of these is dropped, never edited.
_HYGIENE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        "omni" r"_home",
        "knowledge" r"[_ -]?base",
        "kb" r"[_-]internal",
        "omnibase" r"[_-]internal",
        "omninode" r"[_-]infra",
        r"api\.z\.ai",
        "/api/coding",
        r"\bstability-test\b",
        r"/home/",
        r"/users/",
        r"\b192\.168\.",
        r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
        r"[\w.+-]+@[\w-]+\.[\w.]+",
        r"BEGIN [A-Z ]*PRIVATE KEY",
        r"\bsk-[A-Za-z0-9_-]{16,}",
        r"\bghp_[A-Za-z0-9]{20,}",
        r"\bxox[abp]-",
        r"AKIA[0-9A-Z]{12,}",
    )
)


def _person_names() -> set[str]:
    home = os.environ.get("OMNI_HOME")
    if not home:
        return set()
    path = Path(home).joinpath(*PERSON_NAMES_RELATIVE)
    if not path.is_file():
        return set()
    return {
        line.strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if len(line.strip()) >= 4 and not line.startswith("#")
    }


def hygiene_problems(text: str, names: set[str] | None = None) -> list[str]:
    """Reasons ``text`` must not be published in a public benchmark bundle."""
    problems = [p.pattern for p in _HYGIENE_PATTERNS if p.search(text)]
    lowered = text.lower()
    for name in names or ():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            problems.append("person name")
            break
    return problems


def _clone(repo: str) -> Path:
    home = os.environ.get("OMNI_HOME")
    if not home:
        raise RuntimeError("OMNI_HOME must be set; there is no default")
    return Path(home) / repo


def _git(repo: str, *args: str) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(_clone(repo)), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def _pin(repo: str) -> str | None:
    out = _git(repo, "rev-parse", "origin/HEAD")
    return out.strip() if out else None


def _module_name(path: str) -> str:
    dotted = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def _rewrite_test(test: str, dotted: str) -> str:
    """Point a repository test at the harness's ``subject`` module."""
    leaf = dotted.rsplit(".", 1)[-1]
    parent = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    out = test
    if parent:
        out = re.sub(
            rf"^from {re.escape(parent)} import {re.escape(leaf)}[ \t]*$",
            f"import subject as {leaf}",
            out,
            flags=re.MULTILINE,
        )
    return out.replace(dotted, "subject")


_TIMING_RE = re.compile(r" in \d+(?:\.\d+)?s\b")
_SITE_PACKAGES_RE = re.compile(r"(?:/[\w.\-+]+)+/site-packages/")
# pytest's warnings block carries absolute paths and varies by environment.
_WARNINGS_RE = re.compile(
    r"^=+ warnings summary =+$.*?^-- Docs: \S+\n?", re.MULTILINE | re.DOTALL
)


def _run_focused(
    subject: str,
    test: str,
    *,
    tb: str = "short",
    timeout_s: int = 90,
    extra: tuple[str, ...] = (),
) -> tuple[int, str]:
    """pytest on one rewritten test against one subject; the output is scrubbed.

    Scrubbed so the committed trace is reproducible and carries no path from
    the machine that mined it: temp dirs, package paths and run timings go.
    """
    with tempfile.TemporaryDirectory(prefix="ladder-mine-") as tmp:
        work = Path(tmp)
        (work / "subject.py").write_text(subject, encoding="utf-8")
        (work / "test_focused.py").write_text(test, encoding="utf-8")
        try:
            done = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "test_focused.py",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--tb={tb}",
                    *extra,
                ],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        out = (done.stdout + done.stderr).replace(str(work), ".")
    out = _SITE_PACKAGES_RE.sub("site-packages/", out)
    out = _WARNINGS_RE.sub("", out)
    return done.returncode, _TIMING_RE.sub("", out).strip()


def _numbered(lines: list[str], first: int = 1) -> str:
    width = len(str(first + len(lines)))
    return "".join(f"{first + i:>{width}} | {line}" for i, line in enumerate(lines))


def _top_level_spans(tree: ast.Module) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for node in tree.body:
        decorators = [d.lineno for d in getattr(node, "decorator_list", [])]
        spans.append((min([node.lineno, *decorators]), node.end_lineno or node.lineno))
    return spans


def _enclosing_name(tree: ast.Module, line: int) -> str | None:
    """The innermost function (or the top-level definition) holding ``line``."""
    best: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.lineno <= line <= (node.end_lineno or node.lineno)
            and (best is None or node.lineno > best[0])
        ):
            best = (node.lineno, node.name)
    if best:
        return best[1]
    for node in tree.body:
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            name = getattr(node, "name", None)
            if isinstance(name, str):
                return name
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                return node.targets[0].id
    return None


def _import_block(source: str) -> str:
    """The top-level import statements of a module, as written."""
    tree = ast.parse(source)
    lines = source.splitlines()
    chunks = [
        "\n".join(lines[n.lineno - 1 : n.end_lineno])
        for n in tree.body
        if isinstance(n, ast.Import | ast.ImportFrom)
    ]
    return "\n".join(chunks) + "\n"


def _block_for_change(
    pre_lines: list[str], post_lines: list[str]
) -> dict[str, Any] | None:
    """The one run of top-level definitions a change lives in, or None.

    Every changed line of the pre-fix file must fall inside the same run of at
    most four adjacent top-level definitions (with the blank lines between
    them), so that module = prelude + block + suffix holds for the pre-fix
    source and, with the block replaced, for the post-fix source. A definition
    added right after a block belongs to that block.
    """
    matcher = difflib.SequenceMatcher(None, pre_lines, post_lines, autojunk=False)
    ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    if not ops:
        return None
    try:
        spans = _top_level_spans(ast.parse("".join(pre_lines)))
    except SyntaxError:
        return None
    if not spans:
        return None

    def index_of(line: int) -> int:
        for index, (start, end) in enumerate(spans):
            if line <= end:
                return index if line >= start or index == 0 else index - 1
        return len(spans) - 1

    touched: list[int] = []
    removed: list[int] = []
    appended_after: int | None = None
    for tag, i1, i2, _j1, _j2 in ops:
        if tag == "insert":
            touched.append(max(i1, 1))
            appended_after = i1
        else:
            touched.extend(range(i1 + 1, i2 + 1))
            removed.extend(range(i1 + 1, i2 + 1))
    first, last = index_of(min(touched)), index_of(max(touched))
    if last - first > 3:
        return None
    start = min(spans[first][0], min(touched))
    end = max(spans[last][1], max(touched))
    if appended_after is not None and appended_after >= spans[last][1]:
        following = spans[last + 1][0] - 1 if last + 1 < len(spans) else len(pre_lines)
        end = max(end, following)
    if end - start + 1 > 150:
        return None
    tail = len(pre_lines) - end
    post_block = post_lines[start - 1 : len(post_lines) - tail]
    if post_lines[: start - 1] != pre_lines[: start - 1]:
        return None
    if tail and post_lines[len(post_lines) - tail :] != pre_lines[end:]:
        return None
    return {
        "start": start,
        "end": end,
        "removed": removed,
        "prelude": "".join(pre_lines[: start - 1]),
        "block": "".join(pre_lines[start - 1 : end]),
        "post_block": "".join(post_block),
        "suffix": "".join(pre_lines[end:]),
        "single_line": len(ops) == 1
        and ops[0][0] == "replace"
        and ops[0][2] - ops[0][1] == 1,
    }


# --------------------------------------------------------------------------
# Source 1: merged fixes and features whose own test fails before and passes after
# --------------------------------------------------------------------------

_WORK_SUBJECT_RE = re.compile(r"(?i)^(fix|bugfix|feat|refactor|perf)\b")
_TEST_PATH_RE = re.compile(r"(^|/)tests?/|/test_[^/]*\.py$|^test_")


def _fix_commit_candidates(repo: str, ref: str, limit: int) -> list[dict[str, Any]]:
    raw = _git(
        repo,
        "log",
        ref,
        "--no-merges",
        "--format=@@%H\t%P\t%s",
        "--name-status",
        "-n",
        str(limit),
    )
    if raw is None:
        return []
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if line.startswith("@@"):
            sha, parents, subject = line[2:].split("\t", 2)
            current = {
                "repo": repo,
                "sha": sha,
                "parent": parents.split()[0] if parents else None,
                "subject": subject,
                "files": [],
            }
            commits.append(current)
        elif line.strip() and current is not None:
            parts = line.split("\t")
            current["files"].append((parts[0], parts[-1]))
    found: list[dict[str, Any]] = []
    for commit in commits:
        kind = _WORK_SUBJECT_RE.match(commit["subject"])
        if not commit["parent"] or kind is None:
            continue
        py = [(st, f) for st, f in commit["files"] if f.endswith(".py")]
        tests = [f for st, f in py if _TEST_PATH_RE.search(f) and st in ("A", "M")]
        srcs = [
            f
            for st, f in py
            if not _TEST_PATH_RE.search(f) and st == "M" and f.startswith("src/")
        ]
        if not 1 <= len(srcs) <= 3 or not 1 <= len(tests) <= 3:
            continue
        commit["kind"] = kind.group(1).lower()
        commit["srcs"] = srcs
        commit["tests"] = tests
        found.append(commit)
    return found


_FAILED_RE = re.compile(
    r"^FAILED test_focused\.py::(\S+?)(?:\[.*?\])?(?: - .*)?$", re.MULTILINE
)


def _slice_test(test_text: str, failing: set[str]) -> str | None:
    """Keep the module-level setup and only the tests that failed before the fix."""
    try:
        tree = ast.parse(test_text)
    except SyntaxError:
        return None
    drop: list[tuple[int, int]] = []

    def bounds(node: ast.stmt) -> tuple[int, int]:
        decorators = [d.lineno for d in getattr(node, "decorator_list", [])]
        return min([node.lineno, *decorators]), node.end_lineno or node.lineno

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test") and node.name not in failing:
                drop.append(bounds(node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for method in node.body:
                if (
                    isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
                    and method.name.startswith("test")
                    and f"{node.name}::{method.name}" not in failing
                ):
                    drop.append(bounds(method))
    lines = test_text.splitlines(keepends=True)
    dead = {n for lo, hi in drop for n in range(lo, hi + 1)}
    kept = "".join(ln for i, ln in enumerate(lines, start=1) if i not in dead)
    return kept if kept.strip() else None


def _count_tests(test_text: str) -> int:
    return len(re.findall(r"^\s*(?:async )?def test", test_text, re.MULTILINE))


def validate_fix(commit: dict[str, Any], names: set[str]) -> dict[str, Any] | None:
    """The first (changed module, its test) pair of a merged change that flips."""
    for src in commit["srcs"]:
        for test in commit["tests"]:
            pair = {**commit, "src": src, "test": test}
            result = _validate_pair(pair, names)
            if result is not None:
                return result
    return None


def _validate_pair(commit: dict[str, Any], names: set[str]) -> dict[str, Any] | None:
    """Run a merged change's own test before and after; keep only a clean flip."""
    repo = commit["repo"]
    pre = _git(repo, "show", f"{commit['parent']}:{commit['src']}")
    post = _git(repo, "show", f"{commit['sha']}:{commit['src']}")
    test = _git(repo, "show", f"{commit['sha']}:{commit['test']}")
    if pre is None or post is None or test is None:
        return None
    if pre.count("\n") > 3000 or test.count("\n") > 1500:
        return None
    dotted = _module_name(commit["src"])
    if dotted not in test and dotted.rsplit(".", 1)[-1] not in test:
        return None
    rewritten = _rewrite_test(test, dotted)
    if "subject" not in rewritten:
        return None
    block = _block_for_change(
        pre.splitlines(keepends=True), post.splitlines(keepends=True)
    )
    if block is None or (not block["removed"] and commit["kind"] == "fix"):
        return None
    changed = sum(
        1
        for ln in difflib.unified_diff(
            pre.splitlines(), post.splitlines(), lineterm="", n=0
        )
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    )
    if not 1 <= changed <= 80:
        return None
    pre_rc, pre_out = _run_focused(pre, rewritten, extra=("-rf",))
    post_rc, _ = _run_focused(post, rewritten)
    if pre_rc in (0, 5, 124) or post_rc != 0:
        return None
    if re.search(r"ModuleNotFoundError|ImportError|fixture '.*' not found", pre_out):
        return None
    failing = set(_FAILED_RE.findall(pre_out))
    if not failing:
        return None
    if rewritten.count("\n") > 120 or _count_tests(rewritten) > 6:
        sliced = _slice_test(rewritten, failing | {f.split("::")[-1] for f in failing})
        if sliced is None or sliced.count("\n") > 200:
            return None
        rewritten = sliced
        pre_rc, pre_out = _run_focused(pre, rewritten)
        post_rc, _ = _run_focused(post, rewritten)
        if pre_rc in (0, 5, 124) or post_rc != 0:
            return None
    pre_out = re.sub(r"^FAILED .*\n?", "", pre_out, flags=re.MULTILINE).strip()
    if rewritten.count("\n") > 200 or not re.search(r"\d+ failed", pre_out):
        return None
    if hygiene_problems(pre + rewritten + pre_out, names):
        return None
    first_removed = block["removed"][0] if block["removed"] else block["start"]
    return {
        **commit,
        "dotted": dotted,
        "pre": pre,
        "test_text": rewritten,
        "trace": pre_out,
        "block": block,
        "enclosing": _enclosing_name(ast.parse(pre), first_removed),
        "changed_lines": changed,
    }


def mine_fixes(limit: int = 6000, workers: int = 10) -> list[dict[str, Any]]:
    import concurrent.futures

    names = _person_names()
    candidates: list[dict[str, Any]] = []
    for repo in MINED_REPOS:
        ref = _pin(repo)
        if ref:
            candidates += _fix_commit_candidates(repo, ref, limit)
    candidates.sort(key=lambda c: (c["repo"], c["sha"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda c: validate_fix(c, names), candidates))
    return [r for r in results if r is not None]


_FIX_RULES = (
    "Rules the grader enforces:\n"
    "- The block under repair is one part of a larger module. Output the complete "
    "replacement for that block, as one fenced Python code block, and nothing else.\n"
    "- The rest of the module (its imports and every definition before and after "
    "the block) is kept as it is and is not shown again. Do not repeat it.\n"
    "- If your change needs a new import or helper, put it inside your code block.\n"
    "- Your code will be run against the focused test shown, which must pass in "
    "full.\n"
    "- Change only what the failure requires.\n\n"
)


def codegen_task(fix: dict[str, Any]) -> dict[str, Any]:
    block = fix["block"]
    return {
        "title": f"make the focused test pass in {fix['enclosing'] or 'the block'}",
        "task_type": "code_generation",
        "provenance": (
            f"A real merged change, {fix['repo']} {fix['sha']} ({fix['kind']}), "
            f"changing {fix['changed_lines']} lines of {fix['src']}. The focused test "
            "is the change's own test, rewritten to import the module as `subject`; "
            "it is confirmed to fail on the parent's source and pass on the merged "
            "source."
        ),
        "prompt": (
            "The code below fails the focused test shown. Fix it.\n\n"
            + _FIX_RULES
            + f"IMPORTS ALREADY IN THE MODULE (kept, do not repeat):\n```python\n"
            f"{_import_block(fix['pre'])}```\n\n"
            f"BLOCK UNDER REPAIR:\n```python\n{block['block']}```\n\n"
            f"FOCUSED TEST (test_focused.py; the module is importable as "
            f"`subject`):\n```python\n{fix['test_text']}```\n\n"
            f"CURRENT FAILURE:\n```\n{fix['trace']}\n```\n\n{NO_TRACE}"
        ),
        "scorer": "patch_apply_and_test",
        "scorer_config": {
            "prelude_text": block["prelude"],
            "prefix_body_text": block["block"],
            "suffix_text": block["suffix"],
            "focused_test_text": fix["test_text"],
            "timeout_s": 120,
        },
    }


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def reasoning_task_from_fix(fix: dict[str, Any]) -> dict[str, Any] | None:
    """Diagnose the genuine failure of a merged fix's pre-fix code."""
    block = fix["block"]
    removed = block["removed"]
    pre_lines = fix["pre"].splitlines()
    if not removed or not fix["enclosing"]:
        return None
    lines_text = [pre_lines[n - 1].strip() for n in removed]
    quotes = [t for t in lines_text if len(t) >= 12][:3]
    any_of = [*quotes, *[f"line {n}" for n in removed[:3]]]
    first = block["start"]
    shown = fix["pre"].splitlines(keepends=True)[first - 1 : block["end"]]
    return {
        "title": f"diagnose the failing test for {fix['enclosing']}",
        "task_type": "reasoning",
        "provenance": (
            f"A real merged fix, {fix['repo']} {fix['sha']}, to {fix['src']}. The "
            "trace is genuine pytest output from running the fix's own test against "
            "the parent's source. The held cause is the line the fix removed or "
            "replaced."
        ),
        "prompt": (
            "This test suite fails. Name the root cause: the function at fault and "
            "the specific line that is wrong (give its line number in subject.py), "
            "and say in one sentence what the line does wrong. Do not propose a fix "
            "and do not rewrite the code.\n\n"
            f"SOURCE (subject.py, lines {first}-{block['end']} of the module, line "
            f"numbers shown):\n```\n{_numbered(shown, first)}```\n\n"
            f"TEST (test_focused.py):\n```python\n{fix['test_text']}```\n\n"
            f"PYTEST OUTPUT:\n```\n{fix['trace']}\n```\n\n{NO_TRACE}"
        ),
        "scorer": "diagnosis_location",
        "scorer_config": {
            "must_name": [fix["enclosing"]],
            "any_of": any_of,
            "must_not_name": [],
            "proof_subject_text": fix["pre"],
            "proof_test_text": fix["test_text"],
        },
    }


def defect_line_task(fix: dict[str, Any]) -> dict[str, Any] | None:
    """Review: which one line of this block does the merged fix change?"""
    block = fix["block"]
    if fix["kind"] != "fix" or not block["single_line"]:
        return None
    lines = block["block"].splitlines(keepends=True)
    if not 3 <= len(lines) <= 99:
        return None
    offset = block["removed"][0] - block["start"] + 1
    return {
        "title": f"find the defective line in {fix['enclosing'] or 'the block'}",
        "task_type": "review",
        "provenance": (
            f"A real merged fix, {fix['repo']} {fix['sha']}, to {fix['src']}: the fix "
            "changes exactly one line of this block. The held answer is that line's "
            "position in the block."
        ),
        "prompt": (
            "Review the code below. It has to satisfy the test shown, and exactly "
            "one line of it is wrong. Which line? Answer in the form `Line N`, "
            "where N is the number shown beside the line, and nothing else.\n\n"
            f"CODE (line numbers shown):\n```\n{_numbered(lines)}```\n\n"
            f"TEST (test_focused.py; the module is importable as `subject`):\n"
            f"```python\n{fix['test_text']}```\n\n{NO_TRACE}"
        ),
        "scorer": "exact_match",
        "scorer_config": {
            "accepted": [f"Line {offset}"],
            # `Line 1` must not credit `Line 12`; the digits after N are refused.
            "forbidden": [f"Line {offset}{d}" for d in range(10)],
        },
    }


# --------------------------------------------------------------------------
# Source 2: modules with an existing test suite, and edits those tests catch
# --------------------------------------------------------------------------


def _test_imports(repo: str, ref: str) -> dict[str, list[str]]:
    """Dotted module -> repository test files (paths) that import it."""
    listing = _git(repo, "grep", "-l", "-E", r"^(from|import) \w+", ref, "--", "tests")
    if listing is None:
        return {}
    mapping: dict[str, list[str]] = {}
    for entry in listing.splitlines():
        path = entry.split(":", 1)[1] if ":" in entry else entry
        if not path.endswith(".py"):
            continue
        text = _git(repo, "show", f"{ref}:{path}")
        if text is None or text.count("\n") > 200:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mapping.setdefault(node.module, []).append(path)
                for alias in node.names:
                    mapping.setdefault(f"{node.module}.{alias.name}", []).append(path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    mapping.setdefault(alias.name, []).append(path)
    return mapping


def _mutation_sites(source: str) -> list[tuple[int, int, int, int, str, str, str]]:
    """Single-token edits inside function bodies: (row, col, end_row, end_col,
    replacement, kind, enclosing function). ASCII lines only."""
    tree = ast.parse(source)
    lines = source.splitlines()
    sites: list[tuple[int, int, int, int, str, str, str]] = []
    swaps = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "=="}

    def ascii_line(row: int) -> bool:
        return lines[row - 1].isascii()

    def find(
        row: int, lo: int, end_row: int, hi: int, pattern: str
    ) -> re.Match[str] | None:
        if row != end_row:
            return None
        return re.search(pattern, lines[row - 1][lo:hi])

    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        inner = [n for stmt in func.body for n in ast.walk(stmt)]
        for node in inner:
            if isinstance(node, ast.Compare):
                left = node.left
                for op, comp in zip(node.ops, node.comparators, strict=False):
                    row, end_row = left.end_lineno or 0, comp.lineno
                    if not (row and ascii_line(row)):
                        left = comp
                        continue
                    m = find(
                        row,
                        left.end_col_offset or 0,
                        end_row,
                        comp.col_offset,
                        r"<=|>=|==|!=|<|>",
                    )
                    if m and isinstance(
                        op, ast.Lt | ast.LtE | ast.Gt | ast.GtE | ast.Eq | ast.NotEq
                    ):
                        base = left.end_col_offset or 0
                        sites.append(
                            (
                                row,
                                base + m.start(),
                                row,
                                base + m.end(),
                                swaps[m.group()],
                                "comparison",
                                func.name,
                            )
                        )
                    left = comp
            elif isinstance(node, ast.BoolOp):
                for a, b in zip(node.values, node.values[1:], strict=False):
                    row = a.end_lineno or 0
                    if not (row and ascii_line(row)):
                        continue
                    m = find(
                        row,
                        a.end_col_offset or 0,
                        b.lineno,
                        b.col_offset,
                        r"\band\b|\bor\b",
                    )
                    if m:
                        base = a.end_col_offset or 0
                        sites.append(
                            (
                                row,
                                base + m.start(),
                                row,
                                base + m.end(),
                                "or" if m.group() == "and" else "and",
                                "boolean operator",
                                func.name,
                            )
                        )
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub):
                row = node.left.end_lineno or 0
                if not (row and ascii_line(row)):
                    continue
                m = find(
                    row,
                    node.left.end_col_offset or 0,
                    node.right.lineno,
                    node.right.col_offset,
                    r"[+-]",
                )
                if m:
                    base = node.left.end_col_offset or 0
                    sites.append(
                        (
                            row,
                            base + m.start(),
                            row,
                            base + m.end(),
                            "-" if m.group() == "+" else "+",
                            "arithmetic operator",
                            func.name,
                        )
                    )
            elif isinstance(node, ast.Constant) and not isinstance(
                node.value, str | bytes
            ):
                row = node.lineno
                if not ascii_line(row) or node.end_lineno != row:
                    continue
                if isinstance(node.value, bool):
                    sites.append(
                        (
                            row,
                            node.col_offset,
                            row,
                            node.end_col_offset or 0,
                            str(not node.value),
                            "boolean literal",
                            func.name,
                        )
                    )
                elif isinstance(node.value, int):
                    sites.append(
                        (
                            row,
                            node.col_offset,
                            row,
                            node.end_col_offset or 0,
                            str(node.value + 1),
                            "numeric constant",
                            func.name,
                        )
                    )
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                row = node.lineno
                if ascii_line(row) and node.operand.lineno == row:
                    sites.append(
                        (
                            row,
                            node.col_offset,
                            row,
                            node.operand.col_offset,
                            "",
                            "negation",
                            func.name,
                        )
                    )
    return sites


def _apply_site(source: str, site: tuple[int, int, int, int, str, str, str]) -> str:
    row, col, _end_row, end_col, replacement, _kind, _func = site
    lines = source.splitlines(keepends=True)
    line = lines[row - 1]
    lines[row - 1] = line[:col] + replacement + line[end_col:]
    return "".join(lines)


def _tested_module(
    repo: str, ref: str, path: str, tests: list[str]
) -> dict[str, Any] | None:
    source = _git(repo, "show", f"{ref}:{path}")
    if source is None:
        return None
    count = source.count("\n")
    if not 25 <= count <= 150 or re.search(r"^from \.", source, re.MULTILINE):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if not any(isinstance(n, ast.FunctionDef) for n in tree.body):
        return None
    dotted = _module_name(path)
    for test_path in sorted(set(tests)):
        text = _git(repo, "show", f"{ref}:{test_path}")
        if text is None:
            continue
        rewritten = _rewrite_test(text, dotted)
        if "subject" not in rewritten:
            continue
        rc, out = _run_focused(source, rewritten)
        if rc == 0 and re.search(r"\d+ passed", out):
            return {
                "repo": repo,
                "ref": ref,
                "path": path,
                "dotted": dotted,
                "source": source,
                "test_path": test_path,
                "test_text": rewritten,
            }
    return None


def find_killable_mutant(
    module: dict[str, Any], names: set[str]
) -> dict[str, Any] | None:
    """The first deterministic single edit the module's own tests catch by assertion."""
    source = module["source"]
    sites = _mutation_sites(source)
    random.Random(f"{module['repo']}:{module['path']}").shuffle(sites)
    for site in sites[:14]:
        mutant = _apply_site(source, site)
        try:
            ast.parse(mutant)
        except SyntaxError:
            continue
        rc, out = _run_focused(mutant, module["test_text"])
        if rc in (0, 5, 124) or not re.search(r"\d+ failed", out):
            continue
        if not re.search(r"AssertionError|assert ", out) or re.search(
            r"ImportError|ModuleNotFoundError|SyntaxError", out
        ):
            continue
        if hygiene_problems(source + module["test_text"] + out, names):
            return None
        row = site[0]
        return {
            **module,
            "mutant": mutant,
            "mutant_row": row,
            "mutant_line": mutant.splitlines()[row - 1].strip(),
            "original_line": source.splitlines()[row - 1].strip(),
            "kind": site[5],
            "function": site[6],
            "trace": out,
        }
    return None


def mine_modules(workers: int = 10) -> list[dict[str, Any]]:
    import concurrent.futures

    names = _person_names()
    modules: list[dict[str, Any]] = []
    jobs: list[tuple[str, str, str, list[str]]] = []
    for repo in MINED_REPOS:
        ref = _pin(repo)
        if not ref:
            continue
        mapping = _test_imports(repo, ref)
        listing = _git(repo, "ls-tree", "-r", "--name-only", ref, "--", "src") or ""
        for path in sorted(listing.splitlines()):
            if path.endswith(".py") and not path.endswith("__init__.py"):
                tests = mapping.get(_module_name(path))
                if tests:
                    jobs.append((repo, ref, path, tests))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        tested = [m for m in pool.map(lambda j: _tested_module(*j), jobs) if m]
        mutated = list(pool.map(lambda m: find_killable_mutant(m, names), tested))
    by_path = {(m["repo"], m["path"]): m for m in tested}
    for m in mutated:
        if m:
            by_path[(m["repo"], m["path"])] = m
    modules = [by_path[k] for k in sorted(by_path)]
    return modules


def unit_test_task(module: dict[str, Any]) -> dict[str, Any]:
    func = module["function"]
    return {
        "title": f"write a unit test for {func}",
        "task_type": "test",
        "provenance": (
            f"{module['dotted']} at {module['repo']} {module['ref']}, with its own "
            f"repository tests. The mutant is one deliberate edit (a {module['kind']} "
            f"in {func}) that those existing tests are proven to catch by assertion."
        ),
        "prompt": (
            "Write a pytest test module for the code below.\n\n"
            "Rules the grader enforces:\n"
            "- The module under test is importable as `subject`. Import what you "
            "test with `from subject import <name>`.\n"
            "- Use plain pytest: test functions named `test_*`, plain `assert`. No "
            "fixtures, no classes, no mocking, no imports beyond `subject` and the "
            "standard library.\n"
            "- Your tests will be run twice: once against this module, where they "
            "must all pass, and once against a copy containing a single deliberate "
            f"defect in `{func}`, where at least one of them must fail. A test "
            "suite that passes both is worthless.\n"
            "- Output one fenced Python code block and nothing else.\n\n"
            f"```python\n{module['source']}```\n\n{NO_TRACE}"
        ),
        "scorer": "unit_test_execution",
        "scorer_config": {
            "subject_text": module["source"],
            "mutant_text": module["mutant"],
            "timeout_s": 90,
        },
    }


def reasoning_task_from_mutant(module: dict[str, Any]) -> dict[str, Any] | None:
    row = module["mutant_row"]
    mutant_lines = module["mutant"].splitlines(keepends=True)
    line = module["mutant_line"]
    if len(line) < 6:
        return None
    return {
        "title": f"diagnose the failing test for {module['function']}",
        "task_type": "reasoning",
        "provenance": (
            f"{module['dotted']} at {module['repo']} {module['ref']} with its own "
            "repository tests. The trace is genuine pytest output from running those "
            "tests against a copy with one deliberate edit. The held cause is the "
            "edited line."
        ),
        "prompt": (
            "This test suite fails. Name the root cause: the function at fault and "
            "the specific line that is wrong (give its line number in subject.py), "
            "and say in one sentence what the line does wrong. Do not propose a fix "
            "and do not rewrite the code.\n\n"
            f"SOURCE (subject.py, line numbers shown):\n```\n"
            f"{_numbered(mutant_lines)}```\n\n"
            f"TEST (test_focused.py):\n```python\n{module['test_text']}```\n\n"
            f"PYTEST OUTPUT:\n```\n{module['trace']}\n```\n\n{NO_TRACE}"
        ),
        "scorer": "diagnosis_location",
        "scorer_config": {
            "must_name": [module["function"]],
            "any_of": [line, f"line {row}"],
            "must_not_name": [],
            "proof_subject_text": module["mutant"],
            "proof_test_text": module["test_text"],
        },
    }


def _literal(node: ast.expr) -> tuple[bool, object]:
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None


def result_question_task(
    module: dict[str, Any], exclude: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """Review: what does a call return? Held answer: what the repo's test asserts.

    Taken from a test assertion of the form ``assert f(<literals>) == <literal>``.
    """
    try:
        test_tree = ast.parse(module["test_text"])
    except SyntaxError:
        return None
    defined = {
        n.name
        for n in ast.parse(module["source"]).body
        if isinstance(n, ast.FunctionDef)
    }
    rng = random.Random(f"{module['repo']}:{module['path']}:qa")
    options: list[tuple[str, str]] = []
    for node in ast.walk(test_tree):
        if not (isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare)):
            continue
        cmp_ = node.test
        if len(cmp_.ops) != 1 or not isinstance(cmp_.ops[0], ast.Eq):
            continue
        call, expected = cmp_.left, cmp_.comparators[0]
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            continue
        if call.func.id not in defined or call.keywords:
            continue
        parsed = [_literal(a) for a in call.args]
        want_ok, want = _literal(expected)
        if not want_ok or not all(ok for ok, _ in parsed):
            continue
        # The held answer is the literal the repository's own test asserts. That
        # test passed against exactly this source when the module was mined
        # (`_tested_module`), so the answer is confirmed by that execution; the
        # miner never imports or executes the module itself.
        got = want
        shown = repr(got)
        if len(shown) > 80 or shown in {"None"}:
            continue
        call_text = ast.unparse(call)
        if call_text in exclude:
            continue
        options.append((call_text, shown))
    # A call whose answer is `True`/`False`/small ints is the weakest question; keep
    # those only when nothing richer exists.
    rich = [o for o in options if o[1] not in {"True", "False", "0", "1"}]
    pool = rich or options
    if not pool:
        return None
    call_text, shown = rng.choice(pool)
    forbidden = (
        [
            f"RESULT: {shown}{c}"
            for c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
        ]
        if shown[-1].isalnum()
        else []
    )
    return {
        "_call": call_text,
        "title": f"what {call_text.split('(')[0]} returns for a given call",
        "task_type": "review",
        "provenance": (
            f"{module['dotted']} at {module['repo']} {module['ref']}. The question is "
            "taken from an assertion in the module's own repository test; the held "
            "answer holds because that test passes against this source."
        ),
        "prompt": (
            "Read this module and answer the question about it.\n\n"
            f"```python\n{module['source']}```\n\n"
            f"QUESTION: What exactly does `{call_text}` return? Answer as "
            "`RESULT: ` followed by the value exactly as Python's repr prints it, "
            f"and nothing else.\n\n{NO_TRACE}"
        ),
        "scorer": "exact_match",
        "scorer_config": {"accepted": [f"RESULT: {shown}"], "forbidden": forbidden},
    }


# --------------------------------------------------------------------------
# Source 3: merged-change descriptions
# --------------------------------------------------------------------------

_TRAILER_RE = re.compile(
    r"^(co-authored-by|signed-off-by|generated with|🤖|reviewed-by)", re.IGNORECASE
)
_TICKET_RE = re.compile(r"\bOMN-\d+\b")
_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def _merged_changes(repo: str, ref: str, limit: int) -> list[dict[str, Any]]:
    raw = _git(
        repo,
        "log",
        ref,
        "--first-parent",
        "-n",
        str(limit),
        "--format=%x1e%H%x1f%s%x1f%b",
    )
    if raw is None:
        return []
    changes: list[dict[str, Any]] = []
    for entry in raw.split("\x1e"):
        if not entry.strip():
            continue
        sha, subject, body = [*entry.split("\x1f"), "", ""][:3]
        pr = _PR_RE.search(subject)
        body_lines = [
            ln for ln in body.strip().splitlines() if not _TRAILER_RE.match(ln.strip())
        ]
        changes.append(
            {
                "repo": repo,
                "sha": sha.strip(),
                "subject": _PR_RE.sub("", subject).strip(),
                "pr": f"{repo}#{pr.group(1)}" if pr else None,
                "body": "\n".join(body_lines).strip(),
            }
        )
    return changes


_REWRITES: tuple[tuple[str, int, str, bool], ...] = (
    (
        "Rewrite this merged change's description as one release-note sentence for a "
        "reader who has not seen the code.",
        45,
        "sentence",
        True,
    ),
    (
        "Rewrite this merged change's description as a commit-message subject line: "
        "imperative mood, no trailing period.",
        16,
        "subject line",
        True,
    ),
    (
        "Rewrite this merged change's description as two short bullet points: what "
        "changed, and what it affects.",
        55,
        "two bullets",
        False,
    ),
    (
        "Rewrite this merged change's description as a one-sentence summary a "
        "reviewer can read in five seconds.",
        35,
        "reviewer sentence",
        True,
    ),
)


def rewrite_task(
    change: dict[str, Any], variant: int, names: set[str]
) -> dict[str, Any] | None:
    ticket = _TICKET_RE.search(change["subject"]) or _TICKET_RE.search(change["body"])
    symbols = [
        s
        for s in re.findall(r"`([A-Za-z_][\w.]{3,40})`", change["body"])
        if "_" in s or any(c.isupper() for c in s)
    ]
    if not ticket or not symbols or not 200 <= len(change["body"]) <= 2400:
        return None
    instruction, max_words, label, forbid_copy = _REWRITES[variant % len(_REWRITES)]
    symbol = symbols[0]
    description = f"{change['subject']}\n\n{change['body']}"
    if hygiene_problems(description, names):
        return None
    prompt = (
        f"{instruction} Keep the ticket identifier {ticket.group()} and the name "
        f"`{symbol}` exactly as written. Add nothing the description does not "
        f"state. At most {max_words} words.\n\n"
        f"CHANGE ({change['pr'] or change['repo']}):\n{description}\n\n{NO_TRACE}"
    )
    copy = re.sub(r"^\w+(\([^)]*\))?:\s*", "", change["subject"]).lower()
    return {
        "title": f"rewrite a merged change as a {label}",
        "task_type": "document",
        "provenance": (
            f"The squash-commit description of a real merged change, "
            f"{change['repo']} {change['sha']}."
        ),
        "prompt": prompt,
        "scorer": "grounding_rubric",
        "scorer_config": {
            "source_text": prompt,
            "must_contain": [ticket.group(), symbol],
            "must_not_contain": [copy] if forbid_copy and len(copy) >= 25 else [],
            "max_words": max_words,
        },
    }


def summary_task(
    window: list[dict[str, Any]], names: set[str]
) -> dict[str, Any] | None:
    entries = []
    for change in window:
        first = next((ln for ln in change["body"].splitlines() if ln.strip()), "")
        ref = change["pr"] or change["repo"]
        entries.append(
            f"{change['sha'][:7]} {ref}: {change['subject']}\n    {first[:260]}"
        )
    text = "\n".join(entries)
    if hygiene_problems(text, names):
        return None
    counts: dict[str, int] = {}
    for token in _TICKET_RE.findall(text):
        counts[token] = counts.get(token, 0) + 1
    required = sorted(counts)[:10]
    if len(required) < 4:
        return None
    prompt = (
        "Summarise what these merged changes did. One bullet per distinct piece of "
        "work, at most eight bullets. Cite the ticket identifier for every bullet. "
        "Use only identifiers that appear in the list below; if you are unsure of "
        "an identifier, leave it out rather than guessing at one.\n\n"
        f"MERGED CHANGES:\n{text}\n\n{NO_TRACE}"
    )
    first, last = window[0], window[-1]
    return {
        "title": f"what landed, from {len(window)} merged changes",
        "task_type": "document",
        "provenance": (
            f"{len(window)} consecutive real merges to {first['repo']}, from "
            f"{last['sha'][:12]} to {first['sha'][:12]}, subject lines and the first "
            "line of each description."
        ),
        "prompt": prompt,
        "scorer": "grounding_coverage",
        "scorer_config": {
            "source_text": prompt,
            "required_ids": required,
            "coverage_threshold": 0.5,
        },
    }


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

# (rung, task_type, first new number, how many). The hand-built rungs hold 01-04.
MINED_PLAN: dict[str, tuple[str, int]] = {
    "R1": ("document", 12),
    "R2": ("document", 10),
    "R3": ("review", 22),
    "R4": ("test", 22),
    "R5": ("reasoning", 22),
    "R6": ("code_generation", 22),
}


def _task_text(task: dict[str, Any]) -> str:
    """Every string a bundle publishes, as written (not as JSON escapes it)."""
    config = task.get("scorer_config", {})
    strings = [str(task.get(k, "")) for k in ("title", "provenance", "prompt")]
    for value in config.values():
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            strings.extend(str(v) for v in value)
    return "\n".join(strings)


def _order_key(*parts: str) -> str:
    """A fixed, repository-mixing order: the hash of the source's own name."""
    import hashlib

    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def _echo_passes(task: dict[str, Any]) -> bool:
    """True when the task's own fed trace would pass its diagnosis scorer."""
    from benchmarks.delegation_ladder.scorers import score_diagnosis_location

    prompt = str(task["prompt"])
    trace = prompt[prompt.index("PYTEST OUTPUT:") :]
    verdict = score_diagnosis_location(trace, dict(task["scorer_config"]))
    return verdict.outcome.value == "pass"


def assemble_mined(
    limit: int = 6000,
    fixes: list[dict[str, Any]] | None = None,
    modules: list[dict[str, Any]] | None = None,
    screened_out: frozenset[str] = frozenset(),
) -> dict[str, list[dict[str, Any]]]:
    """Mine every source and allocate tasks to rungs without reusing a source.

    ``screened_out`` names sources (``repo:sha`` or ``repo:path``) the
    public-repo hygiene gate flagged on an earlier build; they are skipped.

    ``fixes`` and ``modules`` let a caller hand in sources it already mined, so a
    change to the allocation does not repeat the ten-minute pytest sweep.
    """
    names = _person_names()
    fixes = mine_fixes(limit) if fixes is None else fixes
    modules = mine_modules() if modules is None else modules
    mutated = [m for m in modules if "mutant" in m]

    out: dict[str, list[dict[str, Any]]] = {r: [] for r in MINED_PLAN}
    used_fix: set[str] = set()
    used_module: set[tuple[str, str]] = set()

    def take(rung: str, task: dict[str, Any] | None, source: str) -> bool:
        if task is None or len(out[rung]) >= MINED_PLAN[rung][1]:
            return False
        if source in screened_out or hygiene_problems(_task_text(task), names):
            return False
        task["_source"] = source
        out[rung].append(task)
        return True

    # The scarce sources are allocated first. A fix that changes exactly one line
    # is the only kind a defect-line question can use; a fix that removes lines is
    # the only kind a diagnosis can use; every merged change can feed a patch task.
    fixes.sort(key=lambda f: _order_key(f["repo"], f["sha"]))
    defect_cap, diagnosis_cap = 8, 14
    for fix in fixes:
        if (
            defect_cap
            and fix["sha"] not in used_fix
            and take("R3", defect_line_task(fix), f"{fix['repo']}:{fix['sha']}")
        ):
            used_fix.add(fix["sha"])
            defect_cap -= 1
    diagnosed = 0
    for fix in fixes:
        if diagnosed < diagnosis_cap and fix["sha"] not in used_fix:
            task = reasoning_task_from_fix(fix)
            if (
                task
                and not _echo_passes(task)
                and take("R5", task, f"{fix['repo']}:{fix['sha']}")
            ):
                used_fix.add(fix["sha"])
                diagnosed += 1
    for fix in fixes:
        if fix["sha"] not in used_fix and take(
            "R6", codegen_task(fix), f"{fix['repo']}:{fix['sha']}"
        ):
            used_fix.add(fix["sha"])

    mutated.sort(key=lambda m: _order_key(m["repo"], m["path"]))
    modules.sort(key=lambda m: _order_key(m["repo"], m["path"]))
    for module in mutated:
        if take("R4", unit_test_task(module), f"{module['repo']}:{module['path']}"):
            used_module.add((module["repo"], module["path"]))
    for module in mutated:
        if (module["repo"], module["path"]) in used_module:
            continue
        task = reasoning_task_from_mutant(module)
        if (
            task
            and not _echo_passes(task)
            and take("R5", task, f"{module['repo']}:{module['path']}")
        ):
            used_module.add((module["repo"], module["path"]))
    asked: dict[tuple[str, str], set[str]] = {}
    for _round in range(3):
        for module in modules:
            key = (module["repo"], module["path"])
            if key in used_module:
                continue
            task = result_question_task(module, frozenset(asked.get(key, set())))
            if task is None:
                continue
            question = str(task.pop("_call"))
            if take("R3", task, f"{module['repo']}:{module['path']}"):
                asked.setdefault(key, set()).add(question)

    changes: list[dict[str, Any]] = []
    for repo in MINED_REPOS:
        ref = _pin(repo)
        if ref:
            changes += _merged_changes(repo, ref, 400)
    variant = 0
    for change in sorted(changes, key=lambda c: _order_key(c["repo"], c["sha"])):
        if len(out["R1"]) >= MINED_PLAN["R1"][1]:
            break
        task = rewrite_task(change, variant, names)
        if task and take("R1", task, f"{change['repo']}:{change['sha']}"):
            variant += 1
    windows: list[tuple[str, dict[str, Any]]] = []
    for repo in MINED_REPOS:
        repo_changes = [c for c in changes if c["repo"] == repo and c["pr"]]
        for start in range(0, max(len(repo_changes) - 12, 0), 37):
            size = 8 + (start // 37 % 5)
            task = summary_task(repo_changes[start : start + size], names)
            if task:
                window_source = f"{repo}:window:{repo_changes[start]['sha']}"
                windows.append((window_source, task))
    for source, task in sorted(windows, key=lambda w: _order_key(w[0])):
        take("R2", task, source)
    return out


def write_mined(
    tasks: dict[str, list[dict[str, Any]]],
    screened_out: frozenset[str] = frozenset(),
) -> int:
    for stale in BUNDLES.glob("R*-*.json"):
        if int(stale.stem.rsplit("-", 1)[1]) >= 5:
            stale.unlink()
    written = 0
    for rung, group in tasks.items():
        for index, task in enumerate(group, start=5):
            task["task_id"] = f"{rung}-{index:02d}"
            task["rung"] = rung
            task["dependent_reasoning_steps"] = DECLARED_STEPS[rung]
            task.setdefault("fixture_files", {})
            ordered = {
                k: task[k]
                for k in (
                    "task_id",
                    "title",
                    "task_type",
                    "provenance",
                    "prompt",
                    "scorer",
                    "scorer_config",
                    "fixture_files",
                    "rung",
                    "dependent_reasoning_steps",
                )
            }
            (BUNDLES / f"{task['task_id']}.json").write_text(
                json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            written += 1
    pins = {repo: _pin(repo) for repo in MINED_REPOS}
    sources = {
        task["task_id"]: task["_source"] for group in tasks.values() for task in group
    }
    MINED_SOURCES.write_text(
        json.dumps(
            {
                "pinned_refs": pins,
                "screened_out": sorted(screened_out),
                "task_sources": dict(sorted(sources.items())),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return written


# --------------------------------------------------------------------------
# The structural hard corpus (OMN-20844): multi-file fixes, a cause in another
# file than the one the failing test imports, and inputs padded toward the
# local rung's context limit. Each task is a merged omnimarket commit whose own
# test passes at the merge and fails with the commit's source files put back to
# the parent; the scorer re-runs exactly that, so nothing is judged by eye.
# --------------------------------------------------------------------------

HARD_BUNDLES = HERE / "bundles_hard"

# (merged commit, the test that commit added or changed). Picked from the
# first-parent history of omnimarket dev at 29555c1082f2a8f6d76bd18ac4a73c77bf48a20b
# for a spread of files touched and of where the cause sits.
HARD_COMMITS: tuple[tuple[str, str], ...] = (
    (
        "272fde1064b71d207b57c2a206febe14637cdd68",
        "tests/nodes/node_delegation_availability_report_compute/test_golden_chain_delegation_availability_report_compute.py",
    ),
    (
        "56d792624915857a39365ee58508ca1c1c367859",
        "tests/unit/nodes/node_pr_lifecycle_orchestrator/test_ci_red_triage.py",
    ),
    (
        "7edc88f40c542f3cd06a0302cf04e15aa378c3ca",
        "tests/unit/test_occ_contention_omn_15247.py",
    ),
    (
        "611dedc42eb9740873845c582f7ef6407dd95a73",
        "tests/nodes/node_merge_sweep_orchestrator/test_error_chain_merge_sweep_orchestrator.py",
    ),
    (
        "7036d5794b81fb36c11a671c15a07526d1794b07",
        "tests/nodes/node_delegation_output_materialize_effect/test_materialize_refusals.py",
    ),
    (
        "bf08d061a1c03327f4b4d85868e88add17806250",
        "tests/nodes/node_delegation_eval_sample_compute/test_delegation_eval_sample.py",
    ),
    (
        "4f2cd57b33ae9ecc707361fb04f729523872b347",
        "tests/unit/nodes/node_projection_pr_state/test_pr_state_schema_versions.py",
    ),
    (
        "cdba07dc7337ac7fc347f1e84045a69e4f75e4ff",
        "tests/unit/delegation/test_unbound_local_model_probe_omn17427.py",
    ),
    (
        "15afc64f9df1d957b57b3a3e10fefa905ee757a5",
        "tests/test_omn17372_byok_bus_transport_auth.py",
    ),
    (
        "a0f4654f2ab8d5e523c8acac1b80530ce863fc41",
        "tests/unit/nodes/node_delegate_skill_orchestrator/test_claim_store_contract_schema_omn19661.py",
    ),
)

# Prompt-size targets in characters. Source code runs near 3.3 characters per
# token, so "medium" sits near a 32768-token slot and "large" near the 65536
# tokens the local-coder rung declares (routing_tiers.yaml max_context_tokens).
HARD_SIZE_TARGETS: dict[str, int] = {"small": 0, "medium": 95_000, "large": 190_000}
# The size tier rotates independently of the commit order, so size is not
# confounded with files touched or cross-file cause; the two classes rotate
# with different offsets for the same reason.
HARD_SIZE_ROTATION: tuple[str, ...] = ("small", "medium", "large")
HARD_CLASS_OFFSET: dict[str, int] = {"code_generation": 0, "review": 1}

_HARD_FAILURE_TAIL_LINES = 45


def _hard_changed_files(commit: str) -> list[str]:
    out = git_text(
        "diff-tree", "--no-commit-id", "--name-only", "-r", commit, "--", "src"
    )
    return [line for line in out.splitlines() if line.endswith(".py")]


def _hard_test_imports(commit: str, test_path: str) -> list[str]:
    """Source files the failing test imports directly, as repository paths."""
    text = git_text("show", f"{commit}:{test_path}")
    modules = sorted(
        set(re.findall(r"^\s*from (omnimarket[\w.]*) import", text, re.MULTILINE))
        | set(re.findall(r"^\s*import (omnimarket[\w.]*)", text, re.MULTILINE))
    )
    paths: list[str] = []
    for module in modules:
        base = "src/" + module.replace(".", "/")
        for candidate in (base + ".py", base + "/__init__.py"):
            if git_text("ls-tree", "--name-only", commit, candidate).strip():
                paths.append(candidate)
                break
    return paths


def _hard_required_files(commit: str, test_path: str, changed: list[str]) -> list[str]:
    """The changed files whose parent version alone makes the test fail."""
    required: list[str] = []
    for path in changed:
        with tempfile.TemporaryDirectory(prefix="hard-req-") as tmp:
            tree = Path(tmp)
            materialise_tree(commit, tree, [path])
            rc, _ = run_tree_test(tree, test_path, 180)
        if rc != 0:
            required.append(path)
    return required


def _hard_padding(commit: str, shown: list[str], need: int) -> list[str]:
    """Real sibling source files at the parent, nearest directories first."""
    picked: list[str] = []
    if need <= 0:
        return picked
    seen = set(shown)
    dirs: list[str] = []
    for path in shown:
        parts = path.split("/")[:-1]
        while len(parts) > 2:
            joined = "/".join(parts)
            if joined not in dirs:
                dirs.append(joined)
            parts = parts[:-1]
    dirs.sort(key=lambda d: -d.count("/"))
    total = 0
    for directory in dirs:
        listing = git_text(
            "ls-tree", "-r", "--name-only", f"{commit}^", directory + "/"
        )
        for path in sorted(listing.splitlines()):
            if path in seen or not path.endswith(".py") or path.endswith("__init__.py"):
                continue
            size = len(git_text("show", f"{commit}^:{path}"))
            if size < 400 or total + size > need * 1.06:
                continue
            picked.append(path)
            seen.add(path)
            total += size
            if total >= need:
                return picked
    return picked


def _hard_prompt(
    task_type: str,
    test_path: str,
    test_text: str,
    failure: str,
    files: dict[str, str],
) -> str:
    sources = "\n".join(
        f"### {path}\n```python\n{text.rstrip()}\n```\n" for path, text in files.items()
    )
    head = (
        f"FAILING TEST ({test_path}):\n```python\n{test_text.rstrip()}\n```\n\n"
        f"CURRENT FAILURE:\n```\n{failure.rstrip()}\n```\n\n"
        f"SOURCE FILES ({len(files)}, each shown in full as it is now):\n\n{sources}"
    )
    if task_type == "code_generation":
        return (
            "A test in this Python repository fails. Fix the source so that it passes.\n\n"
            "Rules the grader enforces:\n"
            "- Change only the source files shown under SOURCE FILES. The test is fixed.\n"
            "- Write each edit as a line `FILE: <path>`, then a search block and its "
            "replacement, exactly in this form:\n"
            "FILE: src/omnimarket/example.py\n"
            "<<<<<<< SEARCH\n<lines copied exactly from the file as shown>\n=======\n"
            "<the lines that replace them>\n>>>>>>> REPLACE\n"
            "- Each SEARCH text must appear exactly once in its file, whitespace included.\n"
            "- Use as many edits, in as many files, as the fix needs.\n"
            "- Your edits are applied to the files as shown and the failing test file is "
            "run in full; every test in it must pass.\n\n"
            f"{head}\n"
            "Respond with the edits only. Do not show your reasoning, do not narrate your "
            "steps, and do not preface the answer with anything."
        )
    return (
        "A test in this Python repository fails. Review the source and decide where "
        "the defect is.\n\n"
        f"{head}\n"
        "QUESTION: Which of the source files shown must be changed for the failing test "
        "to pass? Answer with one line per file, `FILE: <path>` with the path exactly as "
        "shown, naming every file that must change and no other.\n\n"
        "Respond with the answer only. Do not show your reasoning, do not narrate your "
        "steps, and do not preface the answer with anything."
    )


def build_hard_corpus() -> list[ModelTaskBundle]:
    bundles: list[ModelTaskBundle] = []
    for index, (commit, test_path) in enumerate(HARD_COMMITS):
        changed = _hard_changed_files(commit)
        imported = _hard_test_imports(commit, test_path)
        required = _hard_required_files(commit, test_path, changed)
        cross_file = not (set(changed) & set(imported))
        with tempfile.TemporaryDirectory(prefix="hard-build-") as tmp:
            tree = Path(tmp)
            materialise_tree(commit, tree, changed)
            rc, failure = run_tree_test(tree, test_path, 180)
        if rc == 0:
            raise RuntimeError(f"{commit}: {test_path} passes on the parent source")
        failure_tail = "\n".join(failure.splitlines()[-_HARD_FAILURE_TAIL_LINES:])
        test_text = git_text("show", f"{commit}:{test_path}")
        core = sorted(set(changed) | set(imported))
        subject = git_text("log", "-1", "--format=%s", commit).strip()
        for task_type, prefix in (("code_generation", "H6"), ("review", "H3")):
            size_tier = HARD_SIZE_ROTATION[
                (index + HARD_CLASS_OFFSET[task_type]) % len(HARD_SIZE_ROTATION)
            ]
            files = {p: git_text("show", f"{commit}^:{p}") for p in core}
            base_len = len(
                _hard_prompt(task_type, test_path, test_text, failure_tail, files)
            )
            padding = _hard_padding(
                commit, core, HARD_SIZE_TARGETS[size_tier] - base_len
            )
            shown = sorted(core + padding)
            files = {p: git_text("show", f"{commit}^:{p}") for p in shown}
            prompt = _hard_prompt(task_type, test_path, test_text, failure_tail, files)
            features: dict[str, object] = {
                "files_touched": len(changed),
                "files_required": len(required),
                "cross_file_cause": cross_file,
                "files_shown": len(shown),
                "context_chars": len(prompt),
                "size_tier": size_tier,
            }
            provenance = (
                f"omnimarket merged commit {commit} ({subject}), changing "
                f"{len(changed)} source file(s). Its own test {test_path} passes at "
                "the commit and fails with those files at the parent; the files are "
                "shown at the parent."
            )
            config: dict[str, object] = {"features": features}
            if task_type == "code_generation":
                scorer = EnumScorerKind.MULTI_FILE_EDIT_AND_TEST
                config |= {
                    "repo_commit": commit,
                    "changed_files": changed,
                    "editable_files": shown,
                    "test_path": test_path,
                    "timeout_s": 240,
                }
                title = f"fix a failing test across {len(shown)} shown files"
            else:
                scorer = EnumScorerKind.EXACT_MATCH
                config |= {
                    "accepted": [f"FILE: {p}" for p in required],
                    "forbidden": [f"FILE: {p}" for p in shown if p not in changed],
                }
                title = f"name the files a failing test needs changed, of {len(shown)}"
            bundles.append(
                ModelTaskBundle(
                    task_id=f"{prefix}-{index + 1:02d}",
                    rung=EnumRung.R6_PRODUCE_PATCH
                    if task_type == "code_generation"
                    else EnumRung.R3_CODE_READING_QA,
                    title=title,
                    task_type=task_type,
                    provenance=provenance,
                    prompt=prompt,
                    scorer=scorer,
                    scorer_config=config,
                    dependent_reasoning_steps=len(changed) + int(cross_file) + 1,
                )
            )
    return bundles


def write_hard_corpus() -> int:
    HARD_BUNDLES.mkdir(exist_ok=True)
    for bundle in build_hard_corpus():
        (HARD_BUNDLES / f"{bundle.task_id}.json").write_text(
            bundle.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        print(bundle.task_id, bundle.scorer_config["features"])
    return 0


def main() -> None:
    BUNDLES.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:2] == ["hard"]:
        write_hard_corpus()
        return
    if sys.argv[1:2] == ["mine"]:
        screened = frozenset(
            json.loads(MINED_SOURCES.read_text(encoding="utf-8")).get(
                "screened_out", []
            )
            if MINED_SOURCES.is_file()
            else []
        )
        mined = assemble_mined(screened_out=screened)
        for rung, group in mined.items():
            print(f"{rung}: {len(group)} of {MINED_PLAN[rung][1]} mined")
        print(f"\n{write_mined(mined, screened)} mined bundles written to {BUNDLES}")
        return
    groups = {
        "R1": r1(),
        "R2": r2(),
        "R3": r3(),
        "R3b": r3b(),
        "R4": r4(),
        "R5": r5(),
        "R6": r6(),
    }
    written = 0
    for rung, tasks in groups.items():
        for task in tasks:
            task["rung"] = rung
            task["dependent_reasoning_steps"] = DECLARED_STEPS[rung]
            task.setdefault("fixture_files", {})
            path = BUNDLES / f"{task['task_id']}.json"
            path.write_text(
                json.dumps(task, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            written += 1
            print(f"{task['task_id']:<7} {len(str(task['prompt'])):>7} prompt chars")
    print(f"\n{written} bundles written to {BUNDLES}")


if __name__ == "__main__":
    main()
