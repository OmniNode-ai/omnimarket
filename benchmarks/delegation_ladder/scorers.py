# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mechanical scorers for the delegation task-complexity ladder (OMN-18300).

Every scorer here is mechanical. Three of them compare strings against an answer
held out of the prompt; three of them EXECUTE the model's output and report what
happened. None of them asks a model to grade a model, and none of them is a
judgement call dressed up as a number.

Two properties are deliberate and load-bearing:

* **The rubric halves are fixed checklists.** A rubric item is a substring that
  must be present, a substring that must be absent, or a word ceiling. It is
  never "is this good". A checklist can be wrong, but it cannot drift between
  runs, and a reader can audit it against the bundle.

* **The execution halves carry a discrimination control.** A generated test that
  passes against the real function proves nothing on its own: ``assert True``
  passes too. It is scored as a pass only if it ALSO fails against a
  deliberately broken copy. The same applies to a patch: the focused test must
  fail on the pre-fix source, or the task is reported as broken rather than
  passed.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from benchmarks.delegation_ladder.models import EnumOutcome, ModelScore

TestRunner = Callable[[Path, str, int], tuple[int, str]]

# --------------------------------------------------------------------------
# Response anatomy: separating the answer from the reasoning trace
# --------------------------------------------------------------------------

# OMN-18278: the local backend prepends a reasoning trace to its answer despite
# the prompt forbidding it, sometimes terminated by a stray closing tag. These
# markers are the observed openers. The list is fixed so the leak measurement
# cannot be tuned after seeing the results.
_LEAK_OPENERS: tuple[str, ...] = (
    "<think>",
    "here's a thinking process",
    "here is a thinking process",
    "let me think through",
    "thinking process:",
)

_CLOSE_TAG = "</think>"

# A heading the model sometimes emits to mark where the answer begins. Matched
# on its own line only, so prose mentioning the word does not trigger it.
_ANSWER_HEADER_RE = re.compile(
    r"^[ \t]*(?:\*\*|#+[ \t]*)?"
    r"(?:final answer|final|rewritten note|rewritten|answer|output|result)"
    r"[ \t]*:?[ \t]*(?:\*\*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_answer(response: str) -> tuple[str, float]:
    """Return ``(answer_region, leak_fraction)`` under fixed, ordered rules.

    1. A closing think tag ends the trace: the answer is everything after the
       LAST one.
    2. An opener with no closing tag means the whole response is trace; the
       answer region is empty and the leak fraction is 1.0.
    3. Otherwise, if the model emitted an answer heading on its own line, the
       answer is everything after the last such heading.
    4. Otherwise the whole response is the answer and nothing leaked.

    The rules run in this order and never look at whether the result scores
    better, which is the only reason the number means anything.
    """
    if not response:
        return "", 0.0

    lowered = response.lower()

    if _CLOSE_TAG in lowered:
        cut = lowered.rindex(_CLOSE_TAG) + len(_CLOSE_TAG)
        answer = response[cut:].strip()
        return answer, _fraction_lost(response, answer)

    has_opener = any(marker in lowered for marker in _LEAK_OPENERS)

    headers = list(_ANSWER_HEADER_RE.finditer(response))
    if headers:
        answer = response[headers[-1].end() :].strip()
        return answer, _fraction_lost(response, answer)

    if has_opener:
        # An unterminated trace with no answer heading. The caller received a
        # reasoning monologue; treating any of it as the answer would credit
        # the model for text it never designated as its answer.
        return "", 1.0

    return response.strip(), 0.0


def _fraction_lost(response: str, answer: str) -> float:
    total = len(response)
    if total == 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (len(answer) / total)))


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Typed reads out of a scorer's config
# --------------------------------------------------------------------------
# A bundle's scorer_config is JSON, so every value arrives as `object`. These
# three readers narrow it once, in one place, instead of scattering casts and
# type-ignore comments through the scoring logic.


def _strs(config: dict[str, object], key: str) -> list[str]:
    value = config.get(key, [])
    if not isinstance(value, list):
        raise TypeError(
            f"scorer config {key!r} must be a list, got {type(value).__name__}"
        )
    return [str(item) for item in value]


def _text(config: dict[str, object], key: str, default: str = "") -> str:
    value = config.get(key, default)
    return value if isinstance(value, str) else default


def _number(config: dict[str, object], key: str, default: float) -> float:
    value = config.get(key, default)
    return float(value) if isinstance(value, (int, float)) else default


def _fixture_text(
    config: dict[str, object], fixtures: Path, file_key: str, text_key: str
) -> str:
    """A fixture's text: inline in the bundle when it carries it, else from a file.

    The mined tasks (OMN-20844) carry their sources inside the bundle, so a task
    drawn from a real merged change is one JSON file that can be read, diffed and
    rescored without a sibling fixture; the hand-built rungs keep their files.
    """
    inline = config.get(text_key)
    if isinstance(inline, str):
        return inline
    return (fixtures / _text(config, file_key)).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Identifier grounding
# --------------------------------------------------------------------------

# Token shapes an output can invent. OMN-18297 recorded exactly this failure:
# the local model produced repository pull-request numbers that occurred zero
# times in its input while getting every ticket number right, so both shapes
# have to be checked, not just the one that happened to be reliable.
_IDENTIFIER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bOMN-\d+\b"),
    re.compile(r"\b[a-z_]+(?:_[a-z]+)*#\d+\b"),
    re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])"),
    re.compile(r"\b(?:[a-z0-9-]+\.)+(?:ai|com|io|dev|local)\b"),
    re.compile(r"\bport \d{2,5}\b", re.IGNORECASE),
)


def extract_identifiers(text: str) -> list[str]:
    """Every identifier-shaped token in ``text``, deduplicated, order preserved."""
    seen: dict[str, None] = {}
    for pattern in _IDENTIFIER_RES:
        for match in pattern.findall(text):
            token = match if isinstance(match, str) else match[0]
            seen.setdefault(token, None)
    return list(seen)


def grounding_report(answer: str, source_text: str) -> tuple[float, list[str]]:
    """Fraction of the answer's identifiers that occur in the fed text.

    Returns ``(fraction, ungrounded_tokens)``. An answer carrying no identifiers
    scores 1.0: it invented nothing. That is not the same as being correct, and
    every grounding scorer pairs this with something else for that reason.
    """
    found = extract_identifiers(answer)
    if not found:
        return 1.0, []
    ungrounded = [token for token in found if token not in source_text]
    return (len(found) - len(ungrounded)) / len(found), ungrounded


# --------------------------------------------------------------------------
# R1 — single-fact rewrite
# --------------------------------------------------------------------------


def score_grounding_rubric(response: str, config: dict[str, object]) -> ModelScore:
    """Identifier grounding plus a fixed present/absent/length checklist."""
    answer, _ = split_answer(response)
    source = _text(config, "source_text")
    must_contain = _strs(config, "must_contain")
    must_not_contain = _strs(config, "must_not_contain")
    max_words = int(_number(config, "max_words", 10_000))

    grounded, ungrounded = grounding_report(answer, source)
    haystack = answer.lower()

    checklist: dict[str, bool] = {"grounded": not ungrounded}
    for needle in must_contain:
        checklist[f"contains:{needle}"] = needle.lower() in haystack
    for needle in must_not_contain:
        checklist[f"absent:{needle}"] = needle.lower() not in haystack
    word_count = len(answer.split())
    checklist[f"at_most_{max_words}_words"] = word_count <= max_words

    passed = all(checklist.values())
    score = sum(1 for ok in checklist.values() if ok) / len(checklist)
    detail = (
        f"{word_count} words; grounding {grounded:.2f}"
        + (f"; invented {ungrounded}" if ungrounded else "")
        + (
            ""
            if passed
            else "; failed " + ", ".join(k for k, ok in checklist.items() if not ok)
        )
    )
    return ModelScore(
        outcome=EnumOutcome.PASS if passed else EnumOutcome.FAIL,
        score=score,
        mechanical=True,
        detail=detail,
        sub_scores={"grounding": grounded, "word_count": float(word_count)},
        checklist=checklist,
    )


# --------------------------------------------------------------------------
# R2 — multi-source summary
# --------------------------------------------------------------------------


def score_grounding_coverage(response: str, config: dict[str, object]) -> ModelScore:
    """Every identifier in the output must occur in the input, and the output
    must mention enough of the identifiers the summary was asked to cover.

    Grounding is a hard gate: a summary that invents one pull-request number is
    not 90% useful, it is unusable unattended, because the reader cannot tell
    which of the numbers is the invented one.
    """
    answer, _ = split_answer(response)
    source = _text(config, "source_text")
    required = _strs(config, "required_ids")
    threshold = _number(config, "coverage_threshold", 0.6)

    grounded, ungrounded = grounding_report(answer, source)
    hits = [token for token in required if token in answer]
    coverage = (len(hits) / len(required)) if required else 1.0

    passed = not ungrounded and coverage >= threshold
    detail = (
        f"grounding {grounded:.2f} ({len(ungrounded)} invented), "
        f"coverage {coverage:.2f} ({len(hits)}/{len(required)})"
        + (f"; invented {ungrounded}" if ungrounded else "")
    )
    return ModelScore(
        outcome=EnumOutcome.PASS if passed else EnumOutcome.FAIL,
        score=(grounded + coverage) / 2.0,
        mechanical=True,
        detail=detail,
        sub_scores={"grounding": grounded, "coverage": coverage},
        checklist={
            "no_invented_identifiers": not ungrounded,
            f"coverage_at_least_{threshold}": coverage >= threshold,
        },
    )


# --------------------------------------------------------------------------
# R3 — code reading question and answer
# --------------------------------------------------------------------------

# The ambiguous characters are the point: the table exists to fold a model's
# typographic quotes onto the ASCII ones a held answer is written with.
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})  # noqa: RUF001


def normalize_answer(text: str) -> str:
    """Collapse whitespace and unify quote characters, nothing else.

    Deliberately NOT stripping punctuation or case beyond this: an exact-match
    rung that normalises aggressively stops measuring whether the model read the
    code and starts measuring whether it produced approximately the right noises.
    """
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip()


def score_exact_match(response: str, config: dict[str, object]) -> ModelScore:
    """Held-answer match: every accepted form present, every forbidden absent."""
    answer, _ = split_answer(response)
    normalized = normalize_answer(answer)
    accepted = [normalize_answer(x) for x in _strs(config, "accepted")]
    forbidden = [normalize_answer(x) for x in _strs(config, "forbidden")]

    checklist: dict[str, bool] = {}
    for needle in accepted:
        checklist[f"states:{needle}"] = needle in normalized
    for needle in forbidden:
        checklist[f"avoids:{needle}"] = needle not in normalized

    passed = all(checklist.values())
    score = (
        sum(1 for ok in checklist.values() if ok) / len(checklist) if checklist else 0.0
    )
    missing = [k for k, ok in checklist.items() if not ok]
    return ModelScore(
        outcome=EnumOutcome.PASS if passed else EnumOutcome.FAIL,
        score=score,
        mechanical=True,
        detail="exact match" if passed else "missed " + ", ".join(missing),
        checklist=checklist,
    )


# --------------------------------------------------------------------------
# Code extraction, shared by the two executing scorers
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)


def extract_code(answer: str) -> str:
    """The largest fenced code block, or the whole answer if it is unfenced.

    Largest rather than first: the local backend often emits a short illustrative
    fragment before the real artifact, and taking the first block would grade the
    fragment.
    """
    blocks: list[str] = _FENCE_RE.findall(answer)
    if blocks:
        return max(blocks, key=len)
    # An unfenced answer is accepted only when it actually looks like Python.
    # Without this, prose such as "I would write a test for the empty case"
    # reaches the interpreter, fails to parse, and is reported as a broken test
    # rather than as the refusal to produce code that it is.
    if re.search(r"^\s*(?:def |class |import |from \w+ import )", answer, re.MULTILINE):
        return answer
    return ""


def _run_pytest(workdir: Path, test_name: str, timeout_s: int) -> tuple[int, str]:
    """Run one generated test file in isolation and return ``(rc, tail)``.

    Focused by construction: one file, one temporary directory, no repository
    test tree in scope. This is the only selection the harness ever runs on the
    launching host.
    """
    # The subject is rewritten in this directory between runs (real, then mutant;
    # broken, then patched). A cached bytecode file is keyed on the source's
    # mtime in whole seconds and its length, so two same-length sources written
    # within one second would share it and the second run would execute the
    # first's code. Writing no bytecode makes the verdict a function of the text.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", test_name, "-q", "-p", "no:cacheprovider"],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout_s}s"
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode, output[-1200:]


def _collected_any(output: str) -> bool:
    """False when pytest found no tests to run, which is not a passing result."""
    return "no tests ran" not in output.lower() and "collected 0 items" not in output


# --------------------------------------------------------------------------
# R4 — write a unit test, with a mutation control
# --------------------------------------------------------------------------


def score_unit_test_execution(
    response: str,
    config: dict[str, object],
    fixtures: Path,
    *,
    test_runner: TestRunner | None = None,
) -> ModelScore:
    """Run the produced test against the real function and against a broken copy.

    A test is credited only when it passes the real function AND fails the
    mutant. A test that passes both does not test anything; a test that fails
    both is simply broken. Both are recorded distinctly so the report can say
    which happened.
    """
    answer, _ = split_answer(response)
    code = extract_code(answer)
    if not code.strip():
        return ModelScore(
            outcome=EnumOutcome.FAIL,
            score=0.0,
            mechanical=True,
            detail="no code in the response",
            checklist={"produced_code": False},
        )

    subject = _fixture_text(config, fixtures, "subject_file", "subject_text")
    mutant = _fixture_text(config, fixtures, "mutant_file", "mutant_text")
    timeout_s = int(_number(config, "timeout_s", 90))
    run_test = test_runner or _run_pytest

    with tempfile.TemporaryDirectory(prefix="ladder-r4-") as tmp:
        workdir = Path(tmp)
        (workdir / "test_generated.py").write_text(code, encoding="utf-8")

        (workdir / "subject.py").write_text(subject, encoding="utf-8")
        real_rc, real_out = run_test(workdir, "test_generated.py", timeout_s)

        (workdir / "subject.py").write_text(mutant, encoding="utf-8")
        mutant_rc, mutant_out = run_test(workdir, "test_generated.py", timeout_s)

    ran_something = _collected_any(real_out)
    passes_real = real_rc == 0 and ran_something
    fails_mutant = mutant_rc != 0

    checklist = {
        "produced_code": True,
        "collected_at_least_one_test": ran_something,
        "passes_the_real_function": passes_real,
        "fails_the_mutant": fails_mutant,
    }
    passed = all(checklist.values())

    if passes_real and not fails_mutant:
        detail = "test passes the real function but ALSO passes the mutant: it does not discriminate"
    elif not passes_real and not ran_something:
        detail = f"no test was collected: {real_out[-300:]}"
    elif not passes_real and not fails_mutant:
        detail = (
            f"test fails the real function and passes the mutant, which is backwards: "
            f"real={real_out[-200:]} mutant={mutant_out[-200:]}"
        )
    elif not passes_real:
        detail = f"test fails the real function: {real_out[-300:]}"
    else:
        detail = "passes the real function and fails the mutant"

    return ModelScore(
        outcome=EnumOutcome.PASS if passed else EnumOutcome.FAIL,
        score=sum(1 for ok in checklist.values() if ok) / len(checklist),
        mechanical=True,
        detail=detail,
        checklist=checklist,
    )


# --------------------------------------------------------------------------
# R5 — diagnose a failing test
# --------------------------------------------------------------------------


def score_diagnosis_location(response: str, config: dict[str, object]) -> ModelScore:
    """Did the answer name the actual defective site, and not a decoy?

    ``must_name`` is the held cause reduced to tokens that cannot be produced by
    restating the trace: the defective expression itself, or the line number. The
    trace is fed, so naming the failing test is worth nothing and appears in
    ``must_not_name`` where a decoy exists.
    """
    answer, _ = split_answer(response)
    haystack = normalize_answer(answer).lower()
    must_name = _strs(config, "must_name")
    any_of = _strs(config, "any_of")
    must_not_name = _strs(config, "must_not_name")

    checklist: dict[str, bool] = {}
    for token in must_name:
        checklist[f"names:{token}"] = normalize_answer(token).lower() in haystack
    if any_of:
        checklist["names_the_site"] = any(
            normalize_answer(token).lower() in haystack for token in any_of
        )
    for token in must_not_name:
        checklist[f"not_a_decoy:{token}"] = (
            normalize_answer(token).lower() not in haystack
        )

    passed = all(checklist.values())
    return ModelScore(
        outcome=EnumOutcome.PASS if passed else EnumOutcome.FAIL,
        score=(
            sum(1 for ok in checklist.values() if ok) / len(checklist)
            if checklist
            else 0.0
        ),
        mechanical=True,
        detail=(
            "named the defective site"
            if passed
            else "missed " + ", ".join(k for k, ok in checklist.items() if not ok)
        ),
        checklist=checklist,
    )


# --------------------------------------------------------------------------
# R6 — produce a patch, applied and tested
# --------------------------------------------------------------------------


def score_patch_apply_and_test(
    response: str,
    config: dict[str, object],
    fixtures: Path,
    *,
    test_runner: TestRunner | None = None,
) -> ModelScore:
    """Apply the produced code as the module body and run the focused test.

    Before crediting anything, the same focused test is run against the PRE-FIX
    source. If it passes there, the task itself is broken -- the test does not
    discriminate -- and the result is a harness error, never a pass. This is the
    control that stops a vacuous test from making every model look competent.
    """
    answer, _ = split_answer(response)
    code = extract_code(answer)
    if not code.strip():
        return ModelScore(
            outcome=EnumOutcome.FAIL,
            score=0.0,
            mechanical=True,
            detail="no code in the response",
            checklist={"produced_code": False},
        )

    prelude = _fixture_text(config, fixtures, "prelude_file", "prelude_text")
    prefix_body = _fixture_text(
        config, fixtures, "prefix_body_file", "prefix_body_text"
    )
    focused_test = _fixture_text(
        config, fixtures, "focused_test_file", "focused_test_text"
    )
    timeout_s = int(_number(config, "timeout_s", 90))
    run_test = test_runner or _run_pytest
    # A mined task repairs one block of a larger module. It declares the code
    # that follows the block (`suffix_text`, possibly empty), and the module is
    # prelude + block + suffix. The hand-built rungs repair everything below the
    # imports, so their module is prelude + block.
    block_layout = "suffix_text" in config
    suffix = _text(config, "suffix_text")

    def assemble(block: str) -> str:
        if block_layout:
            return prelude + block.rstrip("\n") + "\n" + suffix
        return prelude + "\n\n" + block

    with tempfile.TemporaryDirectory(prefix="ladder-r6-") as tmp:
        workdir = Path(tmp)
        (workdir / "test_focused.py").write_text(focused_test, encoding="utf-8")

        (workdir / "subject.py").write_text(assemble(prefix_body), encoding="utf-8")
        control_rc, control_out = run_test(workdir, "test_focused.py", timeout_s)

        (workdir / "subject.py").write_text(assemble(code), encoding="utf-8")
        patched_rc, patched_out = run_test(workdir, "test_focused.py", timeout_s)

    control_collected = _collected_any(control_out)
    if control_rc == 0 or not control_collected:
        reason = (
            "the focused test PASSES against the pre-fix source"
            if control_rc == 0
            else "the focused test collected no tests at all"
        )
        return ModelScore(
            outcome=EnumOutcome.HARNESS_ERROR,
            score=0.0,
            mechanical=True,
            detail=(
                f"{reason}, so it cannot discriminate; this task is broken and its "
                "result is void"
            ),
            checklist={"control_reproduces_the_bug": False},
        )

    ran_something = _collected_any(patched_out)
    patched_ok = patched_rc == 0 and ran_something
    checklist = {
        "produced_code": True,
        "control_reproduces_the_bug": True,
        "patched_source_passes_the_focused_test": patched_ok,
    }
    return ModelScore(
        outcome=EnumOutcome.PASS if patched_ok else EnumOutcome.FAIL,
        score=sum(1 for ok in checklist.values() if ok) / len(checklist),
        mechanical=True,
        detail=(
            "patch applies and the focused test passes"
            if patched_ok
            else f"focused test still fails after the patch: {patched_out[-400:]}"
        ),
        checklist=checklist,
    )


# --------------------------------------------------------------------------
# Multi-file edits against a repository tree (the structural hard corpus)
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def git_text(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def materialise_tree(commit: str, dest: Path, revert_to_parent: list[str]) -> None:
    """Write src, tests and pyproject at ``commit``, with some files at the parent."""
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "archive",
            commit,
            "src",
            "tests",
            "pyproject.toml",
        ],
        capture_output=True,
        check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    for path in revert_to_parent:
        (dest / path).write_text(
            git_text("show", f"{commit}^:{path}"), encoding="utf-8"
        )


def run_tree_test(tree: Path, test_path: str, timeout_s: int) -> tuple[int, str]:
    """Run one repository test file against ``tree/src``; return ``(rc, output)``."""
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tree / "src"),
    }
    argv = [sys.executable, "-m", "pytest", test_path, "-q", "-x", "--no-header"]
    argv += ["-p", "no:cacheprovider", "-o", "addopts="]
    try:
        completed = subprocess.run(
            argv,
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout_s}s"
    output = (completed.stdout + completed.stderr).replace(str(tree) + "/", "")
    return completed.returncode, output


_FILE_LINE_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?FILE:(?:\*\*)?\s*`?([^\s`*]+)`?", re.IGNORECASE
)


def parse_search_replace(answer: str) -> list[tuple[str, str, str]]:
    """``(path, search, replace)`` per edit block, in the order written."""
    edits: list[tuple[str, str, str]] = []
    current: str | None = None
    lines = answer.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _FILE_LINE_RE.match(line)
        if match:
            current = match.group(1)
        elif line.strip().startswith("<<<<<<< SEARCH") and current is not None:
            search: list[str] = []
            replace: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "=======":
                search.append(lines[i])
                i += 1
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(">>>>>>> REPLACE"):
                replace.append(lines[i])
                i += 1
            edits.append((current, "\n".join(search), "\n".join(replace)))
        i += 1
    return edits


def apply_search_replace(
    tree: Path, edits: list[tuple[str, str, str]], editable: list[str]
) -> str | None:
    """Apply every edit to ``tree``; return why one could not apply, or None."""
    for path, search, replace in edits:
        if path not in editable:
            return f"edit names a file not shown: {path}"
        if not search.strip():
            return f"empty SEARCH block in {path}"
        target = tree / path
        text = target.read_text(encoding="utf-8")
        count = text.count(search)
        if count != 1:
            return f"SEARCH text found {count} times in {path}"
        target.write_text(text.replace(search, replace, 1), encoding="utf-8")
    return None


def score_multi_file_edit_and_test(
    response: str, config: dict[str, object]
) -> ModelScore:
    """Apply search/replace edits to the commit's tree at the parent and run its test.

    The control comes first, as in the single-block scorer: the commit's own
    test must FAIL on the files as shown (the parent's), or the task cannot
    discriminate and the verdict is a harness error, never a pass.
    """
    answer, _ = split_answer(response)
    edits = parse_search_replace(answer)
    if not edits:
        return ModelScore(
            outcome=EnumOutcome.FAIL,
            score=0.0,
            mechanical=True,
            detail="no FILE/SEARCH/REPLACE edit in the response",
            checklist={"produced_edits": False},
        )
    commit = _text(config, "repo_commit")
    test_path = _text(config, "test_path")
    changed = _strs(config, "changed_files")
    editable = _strs(config, "editable_files")
    timeout_s = int(_number(config, "timeout_s", 240))
    with tempfile.TemporaryDirectory(prefix="ladder-hard-") as tmp:
        tree = Path(tmp)
        materialise_tree(commit, tree, changed)
        control_rc, control_out = run_tree_test(tree, test_path, timeout_s)
        refused = apply_search_replace(tree, edits, editable)
        patched_rc, patched_out = 1, ""
        if refused is None:
            patched_rc, patched_out = run_tree_test(tree, test_path, timeout_s)
    if control_rc == 0:
        return ModelScore(
            outcome=EnumOutcome.HARNESS_ERROR,
            score=0.0,
            mechanical=True,
            detail="the commit's test PASSES on the files as shown; the task cannot "
            "discriminate: " + control_out[-400:],
            checklist={"control_fails": False},
        )
    files_edited = len({path for path, _, _ in edits})
    if refused is not None:
        return ModelScore(
            outcome=EnumOutcome.FAIL,
            score=0.0,
            mechanical=True,
            detail=refused,
            sub_scores={"files_edited": float(files_edited)},
            checklist={"control_fails": True, "edits_apply": False},
        )
    passed = patched_rc == 0
    return ModelScore(
        outcome=EnumOutcome.PASS if passed else EnumOutcome.FAIL,
        score=1.0 if passed else 0.0,
        mechanical=True,
        detail="the commit's test passes after the edits"
        if passed
        else "the commit's test still fails: " + patched_out[-800:],
        sub_scores={"files_edited": float(files_edited)},
        checklist={"control_fails": True, "edits_apply": True, "test_passes": passed},
    )


# --------------------------------------------------------------------------
# Predict a call's value
# --------------------------------------------------------------------------

_LITERAL_PREFIX_RE = re.compile(
    r"^\s*(?:\*\*|#+)?\s*(?:final answer|answer|result|output|returns?)\s*[:=]\s*",
    re.IGNORECASE,
)


def _same_literal(a: object, b: object) -> bool:
    """Equality that does not let ``True == 1`` or ``1 == 1.0`` through."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_literal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(
            _same_literal(x, y) for x, y in zip(a, b, strict=True)
        )
    return bool(a == b)


def _literal_candidates(answer: str) -> list[str]:
    """The answer as given, unfenced, and each line, so wrapping does not decide."""
    text = answer.strip()
    fence = _FENCE_RE.findall(text)
    candidates = [text] + [f.strip() for f in fence]
    candidates += [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned: list[str] = []
    for item in candidates:
        item = _LITERAL_PREFIX_RE.sub("", item).strip().strip("`").strip()
        if item.endswith(".") and not item.endswith(".."):
            item = item[:-1]
        cleaned.append(item)
    return cleaned


def score_literal_equals(response: str, config: dict[str, object]) -> ModelScore:
    """Did the answer evaluate to the held Python literal, type for type?

    The held value was produced by executing the call, so it is not an opinion.
    Formatting does not decide the verdict: quotes, spacing, a code fence or an
    ``Answer:`` prefix are all folded away, and a value of the wrong TYPE fails.
    """
    answer, _ = split_answer(response)
    expected_text = _text(config, "expected")
    expected = ast.literal_eval(expected_text)
    for candidate in _literal_candidates(answer):
        try:
            got = ast.literal_eval(candidate)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        if _same_literal(got, expected):
            return ModelScore(
                outcome=EnumOutcome.PASS,
                score=1.0,
                mechanical=True,
                detail="evaluates to the held value",
                checklist={"matches_held_value": True},
            )
    return ModelScore(
        outcome=EnumOutcome.FAIL,
        score=0.0,
        mechanical=True,
        detail=f"no literal in the answer equals {expected_text[:80]}",
        checklist={"matches_held_value": False},
    )
