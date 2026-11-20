# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the Model tests tab's compute: snapshots built from run-rate rows (OMN-24174)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from omnimarket.enums.enum_eval_run_state import EnumEvalRunState
from omnimarket.models.eval_item.model_eval_snapshot import (
    ModelEvalSnapshot,
    ModelEvalSnapshotLiveWeek,
    ModelEvalSnapshotModel,
    ModelEvalSnapshotRate,
    ModelEvalSnapshotRun,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_overlay import (
    ModelEvalBoardOverlay,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_request import (
    ModelEvalBoardRequest,
)

LOCAL_ID = "qwen-local-coder-27b"
HOSTED_ID = "hosted-coder-5"
FIRST_TEST = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
WEEK = timedelta(days=7)
PUBLISHED = datetime(2026, 11, 14, 16, 50, tzinfo=UTC)
_Z = 1.959963984540054


def wilson(passed: int, n: int) -> tuple[float, float]:
    p = passed / n
    denominator = 1 + _Z**2 / n
    center = (p + _Z**2 / (2 * n)) / denominator
    half = _Z * math.sqrt(p * (1 - p) / n + _Z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def rate(band: str, n: int, passed: int) -> ModelEvalSnapshotRate:
    low, high = wilson(passed, n) if n else (0.0, 0.0)
    return ModelEvalSnapshotRate(
        size_band=band,
        n=n,
        passed=passed,
        unscorable_model=0,
        unscorable_infrastructure=0,
        wilson_low=low,
        wilson_high=high,
    )


def rates(
    small: tuple[int, int] = (89, 85),
    medium: tuple[int, int] | None = (89, 80),
    large: tuple[int, int] | None = None,
) -> tuple[ModelEvalSnapshotRate, ...]:
    """One overall row and one row per size; the overall counts add the sizes."""
    bands = {"S": small, "M": medium, "L": large}
    held = {band: counts for band, counts in bands.items() if counts is not None}
    n = sum(counts[0] for counts in held.values())
    passed = sum(counts[1] for counts in held.values())
    return (
        rate("all", n, passed),
        *(rate(band, *counts) for band, counts in held.items()),
    )


def run(
    run_id: str,
    at: datetime,
    *,
    state: EnumEvalRunState = EnumEvalRunState.CLOSED,
    rows: tuple[ModelEvalSnapshotRate, ...] | None = None,
    sample: str = "sample-a",
    revision: str = "rev-1",
    settings: str = "settings-1",
    prompt: str = "prompt-1",
    scorer: str = "scorer-1",
    engine: str = "local",
    context_arm: str = "",
    replicate: int = 0,
    void_reason: str = "",
    band_usable: dict[str, int] | None = None,
    band_target: dict[str, int] | None = None,
) -> ModelEvalSnapshotRun:
    return ModelEvalSnapshotRun(
        run_id=run_id,
        state=state,
        series="weekly",
        engine=engine,
        task_sample_digest=sample,
        model_revision=revision,
        generation_settings_hash=settings,
        prompt_template_version=prompt,
        scorer_version=scorer,
        context_arm=context_arm,
        replicate=replicate,
        first_scored_at=at,
        last_scored_at=at,
        void_reason=void_reason,
        band_usable=band_usable or {},
        band_target=band_target or {},
        rates=() if state is EnumEvalRunState.VOID else (rows or rates()),
    )


def live_week(week: str, n: int, passed: int) -> ModelEvalSnapshotLiveWeek:
    low, high = wilson(passed, n) if n else (0.0, 0.0)
    return ModelEvalSnapshotLiveWeek(
        week=week,
        n=n,
        passed=passed,
        wilson_low=low,
        wilson_high=high,
        first_scored_at=FIRST_TEST,
        last_scored_at=FIRST_TEST,
        reviewer_version_count=1,
        reviewer_versions_digest="d" * 64,
    )


def model(
    model_id: str,
    runs: tuple[ModelEvalSnapshotRun, ...] = (),
    live: tuple[ModelEvalSnapshotLiveWeek, ...] = (),
) -> ModelEvalSnapshotModel:
    return ModelEvalSnapshotModel(model_id=model_id, runs=runs, live_weeks=live)


def overlay(**overrides: object) -> ModelEvalBoardOverlay:
    document: dict[str, object] = {
        "display_names": {
            LOCAL_ID: "Local coding model (Qwen)",
            HOSTED_ID: "Hosted coding model",
        },
        "max_models": 4,
        "min_n_point": 15,
        "trend_look_points": 10,
        "glossary": {
            "task": "One past change from our own repositories, given to the model as the ticket described it.",
            "pass": "Every check that ticket required passes once the model's change is applied.",
            "size": "Small, medium and large are measured from what the model is given.",
            "likely range": "The 95 percent range of rates that fit the result.",
        },
        "size_line": {"floor": 0.9, "margin": 0.07, "confidence": 0.95, "power": 0.8},
    }
    document.update(overrides)
    return ModelEvalBoardOverlay.model_validate(document)


def request(
    *models: ModelEvalSnapshotModel, **overlay_overrides: object
) -> ModelEvalBoardRequest:
    return ModelEvalBoardRequest(
        snapshot=ModelEvalSnapshot(
            published_at=PUBLISHED, tenant_id="lab", models=tuple(models)
        ),
        overlay=overlay(**overlay_overrides),
    )


def golden_request() -> ModelEvalBoardRequest:
    """The recorded snapshot the golden file is cut from: two models, markers, a void run and live weeks."""
    local = model(
        LOCAL_ID,
        (
            run("g1", FIRST_TEST, rows=rates((89, 52), (89, 60))),
            run("g2", FIRST_TEST + WEEK, rows=rates((89, 55), (89, 63))),
            run(
                "g3",
                FIRST_TEST + 2 * WEEK,
                revision="rev-2",
                rows=rates((89, 58), (89, 66)),
            ),
            run(
                "g4",
                FIRST_TEST + 3 * WEEK,
                state=EnumEvalRunState.VOID,
                void_reason="registration_mismatch",
            ),
            run(
                "g5",
                FIRST_TEST + 4 * WEEK,
                sample="sample-b",
                revision="rev-2",
                rows=rates((89, 87), (89, 70), (40, 20)),
                band_usable={"S": 89, "M": 89, "L": 40},
                band_target={"S": 89, "M": 89, "L": 89},
            ),
        ),
        (live_week("live-2026-W44", 52, 41), live_week("live-2026-W45", 9, 4)),
    )
    hosted = model(
        HOSTED_ID,
        (
            run(
                "h1",
                FIRST_TEST + 4 * WEEK,
                engine="glm",
                rows=rates((9, 7), None, None),
            ),
        ),
    )
    return request(local, hosted)
