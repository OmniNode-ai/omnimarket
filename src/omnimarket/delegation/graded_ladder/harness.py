# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic scoring harness for the graded ladder benchmark (OMN-13369).

Loads the ladder rungs, the escalating-complexity corpus, and the RECORDED
per-rung outputs, grades every (rung, task) cell with the objective graders,
rolls up per-rung graded scores, and evaluates the floor < ceiling separation
acceptance criterion. No live model call happens here — this is a pure function
of committed fixtures, so it runs hermetically in CI.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from omnimarket.delegation.graded_ladder.graders import grade
from omnimarket.delegation.graded_ladder.models import (
    TIER_WEIGHT,
    EnumBenchmarkTier,
    ModelBenchmarkPacket,
    ModelGradedCell,
    ModelLadderRung,
    ModelLadderTask,
    ModelRungScore,
    ModelSeparationVerdict,
)

_PKG_ROOT = Path(__file__).resolve().parents[3]  # .../src
_REPO_ROOT = _PKG_ROOT.parent  # repo root
_DATA_DIR = _REPO_ROOT / "tests" / "unit" / "delegation" / "graded_ladder"

DEFAULT_RUNGS_PATH = _DATA_DIR / "ladder_rungs.yaml"
DEFAULT_CORPUS_PATH = _DATA_DIR / "escalating_corpus.yaml"
DEFAULT_FIXTURES_PATH = _DATA_DIR / "recorded_rung_outputs.json"

# Acceptance threshold: the ceiling rung's weighted graded score must clear the
# floor rung's by at least this margin for the ladder to count as separated.
# This is a floor, not a fit-to-data value — the recorded run clears it with
# headroom (see the evidence packet), and it is large enough that scoring noise
# on a single task cannot manufacture a pass.
DEFAULT_REQUIRED_MARGIN = 0.15


def load_rungs(path: Path = DEFAULT_RUNGS_PATH) -> list[ModelLadderRung]:
    raw = yaml.safe_load(path.read_text())
    rungs = [ModelLadderRung(**item) for item in raw["rungs"]]
    return sorted(rungs, key=lambda r: r.order)


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> list[ModelLadderTask]:
    raw = yaml.safe_load(path.read_text())
    return [ModelLadderTask(**item) for item in raw["tasks"]]


def load_recorded_outputs(path: Path = DEFAULT_FIXTURES_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


class GradedLadderHarness:
    """Grade recorded rung outputs and compute the separation verdict."""

    def __init__(
        self,
        rungs: list[ModelLadderRung],
        tasks: list[ModelLadderTask],
        recorded: dict[str, Any],
        *,
        required_margin: float = DEFAULT_REQUIRED_MARGIN,
    ) -> None:
        self.rungs = sorted(rungs, key=lambda r: r.order)
        self.tasks = tasks
        self.recorded = recorded
        self.required_margin = required_margin

    def _cell(self, rung: ModelLadderRung, task: ModelLadderTask) -> ModelGradedCell:
        rung_outputs = self.recorded.get("rungs", {}).get(rung.rung_id, {})
        record = rung_outputs.get(task.task_id)
        if record is None:
            return ModelGradedCell(
                rung_id=rung.rung_id,
                task_id=task.task_id,
                benchmark_tier=task.benchmark_tier,
                grader=task.grader,
                passed=False,
                detail="no recorded output for cell",
                output_recorded=False,
                model_name=rung.model_name,
            )
        content = str(record.get("content", ""))
        passed, detail = grade(task, content)
        return ModelGradedCell(
            rung_id=rung.rung_id,
            task_id=task.task_id,
            benchmark_tier=task.benchmark_tier,
            grader=task.grader,
            passed=passed,
            detail=detail,
            output_recorded=True,
            output_chars=len(content),
            latency_ms=int(record.get("latency_ms", 0)),
            model_name=str(record.get("model_name", rung.model_name)),
        )

    def grade_all(self) -> list[ModelGradedCell]:
        return [self._cell(rung, task) for rung in self.rungs for task in self.tasks]

    def _rung_score(
        self, rung: ModelLadderRung, cells: list[ModelGradedCell]
    ) -> ModelRungScore:
        rung_cells = [c for c in cells if c.rung_id == rung.rung_id]
        total = len(rung_cells)
        passed = sum(1 for c in rung_cells if c.passed)
        pass_rate = passed / total if total else 0.0

        weight_sum = 0.0
        weight_passed = 0.0
        per_tier: dict[EnumBenchmarkTier, list[int]] = {}
        for cell in rung_cells:
            w = TIER_WEIGHT[cell.benchmark_tier]
            weight_sum += w
            if cell.passed:
                weight_passed += w
            per_tier.setdefault(cell.benchmark_tier, []).append(1 if cell.passed else 0)

        weighted = weight_passed / weight_sum if weight_sum else 0.0
        per_tier_rate = {
            tier.value: round(sum(v) / len(v), 4) for tier, v in per_tier.items()
        }
        return ModelRungScore(
            rung_id=rung.rung_id,
            order=rung.order,
            model_name=rung.model_name,
            gpu=rung.gpu,
            tasks_total=total,
            tasks_passed=passed,
            pass_rate=round(pass_rate, 4),
            weighted_score=round(weighted, 4),
            per_tier_pass_rate=per_tier_rate,
        )

    def rung_scores(self, cells: list[ModelGradedCell]) -> list[ModelRungScore]:
        return [self._rung_score(rung, cells) for rung in self.rungs]

    def separation(self, scores: list[ModelRungScore]) -> ModelSeparationVerdict:
        ordered = sorted(scores, key=lambda s: s.order)
        floor = ordered[0]
        ceiling = ordered[-1]
        margin = round(ceiling.weighted_score - floor.weighted_score, 4)
        weighted_seq = [s.weighted_score for s in ordered]
        monotonic = all(b >= a - 1e-9 for a, b in pairwise(weighted_seq))

        reasons: list[str] = []
        separated = True
        if ceiling.weighted_score <= floor.weighted_score:
            separated = False
            reasons.append(
                f"ceiling {ceiling.rung_id} ({ceiling.weighted_score}) does not exceed "
                f"floor {floor.rung_id} ({floor.weighted_score})"
            )
        if margin < self.required_margin:
            separated = False
            reasons.append(
                f"separation margin {margin} < required {self.required_margin}"
            )
        return ModelSeparationVerdict(
            floor_rung_id=floor.rung_id,
            ceiling_rung_id=ceiling.rung_id,
            floor_score=floor.weighted_score,
            ceiling_score=ceiling.weighted_score,
            margin=margin,
            required_margin=self.required_margin,
            separated=separated,
            monotonic_nondecreasing=monotonic,
            reasons=tuple(reasons),
        )

    def build_packet(self) -> ModelBenchmarkPacket:
        cells = self.grade_all()
        scores = self.rung_scores(cells)
        verdict = self.separation(scores)

        failures: list[str] = []
        # Structural guards: an escalating-complexity benchmark must actually span
        # the difficulty tiers and cover every rung, else "separation" is vacuous.
        tiers_present = {t.benchmark_tier for t in self.tasks}
        missing_tiers = sorted(
            t.value for t in EnumBenchmarkTier if t not in tiers_present
        )
        if missing_tiers:
            failures.append(f"corpus missing difficulty tiers: {missing_tiers}")
        if len(self.rungs) < 2:
            failures.append("ladder needs >= 2 rungs to prove separation")
        missing_cells = [c for c in cells if not c.output_recorded]
        if missing_cells:
            failures.append(
                f"{len(missing_cells)} (rung,task) cells have no recorded output"
            )
        if not verdict.separated:
            failures.extend(verdict.reasons)

        return ModelBenchmarkPacket(
            rungs=self.rungs,
            n_tasks=len(self.tasks),
            tiers=sorted(t.value for t in tiers_present),
            cells=cells,
            rung_scores=scores,
            separation=verdict,
            fixture_source=str(DEFAULT_FIXTURES_PATH.relative_to(_REPO_ROOT)),
            corpus_source=str(DEFAULT_CORPUS_PATH.relative_to(_REPO_ROOT)),
            rungs_source=str(DEFAULT_RUNGS_PATH.relative_to(_REPO_ROOT)),
            passed=not failures,
            failures=failures,
        )


def build_benchmark_packet(
    *,
    rungs_path: Path = DEFAULT_RUNGS_PATH,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    fixtures_path: Path = DEFAULT_FIXTURES_PATH,
    required_margin: float = DEFAULT_REQUIRED_MARGIN,
) -> ModelBenchmarkPacket:
    """Load committed config + recorded outputs and produce the evidence packet."""

    harness = GradedLadderHarness(
        load_rungs(rungs_path),
        load_corpus(corpus_path),
        load_recorded_outputs(fixtures_path),
        required_margin=required_margin,
    )
    return harness.build_packet()
