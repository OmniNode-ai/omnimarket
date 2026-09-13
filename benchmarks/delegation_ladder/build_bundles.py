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

import json
from pathlib import Path

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


def main() -> None:
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
