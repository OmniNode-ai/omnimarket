# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171 AC4: the snapshot carries only tracking runs, fits its bound at the retention limits, and refuses typed."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_snapshot import (
    build_snapshot,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_RUN_CLOSED,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
    ModelEvalRateRow,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    EnumEvalSnapshotRefusal,
    EvalSnapshotRefusedError,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    make_writer,
    overlay,
    try_row,
)

pytestmark = pytest.mark.unit
_TRACKED = "00000000-0000-0000-0000-0000000000a1"
_VOID = "00000000-0000-0000-0000-0000000000b2"
_OTHER = "00000000-0000-0000-0000-0000000000c3"
_LONG = "x" * 200


async def test_only_tracking_runs_reach_the_snapshot_and_a_void_run_shows_its_state_and_cause() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    for run, series, void in (
        (_TRACKED, "tracking", None),
        (_VOID, "tracking", "registration_mismatch"),
        (_OTHER, "", None),
    ):
        await deliver(
            writer,
            TOPIC_CODE_TRY,
            code_event(
                run, try_row(run, "t1"), try_row(run, "t2", verdict="fail_check")
            ),
            at(2),
        )
        await deliver(
            writer,
            TOPIC_RUN_CLOSED,
            close_event(
                run,
                (("t1", MODEL, "S"), ("t2", MODEL, "S")),
                series=series,
                void_reason=void,
                closed_at=at(30),
            ),
            at(3),
        )
    # The non-tracking run is in the tables, with a closed state and a rate.
    assert any(
        r.run_id == _OTHER and r.run_state is EnumEvalRunState.CLOSED
        for r in store.rates
    )
    _, snapshot = published[-1]
    runs = {r["run_id"]: r for r in snapshot["models"][0]["runs"]}
    assert set(runs) == {_TRACKED, _VOID}
    assert runs[_TRACKED]["state"] == "closed"
    assert runs[_TRACKED]["rates"]
    void = runs[_VOID]
    assert (void["state"], void["void_reason"], void["rates"]) == (
        "void",
        "registration_mismatch",
        [],
    )
    assert void["last_scored_at"] == "2026-11-09T12:30:00Z"


def _rate(
    model: str,
    run: str,
    band: str,
    *,
    state: EnumEvalRunState,
    minutes: int,
    text: str,
    series: str = "tracking",
    live: bool = False,
) -> ModelEvalRateRow:
    return ModelEvalRateRow(
        tenant_id=TENANT,
        run_id=run,
        scorer=EnumEvalScorer.REVIEWER_MODEL
        if live
        else EnumEvalScorer.CONTRACT_CHECKS,
        task_set_digest="" if live else text,
        engine=None if live else EnumEvalEngine.LOCAL,
        model_id=model,
        model_revision="" if live else text,
        generation_settings_hash="" if live else text,
        prompt_template_version="" if live else text,
        scorer_version="" if live else text,
        context_arm="" if live else text,
        replicate=0,
        size_band=band,
        n=89,
        passed=60,
        unscorable_model=3,
        unscorable_infrastructure=0,
        wilson_low=0.5,
        wilson_high=0.8,
        first_scored_at=at(minutes),
        last_scored_at=at(minutes + 5),
        series=series,
        run_state=state,
        void_reason="infrastructure_unscorable_after_retries"
        if state is EnumEvalRunState.VOID
        else "",
        task_sample_digest=text,
        band_usable={"S": 130, "M": 136, "L": 4},
        band_target={"S": 89, "M": 89, "L": 89},
        item_count=178,
        scorer_versions=tuple(f"v{i}" for i in range(40)) if live else (),
    )


def _worst_case(max_chars: int = 200) -> tuple[Any, list[ModelEvalRateRow]]:
    """Eight models, each with more runs than it keeps, 50 task sets, 30 void runs, and live weeks of 40 reviewer versions."""
    models = [f"m{i}" + "m" * (max_chars - 2 - len(str(i))) + "" for i in range(8)]
    models = [m[:max_chars] for m in models]
    rows: list[ModelEvalRateRow] = []
    for m_index, model in enumerate(models):
        for run_index in range(60):
            void = run_index % 2 == 1 and run_index < 60  # 30 void runs per model
            run = f"{m_index}-{run_index:03d}-" + "r" * (max_chars - 8)
            text = f"s{run_index % 50}-" + "t" * (max_chars - 4)
            state = EnumEvalRunState.VOID if void else EnumEvalRunState.CLOSED
            for band in ("S", "M", "L", "all"):
                rows.append(
                    _rate(
                        model, run, band, state=state, minutes=run_index * 10, text=text
                    )
                )
        for week in range(40):
            rows.append(
                _rate(
                    model,
                    f"live-2026-W{week:02d}" + "w" * (max_chars - 14),
                    "all",
                    state=EnumEvalRunState.LIVE,
                    minutes=week,
                    text="",
                    series="",
                    live=True,
                )
            )
    return overlay(
        display_names={m: m for m in models}, max_field_chars=max_chars
    ), rows


def test_worst_case_snapshot_fits_under_the_bound() -> None:
    ov, rows = _worst_case()
    snapshot = build_snapshot(
        tenant_id=TENANT, published_at=at(1000), rates=rows, records=(), overlay=ov
    )
    assert len(snapshot.models) == ov.max_models == 8
    for model in snapshot.models:
        assert len(model.runs) == ov.snapshot_runs == 26
        assert len(model.live_weeks) == ov.live_weeks == 26
        closed = [r for r in model.runs if r.state is EnumEvalRunState.CLOSED]
        assert all(len(r.rates) == 4 for r in closed)
        assert closed
        assert all(
            r.rates == () for r in model.runs if r.state is EnumEvalRunState.VOID
        )
    size = len(snapshot.model_dump_json().encode("utf-8"))
    assert size < 900_000
    assert size > 400_000, (
        f"the worst case is not worst enough to mean anything: {size}"
    )


def test_an_oversized_snapshot_is_refused_with_its_size_never_cut() -> None:
    ov, rows = _worst_case()
    tight = ov.model_copy(update={"snapshot_max_bytes": 100_000})
    with pytest.raises(EvalSnapshotRefusedError) as refusal:
        build_snapshot(
            tenant_id=TENANT,
            published_at=at(1000),
            rates=rows,
            records=(),
            overlay=tight,
        )
    assert refusal.value.kind is EnumEvalSnapshotRefusal.OVER_BOUND
    assert refusal.value.limit == 100_000
    assert refusal.value.size > 100_000


def test_a_201_character_model_id_is_refused_typed() -> None:
    model = "m" * 201
    ov = overlay(display_names={model: "long"})
    rows = [
        _rate(model, "run-1", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t")
    ]
    with pytest.raises(EvalSnapshotRefusedError) as refusal:
        build_snapshot(
            tenant_id=TENANT, published_at=at(5), rates=rows, records=(), overlay=ov
        )
    assert refusal.value.kind is EnumEvalSnapshotRefusal.FIELD_TOO_LONG
    assert refusal.value.size == 201
    assert refusal.value.limit == 200


def test_a_200_character_text_field_is_accepted_and_a_201_one_refused() -> None:
    ov = overlay(display_names={"m": "m"})
    ok = _rate(
        "m", "run-1", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t" * 200
    )
    build_snapshot(
        tenant_id=TENANT, published_at=at(5), rates=[ok], records=(), overlay=ov
    )
    bad = _rate(
        "m", "run-1", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t" * 201
    )
    with pytest.raises(EvalSnapshotRefusedError):
        build_snapshot(
            tenant_id=TENANT, published_at=at(5), rates=[bad], records=(), overlay=ov
        )


def test_models_outside_the_display_overlay_are_not_on_the_snapshot() -> None:
    ov = overlay(display_names={"shown": "Shown"})
    rows = [
        _rate(
            "shown", "run-1", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t"
        ),
        _rate(
            "hidden", "run-2", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t"
        ),
    ]
    snapshot = build_snapshot(
        tenant_id=TENANT, published_at=at(5), rates=rows, records=(), overlay=ov
    )
    assert [m.model_id for m in snapshot.models] == ["shown"]


def test_run_times_never_come_from_the_publish_time() -> None:
    ov = overlay(display_names={"m": "m"})
    rows = [
        _rate("m", "run-1", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t")
    ]
    first = build_snapshot(
        tenant_id=TENANT, published_at=at(5), rates=rows, records=(), overlay=ov
    )
    later = build_snapshot(
        tenant_id=TENANT,
        published_at=at(5) + timedelta(days=3),
        rates=rows,
        records=(),
        overlay=ov,
    )
    assert first.published_at != later.published_at
    assert first.models == later.models


def test_a_closed_run_that_is_not_a_tracking_run_is_not_on_the_snapshot() -> None:
    ov = overlay(display_names={"m": "m"})
    rows = [
        _rate(
            "m", "tracked", "all", state=EnumEvalRunState.CLOSED, minutes=1, text="t"
        ),
        _rate(
            "m",
            "stray",
            "all",
            state=EnumEvalRunState.CLOSED,
            minutes=2,
            text="t",
            series="",
        ),
        _rate(
            "m",
            "stray-void",
            "all",
            state=EnumEvalRunState.VOID,
            minutes=3,
            text="t",
            series="",
        ),
    ]
    snapshot = build_snapshot(
        tenant_id=TENANT, published_at=at(5), rates=rows, records=(), overlay=ov
    )
    assert [r.run_id for r in snapshot.models[0].runs] == ["tracked"]
