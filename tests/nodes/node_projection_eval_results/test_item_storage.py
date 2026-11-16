# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171 AC2: ordering at an equal time, and every field of the event model stored."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from itertools import permutations
from pathlib import Path

import pytest

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.enums.enum_eval_lane import EnumEvalLane
from omnimarket.enums.enum_eval_scorer import EnumEvalScorer
from omnimarket.enums.enum_eval_step_verdict import (
    EnumEvalStepVerdict,
    EnumEvalStepVerdictSource,
)
from omnimarket.enums.enum_eval_unscorable_reason import EnumEvalUnscorableReason
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.eval_item.model_eval_field_score import ModelEvalFieldScore
from omnimarket.models.eval_item.model_eval_item_scored import ModelEvalItemScored
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_store import (
    ITEM_COLUMNS,
    UPSERT_ITEM,
    item_params,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    ModelEvalProjectionResult,
    ModelEvalReadPlan,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    ModelEvalItemRow,
)
from tests.nodes.node_projection_eval_results.support import (
    TENANT,
    MemoryStore,
    at,
    code_event,
    deliver,
    make_writer,
    try_row,
)

pytestmark = pytest.mark.unit
_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_eval_results/migrations/0000_create_eval_results_tables.sql"
)


def _record(**updates: object) -> ModelEvalItemScored:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "run_id": "run-1",
        "experiment_id": "exp-1",
        "replicate": 1,
        "try_ordinal": 1,
        "task_set_id": "set",
        "task_set_digest": "digest",
        "task_id": "task-1",
        "task_type": "code",
        "size_band": EnumSizeBand.M,
        "engine": EnumEvalEngine.GLM,
        "model_id": "model",
        "model_revision": "rev",
        "generation_settings_hash": "gs",
        "prompt_template_version": "pt",
        "context_arm": "off",
        "context_pack_hash": "cp",
        "pack_profile_hash": "pp",
        "prompt_tokens": 7,
        "scorer": EnumEvalScorer.CONTRACT_CHECKS,
        "scorer_version": "sv",
        "field_scores": (ModelEvalFieldScore(name="a", passed=True),),
        "passed": None,
        "unscorable_reason": EnumEvalUnscorableReason.CHECK_COULD_NOT_RUN,
        "step_verdict": EnumEvalStepVerdict.REJECT,
        "step_verdict_source": EnumEvalStepVerdictSource.REVIEWER_MODEL,
        "scored_at": datetime(2026, 11, 9, tzinfo=UTC),
        "trace_sha256": "t" * 64,
        "lane": EnumEvalLane.STABILITY,
        "host": "h",
        "install_revision": "ir",
    }
    return ModelEvalItemScored.model_validate(values | updates)


def _table_columns(table: str) -> set[str]:
    """The columns the migration DDL really declares for ``table``."""
    block = (
        _MIGRATION.read_text()
        .split(f"CREATE TABLE IF NOT EXISTS public.{table} (", 1)[1]
        .split("PRIMARY KEY", 1)[0]
    )
    return set(re.findall(r"^\s+(\w+) [A-Z]", block, re.MULTILINE))


def _insert_columns() -> tuple[str, ...]:
    """The column list the upsert statement names, read from its SQL text."""
    head = UPSERT_ITEM.split("(", 1)[1].split(")", 1)[0]
    return tuple(c.strip() for c in head.split(","))


def test_every_field_of_the_event_model_has_a_real_column_and_is_stored() -> None:
    """Fields of ``ModelEvalItemScored`` against the migration's columns and the SQL's own insert list.

    Neither side is ``ITEM_COLUMNS``, which is derived from the stored row's own fields: a field
    dropped from the table or from the statement fails here.
    """
    record = _record()
    row = ModelEvalItemRow.of(record)
    columns = _table_columns("eval_item_scores")
    insert = _insert_columns()
    params = dict(zip(insert, item_params(row), strict=True))
    for name in ModelEvalItemScored.model_fields:
        assert name in columns, (
            f"{name} has no column in the migration: a dropped field"
        )
        assert name in params, f"{name} is not written by the upsert"
        assert getattr(row, name) == getattr(record, name), (
            f"{name} changed on its way into the row"
        )
    # No field sits at its default in this fixture, so a column that stores a constant fails here.
    for name, value in (
        ("experiment_id", "exp-1"),
        ("replicate", 1),
        ("model_revision", "rev"),
        ("scorer_version", "sv"),
        ("step_verdict", "reject"),
        ("step_verdict_source", "reviewer_model"),
        ("unscorable_reason", "check_could_not_run"),
        ("lane", "stability"),
        ("host", "h"),
        ("install_revision", "ir"),
        ("trace_sha256", "t" * 64),
        ("passed", None),
    ):
        assert params[name] == value
    assert '"name":"a"' in params["field_scores"].replace(" ", "")
    assert params["outcome_digest"] == row.outcome_digest
    assert params["item_key"] == str(record.event_id)


def test_the_migration_holds_a_column_for_every_stored_field() -> None:
    assert set(ITEM_COLUMNS) <= _table_columns("eval_item_scores")
    assert {"item_key", "outcome_digest"} <= _table_columns("eval_item_scores")


def test_outcome_digest_differs_for_every_stored_field() -> None:
    base = ModelEvalItemRow.of(_record())
    changes = {
        "passed": True,
        "unscorable_reason": EnumEvalUnscorableReason.EMPTY_ANSWER,
        "step_verdict": EnumEvalStepVerdict.ACCEPT,
        "step_verdict_source": EnumEvalStepVerdictSource.DETERMINISTIC,
        "model_revision": "other",
        "trace_sha256": "u" * 64,
    }
    seen = {base.outcome_digest}
    for name, value in changes.items():
        other = ModelEvalItemRow.of(_record(**{name: value}))
        assert other.outcome_digest not in seen, name
        seen.add(other.outcome_digest)


def test_the_lane_stamps_are_not_in_the_tie_break_digest() -> None:
    base = ModelEvalItemRow.of(_record())
    for name, value in (
        ("host", "other"),
        ("install_revision", "other"),
        ("lane", EnumEvalLane.DEV),
    ):
        other = ModelEvalItemRow.of(_record(**{name: value}))
        assert other.outcome_digest == base.outcome_digest, name
        assert not other.supersedes(base)
        assert not base.supersedes(other)


def test_equal_time_pair_leaves_the_same_row_in_either_order() -> None:
    """Differing only in the step verdict source: the greater digest stands whatever the arrival order."""
    a = ModelEvalItemRow.of(
        _record(step_verdict_source=EnumEvalStepVerdictSource.DETERMINISTIC)
    )
    b = ModelEvalItemRow.of(
        _record(step_verdict_source=EnumEvalStepVerdictSource.REVIEWER_MODEL)
    )
    assert a.scored_at == b.scored_at
    assert a.item_key == b.item_key
    assert a.outcome_digest != b.outcome_digest
    plan = ModelEvalReadPlan(tenant_id=TENANT, run_id="run-1")
    standing = []
    import asyncio

    for order in permutations((a, b)):
        store = MemoryStore()
        for row in order:
            asyncio.run(store.apply(plan, ModelEvalProjectionResult(items=(row,))))
        standing.append(next(iter(store.items.values())))
    assert standing[0] == standing[1]
    assert standing[0].outcome_digest == max(a.outcome_digest, b.outcome_digest)
    assert a.supersedes(b) != b.supersedes(a)


async def test_equal_time_events_through_the_writer_in_either_order() -> None:
    """Two code events for one item at one envelope time, differing in the verdict."""
    passed = code_event("run-1", try_row("run-1", "t1", verdict="pass"))
    failed = code_event("run-1", try_row("run-1", "t1", verdict="fail_check"))
    rows = []
    for order in ((passed, failed), (failed, passed)):
        store = MemoryStore()
        writer, _ = make_writer(store)
        for payload in order:
            await deliver(writer, TOPIC_CODE_TRY, payload, at(5))
        assert len(store.items) == 1
        rows.append(next(iter(store.items.values())))
    assert rows[0] == rows[1]


async def test_a_later_scored_at_replaces_and_an_earlier_one_never_does() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    first = code_event("run-1", try_row("run-1", "t1", verdict="fail_check"))
    retry = code_event("run-1", try_row("run-1", "t1", verdict="pass"))
    await deliver(writer, TOPIC_CODE_TRY, first, at(5))
    await deliver(writer, TOPIC_CODE_TRY, retry, at(9))
    await deliver(writer, TOPIC_CODE_TRY, first, at(5))
    (row,) = store.items.values()
    assert row.passed is True
    assert row.scored_at == at(9)
