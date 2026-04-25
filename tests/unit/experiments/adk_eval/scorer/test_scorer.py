# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for the ADK-eval scorer (P9).

Synthetic fixtures cover the three canonical shapes:
  - fixture_perfect.json      → expected F1 = 1.0 everywhere
  - fixture_all_noise.json    → expected binary F1 = 0.0
  - fixture_half_right.json   → expected hand-computed values

Scorer must also handle the duplicate-label collapse rule: when P5 contains
two labels for the same file:line, the more severe label wins
(critical > major > minor > noise).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from omnimarket.experiments.adk_eval._local_models import (
    EnumTypeDebtPriority,
    ModelTypeDebtReport,
)
from omnimarket.experiments.adk_eval.scorer import (
    load_labels,
    score_report,
    score_reports,
)

TOL = 1e-3

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "scorer"
LABELS_PATH = FIXTURES / "mini_labels.yaml"


def _load_report(name: str) -> ModelTypeDebtReport:
    return ModelTypeDebtReport.model_validate_json((FIXTURES / name).read_text())


@pytest.mark.unit
class TestLoadLabels:
    def test_strips_repo_prefix_and_collapses_duplicates(self) -> None:
        labels = load_labels(LABELS_PATH)
        # 6 yaml entries collapse to 5 unique file:line keys
        assert len(labels) == 5
        # Repo prefix stripped
        assert "src/a.py:10" in labels
        assert "faux_repo:src/a.py:10" not in labels
        # Most-severe-wins for duplicate key c.py:50 (noise + major → major)
        assert labels["src/c.py:50"] == EnumTypeDebtPriority.MAJOR

    def test_each_tier_present(self) -> None:
        labels = load_labels(LABELS_PATH)
        assert labels["src/a.py:10"] == EnumTypeDebtPriority.CRITICAL
        assert labels["src/a.py:20"] == EnumTypeDebtPriority.MAJOR
        assert labels["src/b.py:30"] == EnumTypeDebtPriority.MINOR
        assert labels["src/b.py:40"] == EnumTypeDebtPriority.NOISE


@pytest.mark.unit
class TestScorePerfect:
    def test_perfect_report_scores_one(self) -> None:
        labels = load_labels(LABELS_PATH)
        report = _load_report("fixture_perfect.json")
        result = score_report(report, labels)
        assert math.isclose(result.binary_f1, 1.0, abs_tol=TOL)
        assert math.isclose(result.binary_precision, 1.0, abs_tol=TOL)
        assert math.isclose(result.binary_recall, 1.0, abs_tol=TOL)
        assert math.isclose(result.macro_f1, 1.0, abs_tol=TOL)
        for tier in ("critical", "major", "minor", "noise"):
            assert math.isclose(result.per_class[tier].f1, 1.0, abs_tol=TOL)


@pytest.mark.unit
class TestScoreAllNoise:
    def test_all_noise_binary_f1_zero(self) -> None:
        labels = load_labels(LABELS_PATH)
        report = _load_report("fixture_all_noise.json")
        result = score_report(report, labels)
        # No TP for actionable → binary F1 = 0
        assert math.isclose(result.binary_f1, 0.0, abs_tol=TOL)
        assert math.isclose(result.binary_precision, 0.0, abs_tol=TOL)
        assert math.isclose(result.binary_recall, 0.0, abs_tol=TOL)

    def test_all_noise_macro_f1(self) -> None:
        labels = load_labels(LABELS_PATH)
        report = _load_report("fixture_all_noise.json")
        result = score_report(report, labels)
        # noise: P=1/5=0.2, R=1/1=1.0, F1=2*0.2*1/1.2 = 1/3
        assert math.isclose(result.per_class["noise"].f1, 1.0 / 3.0, abs_tol=TOL)
        # Other three tiers: TP=FP=0 → F1=0
        assert math.isclose(result.per_class["critical"].f1, 0.0, abs_tol=TOL)
        assert math.isclose(result.per_class["major"].f1, 0.0, abs_tol=TOL)
        assert math.isclose(result.per_class["minor"].f1, 0.0, abs_tol=TOL)
        # Macro = (1/3 + 0 + 0 + 0) / 4 = 1/12
        assert math.isclose(result.macro_f1, 1.0 / 12.0, abs_tol=TOL)


@pytest.mark.unit
class TestScoreHalfRight:
    def test_binary_scores(self) -> None:
        # pred=[crit,major,noise,minor,noise]  true=[crit,major,minor,noise,major]
        # binary pred=[1,1,0,0,0]  true=[1,1,0,0,1]
        # TP=2, FP=0, FN=1 → P=1.0, R=2/3, F1=0.8
        labels = load_labels(LABELS_PATH)
        report = _load_report("fixture_half_right.json")
        result = score_report(report, labels)
        assert math.isclose(result.binary_precision, 1.0, abs_tol=TOL)
        assert math.isclose(result.binary_recall, 2.0 / 3.0, abs_tol=TOL)
        assert math.isclose(result.binary_f1, 0.8, abs_tol=TOL)

    def test_per_class_scores(self) -> None:
        labels = load_labels(LABELS_PATH)
        report = _load_report("fixture_half_right.json")
        result = score_report(report, labels)
        # critical: TP=1, FP=0, FN=0 → F1=1.0
        assert math.isclose(result.per_class["critical"].f1, 1.0, abs_tol=TOL)
        # major: TP=1, FP=0, FN=1 → F1=2/3
        assert math.isclose(result.per_class["major"].f1, 2.0 / 3.0, abs_tol=TOL)
        # minor: TP=0, FP=1, FN=1 → F1=0
        assert math.isclose(result.per_class["minor"].f1, 0.0, abs_tol=TOL)
        # noise: TP=0, FP=2, FN=1 → F1=0
        assert math.isclose(result.per_class["noise"].f1, 0.0, abs_tol=TOL)
        # Macro F1 = (1 + 2/3 + 0 + 0) / 4 = 5/12
        assert math.isclose(result.macro_f1, 5.0 / 12.0, abs_tol=TOL)

    def test_confusion_matrix_counts(self) -> None:
        labels = load_labels(LABELS_PATH)
        report = _load_report("fixture_half_right.json")
        result = score_report(report, labels)
        cm = result.confusion_matrix
        # 5 matched findings total
        assert cm.total_matched == 5
        # Correct predictions = 2 (a.py:10, a.py:20)
        assert cm.total_correct == 2


@pytest.mark.unit
class TestCoverageAndUnmatched:
    def test_unmatched_finding_counts(self, tmp_path: Path) -> None:
        # Build a report with one finding that isn't in labels
        report_data = json.loads((FIXTURES / "fixture_perfect.json").read_text())
        report_data["findings_prioritized"].append(
            {
                "finding_ref": "src/unknown.py:999",
                "priority": "minor",
                "rationale": "not in labels",
                "fix_sketch": None,
            }
        )
        report_data["findings_total"] = 6
        path = tmp_path / "with_extra.json"
        path.write_text(json.dumps(report_data))

        labels = load_labels(LABELS_PATH)
        report = ModelTypeDebtReport.model_validate_json(path.read_text())
        result = score_report(report, labels)
        # One unmatched — doesn't contribute to scores
        assert result.unmatched_findings == 1
        # Other 5 are perfect → F1=1.0
        assert math.isclose(result.binary_f1, 1.0, abs_tol=TOL)

    def test_missing_label_counts(self) -> None:
        # If report is missing a label (e.g. only 3 of 5), record missing_labels count
        labels = load_labels(LABELS_PATH)
        partial_json = {
            "repo": "faux_repo",
            "generated_at": "2026-04-23T00:00:00+00:00",
            "findings_total": 3,
            "findings_prioritized": [
                {
                    "finding_ref": "src/a.py:10",
                    "priority": "critical",
                    "rationale": "x",
                    "fix_sketch": None,
                },
                {
                    "finding_ref": "src/a.py:20",
                    "priority": "major",
                    "rationale": "x",
                    "fix_sketch": None,
                },
                {
                    "finding_ref": "src/b.py:30",
                    "priority": "minor",
                    "rationale": "x",
                    "fix_sketch": None,
                },
            ],
            "tool": "omnimarket_node",
            "latency_seconds": 0.0,
            "llm_calls": 0,
            "estimated_cost_usd": 0.0,
        }
        partial = ModelTypeDebtReport.model_validate(partial_json)
        result = score_report(partial, labels)
        # 5 labels, 3 matched → 2 unscored labels
        assert result.missing_labels == 2
        assert result.matched_findings == 3


@pytest.mark.unit
class TestScoreReports:
    def test_both_tracks_present(self, tmp_path: Path) -> None:
        labels = load_labels(LABELS_PATH)
        track_a_report = _load_report("fixture_perfect.json")
        track_b_report = _load_report("fixture_all_noise.json")
        combined = score_reports(
            track_a=track_a_report,
            track_b=track_b_report,
            labels=labels,
        )
        assert math.isclose(combined["track_a"]["binary_f1"], 1.0, abs_tol=TOL)
        assert math.isclose(combined["track_b"]["binary_f1"], 0.0, abs_tol=TOL)
        assert "caveats" in combined

    def test_only_one_track(self) -> None:
        labels = load_labels(LABELS_PATH)
        track_a_report = _load_report("fixture_perfect.json")
        combined = score_reports(
            track_a=track_a_report,
            track_b=None,
            labels=labels,
        )
        assert math.isclose(combined["track_a"]["binary_f1"], 1.0, abs_tol=TOL)
        # Missing track stubbed with zeros, flagged in caveats
        assert combined["track_b"]["binary_f1"] == 0.0
        assert any("track_b" in c.lower() for c in combined["caveats"])
