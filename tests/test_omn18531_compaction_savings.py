# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18531: compaction counted as token savings, beside delegation savings.

The measure (``HandlerCompactionSavings``): a compaction the mod answered saves
the context the model compaction would have read plus the summary it would
have written; a compaction the mod shortened saves the difference; a native
compaction saves nothing and writes no row. The deployed writer
(``SavingsProjectionRunner``) reads the source off PostCompact on the hook-event
stream and lands the row under the registry tenant UUID, the way delegation
rows land.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_savings.handlers.handler_compaction_savings import (
    COMPACTION_MOD_MODEL,
    COMPACTION_TASK_TYPE,
    EnumCompactionOutcome,
    EnumCompactionSavingsSkip,
    HandlerCompactionSavings,
    ModelCompactionSavingsSource,
    compaction_source_from_post_compact,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG, HOUSE_TENANT_UUID

_CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_projection_savings/contract.yaml"
)
# Priced in the manifest at 0.002 in / 0.01 out per 1k tokens.
_PRICED_MODEL = "claude-sonnet-5-5"
_TS = datetime(2026, 10, 15, 4, 1, 16, tzinfo=UTC)


def _hook_topic() -> str:
    topics = yaml.safe_load(_CONTRACT.read_text())["event_bus"]["subscribe_topics"]
    matches = [t for t in topics if t.endswith(".hook-event.v1")]
    assert len(matches) == 1, topics
    return str(matches[0])


def _source(**overrides: Any) -> ModelCompactionSavingsSource:
    fields: dict[str, Any] = {
        "compaction_id": "c-1",
        "claude_session_id": "s-1",
        "event_timestamp": _TS,
        "trigger": "auto",
        "outcome": EnumCompactionOutcome.MOD_ANSWERED,
        "session_model": _PRICED_MODEL,
        "context_tokens": 100_000,
    }
    fields.update(overrides)
    return ModelCompactionSavingsSource(**fields)


def _post_compact(
    *,
    summary_length: int | None,
    context_tokens: int | None = None,
    model: str | None = None,
    hook_event_name: str = "PostCompact",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"hook_event_name": hook_event_name, "trigger": "manual"}
    if summary_length is not None:
        payload["compact_summary_ref"] = {
            "field": "compact_summary",
            "length": summary_length,
            "sha256": "0" * 64,
            "content_record_id": str(uuid4()),
        }
    if context_tokens is not None:
        payload["context_tokens"] = context_tokens
    if model is not None:
        payload["model"] = model
    return {
        "event_id": str(uuid4()),
        "schema_version": "1.0.0",
        "hook_event_name": hook_event_name,
        "emitted_at": "2026-10-15T04:01:16.352869+00:00",
        "actor": "claude",
        "host": "h101",
        "lineage": {"session_id": "4fb6b47e-ec68-4a15-ab71-0de49ccbcec6"},
        "payload": payload,
        "content_refs": [],
    }


def _runner() -> tuple[SavingsProjectionRunner, AsyncMock]:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    # The mirror holds no row, so the closed legacy UUID answers (OMN-19438).
    db.fetchval = AsyncMock(return_value=None)
    runner = SavingsProjectionRunner()
    runner._db = db
    return runner, db


def _inserts(db: AsyncMock) -> list[Any]:
    return [
        call
        for call in db.execute.await_args_list
        if "INSERT INTO savings_estimates" in str(call.args[0])
    ]


def _meta() -> MessageMeta:
    return MessageMeta(
        topic=_hook_topic(), partition=0, offset=7, fallback_id=str(uuid4())
    )


@pytest.mark.unit
class TestTheMeasure:
    def test_native_compaction_counts_zero_and_writes_no_row(self) -> None:
        result = HandlerCompactionSavings().handle(
            _source(
                outcome=EnumCompactionOutcome.NATIVE,
                model_input_tokens=100_000,
                model_output_tokens=8_000,
                counterfactual_output_tokens=8_000,
            )
        )
        assert result.tokens_saved == 0
        assert result.row is None
        assert result.skipped is EnumCompactionSavingsSkip.NO_SAVING

    def test_mod_answered_saves_the_context_it_would_have_read(self) -> None:
        result = HandlerCompactionSavings().handle(_source())
        assert result.tokens_saved == 100_000
        row = result.row
        assert row is not None
        assert row["task_type"] == COMPACTION_TASK_TYPE
        assert row["model_local"] == COMPACTION_MOD_MODEL
        assert row["model_cloud_baseline"] == _PRICED_MODEL
        assert row["session_id"] == "c-1"
        assert row["cloud_cost_usd"] == Decimal("0.200000")
        assert row["local_cost_usd"] == Decimal("0.000000")
        assert row["savings_usd"] == Decimal("0.200000")
        assert row["prompt_tokens"] == 100_000
        assert row["completion_tokens"] == 0
        assert row["savings_method"] == "measured"
        assert row["usage_source"] == "measured"

    def test_mod_answered_adds_a_measured_summary(self) -> None:
        result = HandlerCompactionSavings().handle(
            _source(counterfactual_output_tokens=10_000)
        )
        assert result.tokens_saved == 110_000
        assert result.row is not None
        assert result.row["cloud_cost_usd"] == Decimal("0.300000")
        assert result.row["savings_usd"] == Decimal("0.300000")
        assert result.row["completion_tokens"] == 10_000

    def test_mod_shortened_saves_the_difference(self) -> None:
        result = HandlerCompactionSavings().handle(
            _source(
                outcome=EnumCompactionOutcome.MOD_SHORTENED,
                model_input_tokens=40_000,
                model_output_tokens=5_000,
                counterfactual_output_tokens=5_000,
            )
        )
        assert result.tokens_saved == 60_000
        row = result.row
        assert row is not None
        assert row["model_local"] == _PRICED_MODEL
        assert row["cloud_cost_usd"] == Decimal("0.250000")
        assert row["local_cost_usd"] == Decimal("0.130000")
        assert row["savings_usd"] == Decimal("0.120000")
        assert row["savings_usd"] == row["cloud_cost_usd"] - row["local_cost_usd"]

    def test_shortening_that_saves_nothing_writes_no_row(self) -> None:
        result = HandlerCompactionSavings().handle(
            _source(
                outcome=EnumCompactionOutcome.MOD_SHORTENED,
                model_input_tokens=100_000,
            )
        )
        assert result.row is None
        assert result.skipped is EnumCompactionSavingsSkip.NO_SAVING

    def test_uncaptured_context_size_writes_no_row(self) -> None:
        result = HandlerCompactionSavings().handle(_source(context_tokens=None))
        assert result.row is None
        assert result.skipped is EnumCompactionSavingsSkip.CONTEXT_SIZE_NOT_CAPTURED

    def test_uncaptured_session_model_writes_no_row(self) -> None:
        result = HandlerCompactionSavings().handle(_source(session_model=None))
        assert result.row is None
        assert result.tokens_saved == 100_000
        assert result.skipped is EnumCompactionSavingsSkip.SESSION_MODEL_NOT_CAPTURED

    def test_unpriced_session_model_is_unresolved_not_zero(self) -> None:
        result = HandlerCompactionSavings().handle(
            _source(session_model="model-not-in-the-manifest")
        )
        assert result.row is None
        assert result.tokens_saved == 100_000
        assert result.skipped is EnumCompactionSavingsSkip.SESSION_MODEL_UNPRICED


@pytest.mark.unit
class TestTheSourceIsReadOffPostCompact:
    def test_other_hook_events_are_not_sources(self) -> None:
        assert (
            compaction_source_from_post_compact(
                _post_compact(summary_length=None, hook_event_name="PreCompact")
            )
            is None
        )

    def test_a_written_summary_is_a_native_compaction(self) -> None:
        source = compaction_source_from_post_compact(
            _post_compact(summary_length=10171)
        )
        assert source is not None
        assert source.outcome is EnumCompactionOutcome.NATIVE

    def test_an_empty_summary_is_the_mods_answer(self) -> None:
        record = _post_compact(summary_length=0, context_tokens=2712, model="m")
        source = compaction_source_from_post_compact(record)
        assert source is not None
        assert source.outcome is EnumCompactionOutcome.MOD_ANSWERED
        assert source.context_tokens == 2712
        assert source.session_model == "m"
        assert source.compaction_id == record["event_id"]
        assert source.claude_session_id == "4fb6b47e-ec68-4a15-ab71-0de49ccbcec6"
        assert source.host == "h101"
        assert source.trigger == "manual"

    def test_a_missing_summary_reference_is_not_read_as_the_mod(self) -> None:
        source = compaction_source_from_post_compact(_post_compact(summary_length=None))
        assert source is not None
        assert source.outcome is EnumCompactionOutcome.NATIVE


@pytest.mark.unit
class TestTheDeployedWriter:
    def test_contract_subscribes_the_hook_event_stream(self) -> None:
        assert _hook_topic() == "onex.evt.omniclaude.hook-event.v1"

    def test_other_hook_events_touch_nothing(self) -> None:
        runner, db = _runner()
        ok = asyncio.run(
            runner.project_event(
                _hook_topic(),
                _post_compact(summary_length=None, hook_event_name="PreToolUse"),
                _meta(),
            )
        )
        assert ok is True
        assert db.execute.await_count == 0
        assert db.fetchval.await_count == 0

    def test_native_post_compact_writes_nothing(self) -> None:
        runner, db = _runner()
        ok = asyncio.run(
            runner.project_event(
                _hook_topic(),
                _post_compact(
                    summary_length=10171, context_tokens=600_000, model=_PRICED_MODEL
                ),
                _meta(),
            )
        )
        assert ok is True
        assert db.execute.await_count == 0

    def test_mod_answered_post_compact_lands_under_the_registry_uuid(self) -> None:
        runner, db = _runner()
        record = _post_compact(
            summary_length=0, context_tokens=100_000, model=_PRICED_MODEL
        )
        ok = asyncio.run(runner.project_event(_hook_topic(), record, _meta()))
        assert ok is True
        inserts = _inserts(db)
        assert len(inserts) == 1
        call = inserts[0]
        assert call.kwargs["tenant"] == str(HOUSE_TENANT_UUID)
        args = call.args[1:]
        assert args[1] == record["event_id"]
        assert args[2] == COMPACTION_MOD_MODEL
        assert args[3] == _PRICED_MODEL
        assert args[6] == Decimal("0.200000")
        assert args[9] == COMPACTION_TASK_TYPE
        assert args[10] == 100_000
        assert args[15] == str(HOUSE_TENANT_UUID)
        assert HOUSE_TENANT_SLUG not in args
