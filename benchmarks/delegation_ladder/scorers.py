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

import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from benchmarks.delegation_ladder.models import EnumOutcome, ModelScore

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
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", test_name, "-q", "-p", "no:cacheprovider"],
            cwd=workdir,
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
    response: str, config: dict[str, object], fixtures: Path
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

    subject = (fixtures / _text(config, "subject_file")).read_text(encoding="utf-8")
    mutant = (fixtures / _text(config, "mutant_file")).read_text(encoding="utf-8")
    timeout_s = int(_number(config, "timeout_s", 90))

    with tempfile.TemporaryDirectory(prefix="ladder-r4-") as tmp:
        workdir = Path(tmp)
        (workdir / "test_generated.py").write_text(code, encoding="utf-8")

        (workdir / "subject.py").write_text(subject, encoding="utf-8")
        real_rc, real_out = _run_pytest(workdir, "test_generated.py", timeout_s)

        (workdir / "subject.py").write_text(mutant, encoding="utf-8")
        mutant_rc, mutant_out = _run_pytest(workdir, "test_generated.py", timeout_s)

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
    response: str, config: dict[str, object], fixtures: Path
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

    prelude = (fixtures / _text(config, "prelude_file")).read_text(encoding="utf-8")
    prefix_body = (fixtures / _text(config, "prefix_body_file")).read_text(
        encoding="utf-8"
    )
    focused_test = (fixtures / _text(config, "focused_test_file")).read_text(
        encoding="utf-8"
    )
    timeout_s = int(_number(config, "timeout_s", 90))

    with tempfile.TemporaryDirectory(prefix="ladder-r6-") as tmp:
        workdir = Path(tmp)
        (workdir / "test_focused.py").write_text(focused_test, encoding="utf-8")

        (workdir / "subject.py").write_text(
            prelude + "\n\n" + prefix_body, encoding="utf-8"
        )
        control_rc, control_out = _run_pytest(workdir, "test_focused.py", timeout_s)

        (workdir / "subject.py").write_text(prelude + "\n\n" + code, encoding="utf-8")
        patched_rc, patched_out = _run_pytest(workdir, "test_focused.py", timeout_s)

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
