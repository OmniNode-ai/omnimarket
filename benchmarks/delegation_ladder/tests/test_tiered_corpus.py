# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tiered public task set: shape, scorers and held answers (OMN-20844).

The set is only worth running if it can separate rungs, so these tests pin what
makes it measure something: enough tasks per class and tier, held answers that
are re-derived rather than trusted, and a scorer that can fail.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.build_bundles import run_call
from benchmarks.delegation_ladder.models import (
    EnumDifficulty,
    EnumOutcome,
    ModelTaskBundle,
)
from benchmarks.delegation_ladder.run_ladder import score
from benchmarks.delegation_ladder.scorers import score_literal_equals

pytestmark = pytest.mark.unit

TIERED = Path(__file__).resolve().parent.parent / "bundles" / "tiered"
CLASSES = ("document", "review", "reasoning")


def _bundles() -> list[ModelTaskBundle]:
    return [
        ModelTaskBundle.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(TIERED.glob("*.json"))
    ]


def test_each_class_has_at_least_eight_per_tier_and_twenty_five_in_all() -> None:
    counts = Counter((b.task_type, b.difficulty) for b in _bundles())
    for task_class in CLASSES:
        for tier in EnumDifficulty:
            assert counts[(task_class, tier)] >= 8, (task_class, tier)
        assert sum(counts[(task_class, t)] for t in EnumDifficulty) >= 25


def test_task_ids_are_unique_and_every_task_is_tiered() -> None:
    bundles = _bundles()
    assert len({b.task_id for b in bundles}) == len(bundles)
    assert all(b.difficulty is not None for b in bundles)


def test_no_bundle_carries_private_workspace_text() -> None:
    private = ["omni" + "_home", "knowledge_base" + "_internal"]
    forbidden = re.compile("|".join(private) + r"|\b\d+\.\d+\.\d+\.\d+\b")
    for path in TIERED.glob("*.json"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name


def _strs(cfg: dict[str, object], key: str) -> list[str]:
    value = cfg[key]
    assert isinstance(value, list)
    return [str(v) for v in value]


def _control(bundle: ModelTaskBundle) -> tuple[str, str]:
    cfg = bundle.scorer_config
    kind = bundle.scorer.value
    if kind == "literal_equals":
        return f"Answer: {cfg['expected']}", "'not-the-value'"
    if kind == "exact_match":
        forbidden = _strs(cfg, "forbidden")
        wrong = forbidden[0] if forbidden else "[99999]"
        return (
            f"{_strs(cfg, 'accepted')[0]} contradicts the test",
            f"{wrong} is the line",
        )
    if kind == "grounding_rubric":
        return "Done " + " ".join(_strs(cfg, "must_contain")), "Nothing relevant."
    return " ".join(_strs(cfg, "required_ids")), "No ids at all."


def test_every_scorer_passes_its_positive_and_fails_its_negative_control() -> None:
    for bundle in _bundles():
        good, bad = _control(bundle)
        assert score(bundle, good).outcome is EnumOutcome.PASS, bundle.task_id
        assert score(bundle, bad).outcome is not EnumOutcome.PASS, bundle.task_id


def test_reasoning_held_answers_are_what_executing_the_call_gives() -> None:
    reasoning = [b for b in _bundles() if b.task_type == "reasoning"]
    assert len(reasoning) >= 25
    for bundle in reasoning:
        code = re.search(r"```python\n(.*?)\n```", bundle.prompt, re.DOTALL)
        call = re.search(r"What does `(.*)` evaluate to", bundle.prompt)
        assert code is not None, bundle.task_id
        assert call is not None, bundle.task_id
        ran = run_call(code.group(1), call.group(1))
        assert ran is not None, bundle.task_id
        assert ran[0] == bundle.scorer_config["expected"]


def test_review_held_label_is_in_the_diff_and_not_among_the_decoys() -> None:
    for bundle in (b for b in _bundles() if b.task_type == "review"):
        accepted = bundle.scorer_config["accepted"]
        forbidden = bundle.scorer_config["forbidden"]
        assert isinstance(accepted, list)
        assert isinstance(forbidden, list)
        assert accepted[0] in bundle.prompt
        assert accepted[0] not in forbidden
        assert len(forbidden) >= 2, "a review with no decoys proves nothing"


def test_document_required_ids_are_all_in_the_fed_text() -> None:
    for bundle in (b for b in _bundles() if b.scorer.value == "grounding_coverage"):
        required = bundle.scorer_config["required_ids"]
        assert isinstance(required, list)
        assert all(f"pull request {r}" in bundle.prompt for r in required)


@pytest.mark.parametrize(
    ("answer", "passes"),
    [
        ("'abc'", True),
        ('"abc"', True),
        ("```python\n'abc'\n```", True),
        ("Answer: 'abc'.", True),
        ("The result is 'abc' I think", False),
        ("'abd'", False),
        ("", False),
    ],
)
def test_literal_equals_folds_formatting_and_rejects_wrong_values(
    answer: str, passes: bool
) -> None:
    verdict = score_literal_equals(answer, {"expected": "'abc'"})
    assert (verdict.outcome is EnumOutcome.PASS) is passes


def test_literal_equals_is_type_strict() -> None:
    assert score_literal_equals("1", {"expected": "True"}).outcome is EnumOutcome.FAIL
    assert score_literal_equals("1.0", {"expected": "1"}).outcome is EnumOutcome.FAIL
    assert (
        score_literal_equals(
            "{'b': 2, 'a': 1}", {"expected": "{'a': 1, 'b': 2}"}
        ).outcome
        is EnumOutcome.PASS
    )
    assert (
        score_literal_equals("[1, 2]", {"expected": "[2, 1]"}).outcome
        is EnumOutcome.FAIL
    )


def test_committed_results_measure_every_rung_on_at_least_25_tasks_per_class() -> None:
    from benchmarks.delegation_ladder import summarise_results as tables

    results = TIERED.parent.parent / "results"
    # A rerun file overrides the cells of its main file, in this order.
    paths = sorted(
        results.glob("tiered-*.jsonl"),
        key=lambda p: (p.stem.endswith("-rerun"), p.name),
    )
    records = [
        r
        for r in tables.load_jsonl(paths)
        if r.get("backend") not in (None, "unpinned")
    ]
    for record in records:
        if record["backend"] == "local-omnipc2-chat":
            record["backend"] = "local-coder"
    rows = tables.class_rows([r for r in records if not tables.is_unreached(r)])
    backends = {row["backend"] for row in rows}
    assert len(backends) >= 5
    for row in rows:
        assert row["n"] >= 25, (row["task_type"], row["backend"], row["n"])
