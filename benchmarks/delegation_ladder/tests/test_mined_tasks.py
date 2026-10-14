# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the tasks mined from real merged work (OMN-20844).

Two kinds. The first run the mining helpers on small synthetic inputs, each with
a negative control, so a helper that only ever accepts (or only ever rejects)
shows up. The second run over the committed mined bundles and re-prove, by
execution, what the miner claimed when it wrote them: the control fails, the
real code passes, a diagnosis cannot be passed by echoing its own trace.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from benchmarks.delegation_ladder import build_bundles as mining
from benchmarks.delegation_ladder.models import EnumOutcome, ModelTaskBundle
from benchmarks.delegation_ladder.scorers import (
    score_diagnosis_location,
    score_patch_apply_and_test,
    score_unit_test_execution,
)

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent.parent
BUNDLES = HERE / "bundles"

SUBJECT = '''\
"""A tiny module."""

import math


def clamp(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def area(radius):
    return math.pi * radius * radius


def passes(score, mark):
    return score >= mark
'''


# --------------------------------------------------------------------------
# What a public bundle must not carry
# --------------------------------------------------------------------------


# Built from parts so that this file carries none of the literals it proves the
# miner refuses (the repository's leak gates scan tests too).
_PRIVATE_SURFACES = [
    "see /" + "home" + "/someone/work/file.py",
    "ssh to " + ".".join(["192", "168", "86", "201"]) + " first",
    "mail a.person" + "@" + "example.com",
    "the omni" + "_home registry",
    "notes in knowledge" + "_base_internal",
    "key " + "sk" + "-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
]


@pytest.mark.parametrize("text", _PRIVATE_SURFACES)
def test_hygiene_refuses_text_that_names_a_private_surface(text: str) -> None:
    assert mining.hygiene_problems(text)


def test_hygiene_passes_plain_code_and_prose() -> None:
    assert mining.hygiene_problems(SUBJECT) == []
    assert mining.hygiene_problems("Retire the pre-push test leg in CI.") == []


def test_hygiene_refuses_a_listed_person_name() -> None:
    assert mining.hygiene_problems("thanks, Zaphod", {"zaphod"})
    assert mining.hygiene_problems("thanks, everyone", {"zaphod"}) == []


# --------------------------------------------------------------------------
# Locating the block a change lives in
# --------------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def test_a_change_inside_one_function_yields_that_function_as_the_block() -> None:
    post = SUBJECT.replace("value < low", "value <= low")
    block = mining._block_for_change(_lines(SUBJECT), _lines(post))
    assert block is not None
    assert block["block"].startswith("def clamp")
    assert block["prelude"].endswith("import math\n\n\n")
    assert block["suffix"].lstrip().startswith("def area")
    assert block["single_line"] is True
    assert block["removed"] == [7]
    # The rebuilt post-fix module is exactly the real post-fix module.
    assert block["prelude"] + block["post_block"] + block["suffix"] == post


def test_a_change_in_two_distant_functions_is_refused() -> None:
    padding = "\n\n".join(f"def f{i}():\n    return {i}\n" for i in range(6))
    pre = SUBJECT + "\n\n" + padding + "\n\n" + SUBJECT.split("def area")[0]
    post = pre.replace("value < low", "value <= low", 1)
    post = post.replace("return 5", "return 55")
    assert mining._block_for_change(_lines(pre), _lines(post)) is None


def test_a_function_added_after_a_block_belongs_to_that_block() -> None:
    post = SUBJECT.replace(
        "    return value\n",
        "    return value\n\n\ndef clamp01(v):\n    return clamp(v, 0, 1)\n",
    )
    block = mining._block_for_change(_lines(SUBJECT), _lines(post))
    assert block is not None
    assert "def clamp01" in block["post_block"]
    assert "def clamp01" not in block["block"]
    assert block["prelude"] + block["post_block"] + block["suffix"] == post


# --------------------------------------------------------------------------
# Pointing a repository test at the harness's `subject`
# --------------------------------------------------------------------------


def test_a_repository_test_is_pointed_at_subject_in_every_import_shape() -> None:
    test = (
        "from pkg.mod import clamp\n"
        "from pkg import mod\n"
        "import pkg.mod as m\n"
        'def test_x(monkeypatch):\n    monkeypatch.setattr("pkg.mod.clamp", len)\n'
    )
    out = mining._rewrite_test(test, "pkg.mod")
    assert "pkg.mod" not in out
    assert "from subject import clamp" in out
    assert "import subject as mod" in out
    assert "import subject as m" in out
    assert '"subject.clamp"' in out


def test_the_focused_run_scrubs_the_machine_it_ran_on() -> None:
    test = "from subject import clamp\n\n\ndef test_it():\n    assert clamp(5, 0, 3) == 4\n"
    rc, out = mining._run_focused(SUBJECT, test)
    assert rc != 0
    assert "1 failed" in out
    assert "assert 3 == 4" in out
    assert not re.search(r"/tmp/|/home/|\d+\.\d+s\b", out)


# --------------------------------------------------------------------------
# Slicing a long test down to what failed
# --------------------------------------------------------------------------

LONG_TEST = """\
from subject import clamp

LIMIT = 3


def helper():
    return clamp(9, 0, LIMIT)


def test_a():
    assert helper() == 3


def test_b():
    assert clamp(-1, 0, 3) == 0


class TestC:
    def test_c1(self):
        assert clamp(1, 0, 3) == 1

    def test_c2(self):
        assert clamp(2, 0, 3) == 2
"""


def test_slicing_keeps_setup_and_only_the_failing_tests() -> None:
    sliced = mining._slice_test(LONG_TEST, {"test_b", "TestC::test_c2"})
    assert sliced is not None
    assert "LIMIT = 3" in sliced
    assert "def helper" in sliced
    assert "def test_b" in sliced
    assert "def test_c2" in sliced
    assert "def test_a" not in sliced
    assert "def test_c1" not in sliced
    compile(sliced, "sliced", "exec")


def test_slicing_to_nothing_is_refused() -> None:
    assert mining._slice_test("def test_only():\n    assert True\n", set()) is None


# --------------------------------------------------------------------------
# Mutants
# --------------------------------------------------------------------------


def test_every_mutation_site_makes_a_one_token_edit_that_still_parses() -> None:
    sites = mining._mutation_sites(SUBJECT)
    kinds = {site[5] for site in sites}
    assert {"comparison"} <= kinds
    assert sites
    for site in sites:
        mutant = mining._apply_site(SUBJECT, site)
        compile(mutant, "mutant", "exec")
        assert mutant != SUBJECT
        # One token: the edit never touches more than its own line.
        diff = [
            (a, b)
            for a, b in zip(SUBJECT.splitlines(), mutant.splitlines(), strict=True)
            if a != b
        ]
        assert len(diff) == 1


def test_a_killable_mutant_is_one_the_modules_own_test_fails_on() -> None:
    test = (
        "from subject import clamp, passes\n\n\n"
        "def test_low():\n    assert clamp(-5, 0, 3) == 0\n\n\n"
        "def test_high():\n    assert clamp(9, 0, 3) == 3\n\n\n"
        "def test_inside():\n    assert clamp(2, 0, 3) == 2\n\n\n"
        "def test_edge_low():\n    assert clamp(0, 0, 3) == 0\n\n\n"
        "def test_boundary():\n    assert passes(5, 5)\n"
    )
    module = {
        "repo": "synthetic",
        "path": "src/pkg/mod.py",
        "source": SUBJECT,
        "test_text": test,
        "dotted": "pkg.mod",
        "ref": "none",
    }
    found = mining.find_killable_mutant(module, set())
    assert found is not None
    assert found["mutant"] != SUBJECT
    rc, _ = mining._run_focused(found["mutant"], test)
    assert rc != 0
    rc, _ = mining._run_focused(SUBJECT, test)
    assert rc == 0


def test_a_module_whose_tests_catch_nothing_yields_no_mutant() -> None:
    module = {
        "repo": "synthetic",
        "path": "src/pkg/mod.py",
        "source": SUBJECT,
        "test_text": "from subject import clamp\n\n\ndef test_vacuous():\n    assert True\n",
        "dotted": "pkg.mod",
        "ref": "none",
    }
    assert mining.find_killable_mutant(module, set()) is None


# --------------------------------------------------------------------------
# A question about a call is held to what the repository test asserts
# --------------------------------------------------------------------------


def test_a_result_question_takes_its_answer_from_an_asserted_call() -> None:
    module = {
        "repo": "synthetic",
        "path": "src/pkg/mod.py",
        "source": SUBJECT,
        "test_text": "from subject import clamp\n\n\ndef test_it():\n    assert clamp(9, 0, 3) == 3\n    assert clamp(9, 0, 30) == 9\n",
        "dotted": "pkg.mod",
        "ref": "none",
    }
    task = mining.result_question_task(module)
    assert task is not None
    accepted = task["scorer_config"]["accepted"][0]
    assert accepted in {"RESULT: 3", "RESULT: 9"}
    assert task["scorer"] == "exact_match"
    assert "clamp(9, 0," in task["prompt"]
    # `RESULT: 3` must not credit `RESULT: 30`.
    forbidden = task["scorer_config"]["forbidden"]
    assert ("RESULT: 30" in forbidden) == (accepted == "RESULT: 3")


def test_a_result_question_is_refused_for_a_call_the_module_does_not_define() -> None:
    module = {
        "repo": "synthetic",
        "path": "src/pkg/mod.py",
        "source": SUBJECT,
        "test_text": "from subject import elsewhere\n\n\ndef test_it():\n    assert elsewhere(9, 0, 3) == 3\n",
        "dotted": "pkg.mod",
        "ref": "none",
    }
    assert mining.result_question_task(module) is None


def test_mining_never_imports_or_executes_the_module_it_mines() -> None:
    import inspect

    source = inspect.getsource(mining.result_question_task)
    assert "exec(" not in source
    assert "import_module" not in source


# --------------------------------------------------------------------------
# Inline fixtures: a mined task is one JSON file
# --------------------------------------------------------------------------


def test_the_unit_test_scorer_reads_inline_subject_and_mutant() -> None:
    good = (
        "```python\nfrom subject import clamp\n\n\n"
        "def test_a():\n    assert clamp(2, 0, 3) == 2\n    assert clamp(-1, 0, 3) == 0\n\n\n"
        "def test_b():\n    assert clamp(9, 0, 3) == 3\n```"
    )
    # Passes both versions: a test that does not discriminate.
    weak = (
        "```python\nfrom subject import clamp\n\n\n"
        "def test_a():\n    assert clamp(2, 0, 3) == 2\n```"
    )
    mutant_only = SUBJECT.replace("return low", "return low + 1")
    config_hard = {"subject_text": SUBJECT, "mutant_text": mutant_only}
    assert (
        score_unit_test_execution(weak, config_hard, HERE / "fixtures").outcome
        is EnumOutcome.FAIL
    )
    assert (
        score_unit_test_execution(good, config_hard, HERE / "fixtures").outcome
        is EnumOutcome.PASS
    )


def test_the_patch_scorer_replaces_only_the_block_and_keeps_the_rest() -> None:
    pre = SUBJECT.replace("value < low", "value <= low").replace(
        "return low", "return low + 1"
    )
    block = mining._block_for_change(_lines(pre), _lines(SUBJECT))
    assert block is not None
    test = (
        "from subject import clamp, area\n\n\n"
        "def test_clamp():\n    assert clamp(-1, 0, 3) == 0\n\n\n"
        "def test_area_is_untouched():\n    assert area(1) > 3.14\n"
    )
    config = {
        "prelude_text": block["prelude"],
        "prefix_body_text": block["block"],
        "suffix_text": block["suffix"],
        "focused_test_text": test,
    }
    fixed = f"```python\n{block['post_block']}```"
    assert (
        score_patch_apply_and_test(fixed, config, HERE / "fixtures").outcome
        is EnumOutcome.PASS
    )
    unchanged = f"```python\n{block['block']}```"
    assert (
        score_patch_apply_and_test(unchanged, config, HERE / "fixtures").outcome
        is EnumOutcome.FAIL
    )
    # A test that passes the pre-fix block makes the task void, never a pass.
    vacuous = {**config, "focused_test_text": "def test_v():\n    assert True\n"}
    assert (
        score_patch_apply_and_test(fixed, vacuous, HERE / "fixtures").outcome
        is EnumOutcome.HARNESS_ERROR
    )


# --------------------------------------------------------------------------
# The committed mined bundles are re-proved by execution
# --------------------------------------------------------------------------


def _mined() -> list[ModelTaskBundle]:
    bundles = [
        ModelTaskBundle.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(BUNDLES.glob("*.json"))
    ]
    return [b for b in bundles if int(b.task_id.rsplit("-", 1)[1]) >= 5]


def test_mined_bundles_carry_no_private_surface() -> None:
    names = mining._person_names()
    for bundle in _mined():
        problems = mining.hygiene_problems(
            mining._task_text(bundle.model_dump(mode="json")), names
        )
        assert not problems, f"{bundle.task_id}: {problems}"


def test_mined_bundles_name_a_real_source() -> None:
    for bundle in _mined():
        assert re.search(r"\b[0-9a-f]{12,40}\b|repositor", bundle.provenance), (
            f"{bundle.task_id} names no commit or repository test"
        )


def test_every_mined_patch_task_fails_before_and_passes_after() -> None:
    checked = 0
    for bundle in _mined():
        if bundle.scorer.value != "patch_apply_and_test":
            continue
        config = bundle.scorer_config
        pre = "".join(
            str(config[key])
            for key in ("prelude_text", "prefix_body_text", "suffix_text")
        )
        rc_pre, _ = mining._run_focused(str(pre), str(config["focused_test_text"]))
        assert rc_pre != 0, (
            f"{bundle.task_id}: the focused test passes the pre-fix code"
        )
        checked += 1
    assert checked >= 1


def test_every_mined_unit_test_task_has_a_mutant_the_real_code_does_not_share() -> None:
    checked = 0
    for bundle in _mined():
        if bundle.scorer.value != "unit_test_execution":
            continue
        assert (
            bundle.scorer_config["subject_text"] != bundle.scorer_config["mutant_text"]
        )
        compile(str(bundle.scorer_config["mutant_text"]), bundle.task_id, "exec")
        checked += 1
    assert checked >= 1


def test_every_mined_diagnosis_trace_regenerates_from_its_inline_sources() -> None:
    checked = 0
    for bundle in _mined():
        if bundle.scorer.value != "diagnosis_location":
            continue
        config = bundle.scorer_config
        subject, test = (
            str(config["proof_subject_text"]),
            str(config["proof_test_text"]),
        )
        rc, out = mining._run_focused(subject, test)
        assert rc != 0, f"{bundle.task_id}: the proof test passes its own subject"
        committed = re.search(r"^\d+ failed.*$", bundle.prompt, re.MULTILINE)
        regenerated = re.search(r"^\d+ failed.*$", out, re.MULTILINE)
        assert committed
        assert regenerated
        assert (
            committed.group().split(" in ")[0] == regenerated.group().split(" in ")[0]
        )
        checked += 1
    assert checked >= 1


def test_no_mined_diagnosis_can_be_passed_by_echoing_its_own_trace() -> None:
    for bundle in _mined():
        if bundle.scorer.value != "diagnosis_location":
            continue
        trace = bundle.prompt[bundle.prompt.index("PYTEST OUTPUT:") :]
        verdict = score_diagnosis_location(trace, dict(bundle.scorer_config))
        assert verdict.outcome is not EnumOutcome.PASS, bundle.task_id


def test_a_defect_line_answer_is_not_credited_for_a_longer_number() -> None:
    from benchmarks.delegation_ladder.scorers import score_exact_match

    config = {
        "accepted": ["Line 4"],
        "forbidden": [f"Line 4{d}" for d in range(10)],
    }
    assert score_exact_match("Line 4", config).outcome is EnumOutcome.PASS
    assert score_exact_match("Line 42", config).outcome is EnumOutcome.FAIL
    assert score_exact_match("Line 7", config).outcome is EnumOutcome.FAIL


def test_the_python_running_these_tests_can_collect_a_pytest_module() -> None:
    # Positive control for the executions above: a zero from them means nothing
    # unless this same interpreter can run a trivial test and see it pass.
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test_ok.py").write_text("def test_ok():\n    assert True\n")
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "test_ok.py",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
        )
    assert done.returncode == 0
    assert "1 passed" in done.stdout
