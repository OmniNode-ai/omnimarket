# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4: the data file names the live hypotheses, safeguards and denominators."""

from __future__ import annotations

from omnimarket.nodes.node_projection_compaction_ab.models import (
    ModelAbRow,
    ModelLiveMeasurementSpec,
    load_live_spec,
)


def _problems(spec: ModelLiveMeasurementSpec) -> list[str]:
    problems: list[str] = []
    all_cols = {c for w in spec.writers.values() for c in w.columns}
    measures = (*spec.live_hypotheses, *spec.live_safeguards)
    referenced: set[str] = set()
    for m in measures:
        referenced.add(m.numerator_column)
        if m.numerator_column not in spec.writers["scores"].columns:
            problems.append(f"{m.id}: numerator not a scores column")
        if m.denominator != "row":
            if m.denominator not in all_cols:
                problems.append(f"{m.id}: denominator {m.denominator} unreadable")
            referenced.add(m.denominator)
    for column in spec.writers["scores"].columns:
        if column not in referenced:
            problems.append(f"{column}: live column with no denominator")
    return problems


def test_live_denominators() -> None:
    spec = load_live_spec()
    assert _problems(spec) == []
    assert [h.id for h in spec.live_hypotheses] == ["HL1", "HL2"]
    assert set(ModelAbRow.model_fields) >= {
        m.numerator_column for m in (*spec.live_hypotheses, *spec.live_safeguards)
    }
    live_ids = {m.id for m in spec.live_safeguards}
    assert live_ids == {
        "kill_switch_incident_rate",
        "hold_scope_violation_rate",
        "s5_cost_ratio",
        "s3_defect_rate_per_merged_pr",
    }
    assert set(spec.replay_only) == {"s1_judged_recall", "planted_stratum"}
    assert not live_ids & set(spec.replay_only)


def test_live_denominators_falsifiers_fire() -> None:
    spec = load_live_spec()
    orphan = spec.model_copy(
        update={
            "writers": {
                **spec.writers,
                "scores": spec.writers["scores"].model_copy(
                    update={
                        "columns": (*spec.writers["scores"].columns, "extra_live_col")
                    }
                ),
            }
        }
    )
    assert any("extra_live_col" in p for p in _problems(orphan))
    unreadable = spec.model_copy(
        update={
            "live_safeguards": (
                spec.live_safeguards[0].model_copy(update={"denominator": "nope"}),
            )
        }
    )
    assert any("nope" in p for p in _problems(unreadable))
