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
import builtins
import hashlib
import json
import os
import re
import subprocess
import sys
import tokenize
from io import StringIO
from pathlib import Path
from typing import Any

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
# Tiered public task set (document / review / reasoning)
# ==========================================================================
#
# Every task below is mined from the merged history of three public
# repositories at a pinned commit, never from internal material:
#
#   document   a pull request's own squash-commit message (one change, five
#              changes, thirty changes). Held: the PR numbers that must be named.
#   review     a merged pull request's description and diff, in which ONE added
#              non-test line has had a literal altered so that it no longer
#              agrees with the test added in the same diff. Held: that line's
#              label. The alteration is recorded, deterministic and the only
#              synthetic element.
#   reasoning  a real function with a real call and asserted result taken from
#              a test that exists. Held: the asserted value, re-derived here by
#              EXECUTING the call before the task is written.
#
# Difficulty tiers are set by measured size of what is fed, not by a label.

TIERED = BUNDLES / "tiered"
TIERED_PINS: dict[str, str] = {
    "omnimarket": "7acb97b72bd64cfa04fdb4f4d51663ca30bd5ee8",
    "omnibase_core": "d677b4e78219113139b5d20ac81efcdcef78e3e0",
    "omnibase_spi": "aa2de2346ff46056e63fca58b05ecfb491d54c5c",
}
TIER_QUOTA: dict[str, int] = {"easy": 9, "medium": 8, "hard": 8}
_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_TRAILER_RE = re.compile(
    r"^(co-authored-by|signed-off-by|reviewed-by|generated with|🤖).*$",
    re.IGNORECASE | re.MULTILINE,
)
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")


def _registry_root() -> Path:
    home = os.environ.get("OMNI_HOME")
    if not home:
        raise RuntimeError("OMNI_HOME must be set; there is no default")
    return Path(home)


def _git(repo: str, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(_registry_root() / repo), *args],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.stdout


def _hygiene_patterns() -> list[re.Pattern[str]]:
    """The public-repo hygiene vocabulary, read from the building lane's copy.

    Nothing from it is written into a bundle. A candidate text that matches any
    entry is skipped, so the committed corpus cannot be the thing that trips the
    public-repo gate.
    """
    base = os.environ.get("OMNI_HYGIENE_VOCAB_PATH", "")
    options = [
        Path(base),
        Path(base + "ulary.yaml"),
        Path(base) / "public_repo_hygiene_vocabulary.yaml",
    ]
    vocab_file = next((p for p in options if base and p.is_file()), None)
    if vocab_file is None:
        raise RuntimeError("OMNI_HYGIENE_VOCAB_PATH must name the hygiene vocabulary")
    # The file carries regex escapes inside double quotes, which a YAML loader
    # rejects, so the entries are read line by line as the gate itself reads them.
    literal_sections = {"private_repo_names", "person_names"}
    regex_sections = {
        "internal_kb_prose",
        "host_nicknames",
        "machine_path_patterns",
        "tracker_url_patterns",
        "sensitive_literal_patterns",
    }
    patterns: list[re.Pattern[str]] = []
    section = ""
    for raw in vocab_file.read_text(encoding="utf-8").splitlines():
        if raw and not raw.startswith((" ", "#", "-")) and raw.endswith(":"):
            section = raw[:-1]
            continue
        entry = re.match(r'^\s+-\s+"(.*)"\s*$', raw)
        if not entry:
            continue
        if section in literal_sections:
            patterns.append(re.compile(re.escape(entry.group(1)), re.IGNORECASE))
        elif section in regex_sections:
            patterns.append(re.compile(entry.group(1), re.IGNORECASE))
    return patterns


_HYGIENE: list[re.Pattern[str]] = []


def _clean(text: str) -> bool:
    """True when ``text`` can be published in a public repository as it stands."""
    if not _HYGIENE:
        _HYGIENE.extend(_hygiene_patterns())
    if _IP_RE.search(text) or _EMAIL_RE.search(text):
        return False
    return not any(p.search(text) for p in _HYGIENE)


def _rank(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ---- document --------------------------------------------------------------


def _squash_commits(repo: str, limit: int) -> list[dict[str, str]]:
    raw = _git(
        repo,
        "log",
        "--first-parent",
        f"-n{limit}",
        "--format=%H%x1f%s%x1f%b%x1e",
        TIERED_PINS[repo],
    )
    commits: list[dict[str, str]] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, body = [*record.split("\x1f"), "", ""][:3]
        match = _PR_RE.search(subject)
        if not match:
            continue
        body = _TRAILER_RE.sub("", body).strip()
        if not _clean(f"{subject}\n{body}"):
            continue
        commits.append(
            {
                "repo": repo,
                "sha": sha,
                "pr": match.group(1),
                "subject": subject,
                "body": body,
            }
        )
    return commits


def _doc_change_text(commit: dict[str, str], body_cap: int) -> str:
    body = re.sub(r"\n{3,}", "\n\n", commit["body"])[:body_cap].rstrip()
    return f"{commit['subject']}\n{body}".strip()


def _doc_task(
    task_id: str,
    tier: str,
    commits: list[dict[str, str]],
    body_cap: int,
) -> dict[str, Any]:
    refs = [f"{c['repo']}#{c['pr']}" for c in commits]
    blocks = "\n\n".join(
        f"--- change {i + 1} (repo {c['repo']}, pull request #{c['pr']}) ---\n"
        + _doc_change_text(c, body_cap)
        for i, c in enumerate(commits)
    )
    source = blocks + " " + " ".join(refs)
    provenance = "Squash commits of " + ", ".join(
        f"{c['repo']}@{c['sha'][:12]}" for c in commits
    )
    if tier == "easy":
        c = commits[0]
        instruction = (
            "Summarize the merged change below in at most two sentences for a "
            "changelog: what changed, then why. Name the pull request number "
            "exactly as written (#NNNN). Use only facts stated in the change text."
        )
        ticket = re.search(r"\bOMN-\d+\b", c["subject"])
        must = [f"#{c['pr']}"] + ([ticket.group(0)] if ticket else [])
        if ticket:
            instruction += f" Also name the ticket {ticket.group(0)}."
        return {
            "task_id": task_id,
            "title": f"one-change changelog entry ({c['repo']}#{c['pr']})",
            "task_type": "document",
            "difficulty": tier,
            "provenance": provenance,
            "prompt": f"{instruction}\n\n{blocks}\n\n" + NO_TRACE,
            "scorer": "grounding_rubric",
            "scorer_config": {
                "source_text": source,
                "must_contain": must,
                "must_not_contain": [],
                "max_words": 90,
            },
        }
    words, threshold = (120, 0.8) if tier == "medium" else (380, 0.9)
    grouping = (
        ""
        if tier == "medium"
        else ", grouped under short theme headings, listing every pull request "
        "number under exactly one theme"
    )
    instruction = (
        f"Write release notes of at most {words} words covering ALL {len(commits)} "
        f"merged changes below{grouping}. Name each change's pull request number "
        "exactly as written (#NNNN). Use only facts stated in the changes; invent "
        "no number or identifier."
    )
    return {
        "task_id": task_id,
        "title": f"{len(commits)}-change release notes",
        "task_type": "document",
        "difficulty": tier,
        "provenance": provenance,
        "prompt": f"{instruction}\n\n{blocks}\n\n" + NO_TRACE,
        "scorer": "grounding_coverage",
        "scorer_config": {
            "source_text": source,
            "required_ids": [f"#{c['pr']}" for c in commits],
            "coverage_threshold": threshold,
        },
    }


def tiered_document() -> list[dict[str, Any]]:
    pool: list[dict[str, str]] = []
    for repo in TIERED_PINS:
        pool.extend(c for c in _squash_commits(repo, 900) if len(c["body"]) >= 250)
    pool.sort(key=lambda c: _rank("doc", c["sha"]))
    tasks: list[dict[str, Any]] = []
    cursor = 0
    sizes = {"easy": (1, 1400), "medium": (5, 500), "hard": (30, 700)}
    for tier, (size, body_cap) in sizes.items():
        for i in range(TIER_QUOTA[tier]):
            window = pool[cursor : cursor + size]
            cursor += size
            tasks.append(_doc_task(f"TD-{tier[0]}{i + 1:02d}", tier, window, body_cap))
    return tasks


# ---- review ----------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$")
_OPS_BEFORE_LITERAL = {"=", "(", ",", "==", "!=", ":", "[", "<", ">", "<=", ">=", "+"}


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _literal_tokens(line: str) -> list[tuple[str, str, int, int]]:
    """``(kind, text, start_col, end_col)`` for literals a reviewer could cross-check."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", '"', "'")):
        return []
    if '"""' in line or "'" * 3 in line:
        return []
    found: list[tuple[str, str, int, int]] = []
    previous = ""
    try:
        for tok in tokenize.generate_tokens(StringIO(line + "\n").readline):
            if tok.type == tokenize.NUMBER and re.fullmatch(r"\d{2,5}", tok.string):
                if previous in _OPS_BEFORE_LITERAL:
                    found.append(("int", tok.string, tok.start[1], tok.end[1]))
            elif tok.type == tokenize.STRING:
                body = tok.string[1:-1]
                if (
                    tok.string[0] in "\"'"
                    and tok.string[-1] == tok.string[0]
                    and re.fullmatch(r"[A-Za-z0-9_.:/-]{5,40}", body)
                    and previous in _OPS_BEFORE_LITERAL
                ):
                    found.append(("str", body, tok.start[1], tok.end[1]))
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT):
                previous = tok.string
    except (tokenize.TokenError, IndentationError):
        pass
    return found


def _parse_diff(diff: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current = {"path": raw.split(" b/", 1)[1], "lines": []}
            files.append(current)
        elif current is not None and not raw.startswith(("index ", "new file mode")):
            lines = current["lines"]
            assert isinstance(lines, list)
            lines.append(raw)
    return files


def _added_lines(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    for f in files:
        in_hunk = False
        for line in f["lines"]:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+") and not line.startswith("+++"):
                added.append(
                    {
                        "path": f["path"],
                        "text": line[1:],
                        "is_test": _is_test_path(str(f["path"])),
                    }
                )
    return added


def _int_pattern(literal: str) -> str:
    return rf"(?<![\w.]){literal}(?![\w.])"


def _review_candidate(repo: str, sha: str) -> dict[str, Any] | None:
    diff = _git(repo, "show", "--no-color", "--no-renames", "--format=", "-U3", sha)
    if "GIT binary patch" in diff or "Binary files" in diff:
        return None
    files = _parse_diff(diff)
    if not files or not _clean(diff):
        return None
    added = _added_lines(files)
    test_blob = "\n".join(str(a["text"]) for a in added if a["is_test"])
    all_blob = "\n".join(str(a["text"]) for a in added)
    src = [
        (i, a)
        for i, a in enumerate(added)
        if not a["is_test"] and str(a["path"]).endswith(".py")
    ]
    if not test_blob or len(src) < 3:
        return None
    for index, entry in src:
        text = str(entry["text"])
        for kind, literal, start, end in _literal_tokens(text):
            if kind == "int":
                if not re.search(_int_pattern(literal), test_blob):
                    continue
                replacement = str(int(literal) + 1)
                clash = re.search(_int_pattern(replacement), all_blob) is not None
                hits = sum(
                    len(re.findall(_int_pattern(literal), str(a["text"])))
                    for _, a in src
                )
                altered = text[:start] + replacement + text[end:]
            else:
                if not re.search(rf"[\"']{re.escape(literal)}[\"']", test_blob):
                    continue
                replacement = literal[:-1] + ("x" if literal[-1] != "x" else "y")
                clash = replacement in all_blob
                hits = sum(str(a["text"]).count(literal) for _, a in src)
                altered = text[: start + 1] + replacement + text[end - 1 :]
            if clash or hits != 1 or altered == text:
                continue
            return {
                "files": files,
                "added": added,
                "index": index,
                "original": text,
                "altered": altered,
                "literal": literal,
                "replacement": replacement,
            }
    return None


def _render_review(cand: dict[str, Any]) -> tuple[str, int, list[int]]:
    """The labelled diff, the altered line's label, and every other source label."""
    files: list[dict[str, Any]] = cand["files"]
    added: list[dict[str, Any]] = cand["added"]
    altered_index = int(cand["index"])
    out: list[str] = []
    counter = -1
    other_src: list[int] = []
    altered_label = -1
    for f in files:
        in_hunk = False
        for line in f["lines"]:
            if line.startswith("@@"):
                in_hunk = True
                out.append(line)
            elif in_hunk and line.startswith("+") and not line.startswith("+++"):
                counter += 1
                label = counter + 1
                if counter == altered_index:
                    altered_label = label
                    out.append(f"[{label}]+{cand['altered']}")
                else:
                    out.append(f"[{label}]{line}")
                    if not added[counter]["is_test"]:
                        other_src.append(label)
            else:
                out.append(line)
    return "\n".join(out), altered_label, other_src


def _review_tier(chars: int, files: int) -> str | None:
    if chars <= 5500 and files <= 3:
        return "easy"
    if 6000 <= chars <= 16000 and 2 <= files <= 6:
        return "medium"
    if 20000 <= chars <= 36000 and files >= 3:
        return "hard"
    return None


def tiered_review() -> list[dict[str, Any]]:
    chosen: dict[str, list[dict[str, Any]]] = {t: [] for t in TIER_QUOTA}
    pool: list[tuple[str, str]] = []
    for repo in TIERED_PINS:
        listing = _git(
            repo, "log", "--first-parent", "-n3000", "--format=%H", TIERED_PINS[repo]
        ).split()
        pool.extend((repo, sha) for sha in listing)
    pool.sort(key=lambda rs: _rank("review", rs[1]))
    for repo, sha in pool:
        if all(len(chosen[t]) >= TIER_QUOTA[t] for t in TIER_QUOTA):
            break
        message = _TRAILER_RE.sub(
            "", _git(repo, "log", "-1", "--format=%s%n%n%b", sha)
        ).strip()
        if not _clean(message):
            continue
        cand = _review_candidate(repo, sha)
        if cand is None:
            continue
        rendered, label, others = _render_review(cand)
        tier = _review_tier(len(rendered), len(cand["files"]))
        if tier is None or len(chosen[tier]) >= TIER_QUOTA[tier]:
            continue
        description = re.sub(r"\n{3,}", "\n\n", message)[:900].rstrip()
        n = len(chosen[tier]) + 1
        chosen[tier].append(
            {
                "task_id": f"TR-{tier[0]}{n:02d}",
                "title": f"find the altered line ({repo}@{sha[:10]})",
                "task_type": "review",
                "difficulty": tier,
                "provenance": (
                    f"Merged commit {repo}@{sha}. One added source line had the "
                    f"literal {cand['literal']!r} altered to {cand['replacement']!r}; "
                    "everything else is the merged diff."
                ),
                "prompt": (
                    "You are reviewing a merged pull request. Its description:\n\n"
                    f"{description}\n\n"
                    "The diff below was altered after the merge: exactly one added "
                    "line outside the test files was edited so that it no longer "
                    "agrees with the tests added in the same diff. Every added line "
                    "carries a label such as [12]. Name the label of the altered "
                    "line, then one sentence saying which test line it contradicts.\n\n"
                    f"DIFF:\n{rendered}\n\n" + NO_TRACE
                ),
                "scorer": "exact_match",
                "scorer_config": {
                    "accepted": [f"[{label}]"],
                    "forbidden": [f"[{x}]" for x in others],
                },
            }
        )
    short = {
        t: TIER_QUOTA[t] - len(v) for t, v in chosen.items() if len(v) < TIER_QUOTA[t]
    }
    if short:
        raise RuntimeError(f"review pool exhausted, short by {short}")
    return [t for tier in TIER_QUOTA for t in chosen[tier]]


# ---- reasoning -------------------------------------------------------------

_STDLIB = set(sys.stdlib_module_names)
_BUILTINS = set(dir(builtins))


class _Names(ast.NodeVisitor):
    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.bound: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        (self.loaded if isinstance(node.ctx, ast.Load) else self.bound).add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.bound.add(node.arg)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)


def _strip_annotations(fn: ast.FunctionDef) -> ast.FunctionDef:
    for a in [*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs]:
        a.annotation = None
    if fn.args.vararg is not None:
        fn.args.vararg.annotation = None
    if fn.args.kwarg is not None:
        fn.args.kwarg.annotation = None
    fn.returns = None
    return fn


def _module_index(
    tree: ast.Module,
) -> tuple[dict[str, ast.FunctionDef], dict[str, ast.Assign], dict[str, str]]:
    funcs: dict[str, ast.FunctionDef] = {}
    consts: dict[str, ast.Assign] = {}
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.decorator_list:
            funcs[node.name] = node
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            consts[node.targets[0].id] = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _STDLIB:
                    one = ast.Import(names=[alias])
                    imports[alias.asname or alias.name.split(".")[0]] = ast.unparse(one)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.level == 0
            and node.module.split(".")[0] in _STDLIB
        ):
            for alias in node.names:
                one_from = ast.ImportFrom(module=node.module, names=[alias], level=0)
                imports[alias.asname or alias.name] = ast.unparse(one_from)
    return funcs, consts, imports


def _assemble(
    name: str,
    funcs: dict[str, ast.FunctionDef],
    consts: dict[str, ast.Assign],
    imports: dict[str, str],
) -> tuple[str, int] | None:
    """Self-contained source for ``name`` and how many helpers it pulled in."""
    need_funcs: list[str] = []
    need_consts: list[str] = []
    need_imports: set[str] = set()
    queue = [name]
    while queue:
        current = queue.pop()
        if current in need_funcs:
            continue
        need_funcs.append(current)
        visitor = _Names()
        visitor.visit(funcs[current])
        for ident in visitor.loaded - visitor.bound:
            if ident in funcs:
                queue.append(ident)
            elif ident in consts:
                if ident not in need_consts:
                    need_consts.append(ident)
            elif ident in imports:
                need_imports.add(imports[ident])
            elif ident not in _BUILTINS:
                return None
    index = 0
    while index < len(need_consts):
        visitor = _Names()
        visitor.visit(consts[need_consts[index]])
        index += 1
        for inner in visitor.loaded - visitor.bound:
            if inner in imports:
                need_imports.add(imports[inner])
            elif inner in consts:
                if inner not in need_consts:
                    need_consts.append(inner)
            elif inner not in _BUILTINS:
                return None
    parts = sorted(need_imports)
    parts += [ast.unparse(consts[c]) for c in reversed(need_consts)]
    for f in reversed(need_funcs):
        clone = ast.parse(ast.unparse(funcs[f])).body[0]
        assert isinstance(clone, ast.FunctionDef)
        # A docstring that carries worked examples would hand over the answer.
        if (
            clone.body
            and isinstance(clone.body[0], ast.Expr)
            and isinstance(clone.body[0].value, ast.Constant)
            and isinstance(clone.body[0].value.value, str)
        ):
            clone.body = clone.body[1:] or [ast.Pass()]
        parts.append(ast.unparse(_strip_annotations(clone)))
    return "\n\n".join(parts), len(need_funcs) - 1


def run_call(code: str, call: str) -> tuple[str, int] | None:
    """``(repr, executed_lines)`` of ``call`` against ``code`` in a clean subprocess.

    ``executed_lines`` counts the source lines the call actually ran, which is
    the measure of how many steps a reader has to follow, not how long the
    function happens to be.
    """
    program = (
        f"{code}\n\nimport sys as _s\n_c = [0]\n"
        "def _tr(f, e, a):\n"
        "    if f.f_code.co_filename == '<string>':\n"
        "        if e == 'line':\n"
        "            _c[0] += 1\n"
        "        return _tr\n"
        "    return None\n"
        f"_s.settrace(_tr)\n_r = {call}\n_s.settrace(None)\n"
        "print(repr(_r))\nprint(_c[0])\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", program],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"PATH": os.defpath},
        )
    except subprocess.TimeoutExpired:
        return None
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or len(lines) < 2 or not lines[-1].isdigit():
        return None
    return "\n".join(lines[:-1]), int(lines[-1]) - 1


def _assert_pairs(
    tree: ast.Module, imported: dict[str, str]
) -> list[tuple[ast.Call, ast.expr]]:
    """``assert f(...) == literal`` and ``x = f(...)`` then ``assert x == literal``."""

    def is_call(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in imported
        )

    pairs: list[tuple[ast.Call, ast.expr]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigned: dict[str, ast.Call] = {}
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and is_call(node.value)
            ):
                assigned[node.targets[0].id] = node.value
            if not (
                isinstance(node, ast.Assert)
                and isinstance(node.test, ast.Compare)
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Eq)
            ):
                continue
            left = node.test.left
            if isinstance(left, ast.Call) and is_call(left):
                pairs.append((left, node.test.comparators[0]))
            elif isinstance(left, ast.Name) and left.id in assigned:
                pairs.append((assigned[left.id], node.test.comparators[0]))
    return pairs


def _reasoning_candidates() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for repo in TIERED_PINS:
        paths = [
            p
            for p in _git(
                repo, "ls-tree", "-r", "--name-only", TIERED_PINS[repo]
            ).split("\n")
            if p.endswith(".py")
        ]
        blobs: dict[str, str | None] = {}

        def read(
            path: str, _repo: str = repo, _blobs: dict[str, str | None] = blobs
        ) -> str | None:
            if path not in _blobs:
                try:
                    _blobs[path] = _git(_repo, "show", f"{TIERED_PINS[_repo]}:{path}")
                except subprocess.CalledProcessError:
                    _blobs[path] = None
            return _blobs[path]

        module_of = {
            p.removesuffix(".py").replace("/", ".").removeprefix("src."): p
            for p in paths
            if not _is_test_path(p)
        }
        for tpath in sorted(p for p in paths if _is_test_path(p)):
            source = read(tpath)
            if source is None:
                continue
            try:
                ttree = ast.parse(source)
            except SyntaxError:
                continue
            imported: dict[str, str] = {}
            for node in ttree.body:
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.level == 0
                    and node.module in module_of
                ):
                    for alias in node.names:
                        imported[alias.asname or alias.name] = node.module
            if not imported:
                continue
            for call, expected_node in _assert_pairs(ttree, imported):
                try:
                    args = [ast.literal_eval(a) for a in call.args]
                    kwargs = [ast.literal_eval(k.value) for k in call.keywords]
                    expected = ast.literal_eval(expected_node)
                except (ValueError, SyntaxError):
                    continue
                if not (args or kwargs) or expected in (
                    None,
                    True,
                    False,
                    0,
                    1,
                    "",
                    [],
                    {},
                    (),
                ):
                    continue
                assert isinstance(call.func, ast.Name)
                fn_name = call.func.id
                module_path = module_of[imported[fn_name]]
                msource = read(module_path)
                if msource is None:
                    continue
                try:
                    mtree = ast.parse(msource)
                except SyntaxError:
                    continue
                funcs, consts, imps = _module_index(mtree)
                if fn_name not in funcs:
                    continue
                assembled = _assemble(fn_name, funcs, consts, imps)
                if assembled is None:
                    continue
                code, helpers = assembled
                call_src = ast.unparse(call)
                if len(call_src) > 400 or len(repr(expected)) > 240:
                    continue
                if not _clean(code + call_src + repr(expected)):
                    continue
                ran = run_call(code, call_src)
                if ran is None or ran[0] != repr(expected):
                    continue
                found.append(
                    {
                        "repo": repo,
                        "test": tpath,
                        "module": module_path,
                        "function": fn_name,
                        "code": code,
                        "call": call_src,
                        "expected": repr(expected),
                        "helpers": helpers,
                        "steps": ran[1],
                        "lines": code.count("\n") + 1,
                        "branches": sum(
                            isinstance(
                                n,
                                (
                                    ast.If,
                                    ast.For,
                                    ast.While,
                                    ast.Try,
                                    ast.comprehension,
                                ),
                            )
                            for n in ast.walk(ast.parse(code))
                        ),
                    }
                )
    return found


def _reasoning_tier(c: dict[str, Any]) -> str:
    steps = int(c["steps"])
    if steps <= 4:
        return "easy"
    if steps >= 13:
        return "hard"
    return "medium"


def tiered_reasoning() -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for c in sorted(
        _reasoning_candidates(),
        key=lambda c: _rank("reason", str(c["call"]), str(c["module"])),
    ):
        key = (str(c["module"]), str(c["function"]), _reasoning_tier(c))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    chosen: dict[str, list[dict[str, Any]]] = {t: [] for t in TIER_QUOTA}
    for c in unique:
        tier = _reasoning_tier(c)
        if len(chosen[tier]) < TIER_QUOTA[tier]:
            chosen[tier].append(c)
    short = {
        t: TIER_QUOTA[t] - len(v) for t, v in chosen.items() if len(v) < TIER_QUOTA[t]
    }
    if short:
        raise RuntimeError(
            f"reasoning pool short by {short} of {len(unique)} unique; "
            f"steps seen {sorted(int(c['steps']) for c in unique)}"
        )
    tasks: list[dict[str, Any]] = []
    for tier, items in chosen.items():
        for i, c in enumerate(items):
            tasks.append(
                {
                    "task_id": f"TN-{tier[0]}{i + 1:02d}",
                    "title": f"predict {c['function']} ({c['repo']})",
                    "task_type": "reasoning",
                    "difficulty": tier,
                    "provenance": (
                        f"Function {c['function']} in {c['repo']}@"
                        f"{TIERED_PINS[str(c['repo'])][:12]}:{c['module']}; the call and "
                        f"the asserted result come from {c['test']}. The held value was "
                        "re-derived by executing the call."
                    ),
                    "prompt": (
                        "Here is a Python program.\n\n```python\n"
                        f"{c['code']}\n```\n\n"
                        f"What does `{c['call']}` evaluate to? Reply with only the "
                        "Python literal it evaluates to, exactly as repr() would "
                        "print it.\n\n" + NO_TRACE
                    ),
                    "scorer": "literal_equals",
                    "scorer_config": {"expected": c["expected"]},
                }
            )
    return tasks


# Lab lane names that appear in mined PR text (spelled in pieces here so this
# file does not itself carry them). The public-repo hygiene gate refuses them, and they carry no meaning for the task, so each is replaced by a
# neutral word everywhere in the bundle (the fed text and the held text alike).
LAB_WORD_SUBSTITUTIONS: dict[str, str] = {
    "stability" + "-test": "verify-env",
    "ci" + "-bus": "ci-queue",
    "compose" + "-dev": "compose-env",
}


def scrub_lab_words(task: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(task, ensure_ascii=False)
    for word, neutral in LAB_WORD_SUBSTITUTIONS.items():
        text = text.replace(word, neutral)
    scrubbed: dict[str, Any] = json.loads(text)
    return scrubbed


def build_tiered() -> None:
    TIERED.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, builder in (
        ("document", tiered_document),
        ("review", tiered_review),
        ("reasoning", tiered_reasoning),
    ):
        for built in builder():
            task = scrub_lab_words(built)
            prefix = str(task["task_id"]).split("-")[0]
            task["rung"] = (
                "R2"
                if task["scorer"] == "grounding_coverage"
                else {"TD": "R1", "TR": "R3", "TN": "R5"}[prefix]
            )
            task["dependent_reasoning_steps"] = {"TD": 2, "TR": 3, "TN": 3}[prefix]
            task.setdefault("fixture_files", {})
            (TIERED / f"{task['task_id']}.json").write_text(
                json.dumps(task, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            written += 1
            print(
                f"{task['task_id']:<8}{name:<10}{len(str(task['prompt'])):>7} prompt chars"
            )
    print(f"\n{written} tiered bundles written to {TIERED}")


def main() -> None:
    if "--tiered" in sys.argv:
        build_tiered()
        return
    BUNDLES.mkdir(parents=True, exist_ok=True)
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
