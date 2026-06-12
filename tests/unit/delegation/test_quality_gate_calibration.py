# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Quality-gate calibration ratchet (OMN-12964).

This is the enforcement gate for P1.7. It proves the delegation quality gate
produces a NON-DEGENERATE score distribution and that known-good outputs
out-score known-bad outputs. Before OMN-12964 the gate returned a degenerate
{0.0, 1.0} verdict and applied code-docstring DoD to prose `document` tasks, so
every output scored 0.000 on both tiers (live CID a604cd40) - a scoring artifact
that would masquerade as an ON/OFF effect and invalidate Experiments 1-3.

If this test regresses (scores collapse, or good no longer beats bad), the
quality signal is meaningless again and experiment results must not be trusted.
The same assertions run in CI via scripts/ci/run_quality_gate_calibration.py.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_CORPUS_PATH = Path(__file__).parent / "quality_gate_calibration_corpus.yaml"

# Minimum spread between the worst and best score across the corpus. A degenerate
# gate (the OMN-12964 defect) yields 0.0 here; a discriminating gate is well above.
_MIN_SCORE_RANGE = 0.3
# Minimum number of distinct score values - a binary {0.0, 1.0} gate yields ≤ 2.
_MIN_DISTINCT_SCORES = 4
# Minimum margin by which mean(good) must exceed mean(bad).
_MIN_GOOD_BAD_MARGIN = 0.2
# Per-task-class minimum margin: min(good) - max(bad) must reach this threshold.
# A soft refusal that passes response_non_empty but fails no_refusal would otherwise
# produce a max_bad of 0.800 (1-heuristic-miss in a 2-heuristic DoD), leaving only
# a 0.200 margin — insufficient to distinguish quality tiers reliably (OMN-12964).
_MIN_PER_CLASS_MARGIN = 0.3


def _load_corpus() -> dict[str, object]:
    with _CORPUS_PATH.open() as fh:
        return yaml.safe_load(fh)


def _score_case(case: dict[str, object], task_classes: dict[str, dict]) -> float:
    task_class = str(case["task_class"])
    dod = task_classes[task_class]
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_class,
            llm_response_content=str(case["content"]),
            dod_deterministic=tuple(dod.get("deterministic", ())),
            dod_heuristic=tuple(dod.get("heuristic", ())),
        )
    )
    return result.quality_score


def _scored_corpus() -> list[tuple[str, str, float]]:
    corpus = _load_corpus()
    task_classes = corpus["task_classes"]  # type: ignore[index]
    rows: list[tuple[str, str, float]] = []
    for case in corpus["cases"]:  # type: ignore[index]
        rows.append(
            (
                str(case["id"]),
                str(case["label"]),
                _score_case(case, task_classes),  # type: ignore[arg-type]
            )
        )
    return rows


@pytest.mark.unit
def test_score_distribution_is_non_degenerate() -> None:
    """The corpus must yield a spread of scores, not a single collapsed value."""
    scores = [score for _, _, score in _scored_corpus()]
    distinct = sorted({round(s, 3) for s in scores})

    assert len(distinct) >= _MIN_DISTINCT_SCORES, (
        f"Degenerate score distribution - only {len(distinct)} distinct values "
        f"{distinct}. The quality gate collapsed (OMN-12964 regression); "
        f"experiment results would be invalid."
    )
    score_range = max(scores) - min(scores)
    assert score_range >= _MIN_SCORE_RANGE, (
        f"Score range {score_range:.3f} below {_MIN_SCORE_RANGE}; the gate does "
        f"not discriminate. scores={sorted(round(s, 3) for s in scores)}"
    )


@pytest.mark.unit
def test_known_good_outscores_known_bad() -> None:
    """Mean(good) must clear mean(bad) by a real margin, and beat every bad case."""
    rows = _scored_corpus()
    good = [s for _, label, s in rows if label == "good"]
    bad = [s for _, label, s in rows if label == "bad"]

    assert good, "calibration corpus has no good cases"
    assert bad, "calibration corpus has no bad cases"

    mean_good = statistics.mean(good)
    mean_bad = statistics.mean(bad)
    assert mean_good - mean_bad >= _MIN_GOOD_BAD_MARGIN, (
        f"mean(good)={mean_good:.3f} does not exceed mean(bad)={mean_bad:.3f} by "
        f"{_MIN_GOOD_BAD_MARGIN}; quality signal does not separate good from bad."
    )
    assert min(good) > max(bad), (
        f"worst good case ({min(good):.3f}) does not beat best bad case "
        f"({max(bad):.3f}); the bands overlap."
    )


@pytest.mark.unit
def test_prose_document_is_not_scored_against_docstring_dod() -> None:
    """A good prose `document` output must pass - not fail docstring DoD (OMN-12964)."""
    corpus = _load_corpus()
    task_classes = corpus["task_classes"]  # type: ignore[index]
    good_prose = next(
        c
        for c in corpus["cases"]
        if c["id"] == "doc_prose_excellent"  # type: ignore[index]
    )
    dod = task_classes["document"]  # type: ignore[index]
    # The prose DoD must not demand docstring syntax.
    assert "docstring_present" not in dod["deterministic"]
    assert "follows_google_style" not in dod["heuristic"]
    assert "covers_args_returns_raises" not in dod["heuristic"]

    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="document",
            llm_response_content=str(good_prose["content"]),
            dod_deterministic=tuple(dod["deterministic"]),
            dod_heuristic=tuple(dod["heuristic"]),
        )
    )
    assert result.passed, f"good prose document failed gate: {result.failure_reasons}"
    assert result.quality_score == pytest.approx(1.0)


@pytest.mark.unit
def test_corpus_task_class_dod_matches_shipped_contract() -> None:
    """Corpus DoD must mirror task_class_contracts.v1.yaml so the proof is real."""
    corpus = _load_corpus()
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "configs"
        / "task_class_contracts.v1.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    shipped = contract["task_classes"]
    for name, dod in corpus["task_classes"].items():  # type: ignore[index]
        shipped_dod = shipped[name]["definition_of_done"]
        assert list(dod["deterministic"]) == list(shipped_dod["deterministic"]), (
            f"corpus deterministic DoD for '{name}' drifted from the contract"
        )
        assert list(dod["heuristic"]) == list(shipped_dod["heuristic"]), (
            f"corpus heuristic DoD for '{name}' drifted from the contract"
        )


@pytest.mark.unit
def test_per_class_margin_meets_threshold() -> None:
    """Per task-class min(good) - max(bad) must be >= 0.3 with both bands nonzero.

    The corpus-level mean test (test_known_good_outscores_known_bad) passes even when
    a single task class has a narrow margin — a soft refusal that only fails one
    heuristic (e.g. no_refusal in a 2-heuristic DoD) scores 0.800, leaving only a
    0.200 margin that is indistinguishable from measurement noise in experiment data.
    This test locks the per-class floor introduced by OMN-12964 M2.4 fix.
    """
    corpus = _load_corpus()
    task_classes = corpus["task_classes"]  # type: ignore[index]

    # Group scores by task_class x label
    class_good: dict[str, list[float]] = {}
    class_bad: dict[str, list[float]] = {}
    for case in corpus["cases"]:  # type: ignore[index]
        tc = str(case["task_class"])
        label = str(case["label"])
        score = _score_case(case, task_classes)  # type: ignore[arg-type]
        if label == "good":
            class_good.setdefault(tc, []).append(score)
        elif label == "bad":
            class_bad.setdefault(tc, []).append(score)

    all_classes = set(class_good) | set(class_bad)
    assert all_classes, "corpus has no task classes"

    failures: list[str] = []
    for tc in sorted(all_classes):
        good = class_good.get(tc, [])
        bad = class_bad.get(tc, [])
        if not good or not bad:
            failures.append(
                f"  {tc}: missing good ({len(good)}) or bad ({len(bad)}) cases"
            )
            continue
        min_good = min(good)
        max_bad = max(bad)
        margin = round(min_good - max_bad, 3)
        if margin < _MIN_PER_CLASS_MARGIN:
            failures.append(
                f"  {tc}: min_good={min_good:.3f} max_bad={max_bad:.3f} "
                f"margin={margin:.3f} < {_MIN_PER_CLASS_MARGIN} — "
                f"quality gate does not discriminate this class reliably"
            )

    assert not failures, (
        f"Per-class margin below {_MIN_PER_CLASS_MARGIN} for "
        f"{len(failures)} class(es):\n" + "\n".join(failures)
    )
