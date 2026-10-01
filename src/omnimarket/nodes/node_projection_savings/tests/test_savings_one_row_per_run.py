# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20303 AC1: one delegation run states its saving in ONE savings_estimates row.

A run emits two terminals, the delegate-skill terminal and the canonical
delegation terminal, and the savings writers read both. Each derives its
``event_timestamp`` from its own terminal time, truncated to the second, and
that time is part of the table's identity key, so terminals one second apart
landed as two rows for one run: on the .201 dev lane on 2026-10-01, 596 rows
for 522 runs over 24h and 381.17 USD summed against 313.95 USD with one row
per run.

The fold below decides the identity a run's row is written under: the run's
stored row when there is one, else the incoming terminal's own. Both writers
-- the sync handler and the deployed async runner -- read the run's stored
identity, fold, and upsert under what the fold returns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings_run_identity_fold import (
    HandlerSavingsRunIdentityFold,
    ModelSavingsRunIdentity,
    ModelSavingsRunIdentityFoldRequest,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta

RUN = "727c0456-1758-421a-8600-25da6410f2cf"
DELEGATE_SKILL_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
CANONICAL_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
FIRST = "2026-10-01T12:23:58.700000+00:00"
SECOND = "2026-10-01T12:23:59.100000+00:00"


def _cf_cost() -> float:
    cf = build_premium_counterfactual(prompt_tokens=319, completion_tokens=154)
    assert cf is not None
    return float(cf.counterfactual_cost_usd)


def _delegate_skill_payload(timestamp: str) -> dict[str, object]:
    return {
        "_event_type": "delegate-skill-completed",
        "status": "completed",
        "correlation_id": RUN,
        "task_type": "summarization",
        "provider": "local",
        "model_name": "Qwen3.8-27B",
        "quality_gate_passed": True,
        "emitted_at": timestamp,
        "metrics": {
            "input_tokens": 319,
            "output_tokens": 154,
            "total_tokens": 473,
            "cost_usd": 0.0,
            "cost_savings_usd": _cf_cost(),
            "latency_ms": 900,
        },
    }


def _canonical_payload(timestamp: str) -> dict[str, object]:
    return {
        "_event_type": CANONICAL_TOPIC,
        "correlation_id": RUN,
        "task_type": "summarization",
        "model_used": "Qwen3.8-27B",
        "quality_passed": True,
        "cumulative_attempt_cost": 0.0,
        "final_attempt_cost": 0.0,
        "cumulative_input_tokens": 319,
        "cumulative_output_tokens": 154,
        "prompt_tokens": 319,
        "completion_tokens": 154,
        "timestamp": timestamp,
    }


def _identity(at: str, model_local: str = "Qwen3.8-27B") -> ModelSavingsRunIdentity:
    return ModelSavingsRunIdentity(
        event_timestamp=datetime.fromisoformat(at),
        model_local=model_local,
        model_cloud_baseline="claude-opus-4-6",
    )


@pytest.mark.unit
class TestSavingsRunIdentityFold:
    def test_a_run_with_no_stored_row_keeps_the_incoming_identity(self) -> None:
        incoming = _identity(SECOND)
        folded = HandlerSavingsRunIdentityFold().handle(
            ModelSavingsRunIdentityFoldRequest(incoming=incoming, stored=())
        )
        assert folded == incoming

    def test_a_run_with_a_stored_row_takes_the_stored_identity(self) -> None:
        stored = _identity(FIRST)
        folded = HandlerSavingsRunIdentityFold().handle(
            ModelSavingsRunIdentityFoldRequest(
                incoming=_identity(SECOND), stored=(stored,)
            )
        )
        assert folded == stored

    def test_historical_duplicates_converge_on_the_earliest_row(self) -> None:
        # A run that already holds two rows (written before this fix) gets every
        # later terminal folded onto ONE of them, never a third.
        earliest = _identity(FIRST)
        later = _identity(SECOND)
        folded = HandlerSavingsRunIdentityFold().handle(
            ModelSavingsRunIdentityFoldRequest(
                incoming=_identity("2026-10-01T12:24:05+00:00"),
                stored=(later, earliest),
            )
        )
        assert folded == earliest


@pytest.mark.unit
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_delegate_skill_payload(FIRST), _canonical_payload(SECOND)),
        (_canonical_payload(FIRST), _delegate_skill_payload(SECOND)),
    ],
    ids=["delegate_skill_then_canonical", "canonical_then_delegate_skill"],
)
def test_sync_handler_writes_one_row_per_run(
    first: dict[str, object], second: dict[str, object]
) -> None:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionSavings()
    assert handler.handle({**first, "_db": db})["rows_upserted"] == 1
    assert handler.handle({**second, "_db": db})["rows_upserted"] == 1

    rows = db.query("savings_estimates", {"session_id": RUN})
    assert len(rows) == 1
    assert Decimal(str(rows[0]["savings_usd"])) == Decimal(str(_cf_cost()))


class _StoredSavingsDb:
    """An asyncpg double holding the savings_estimates identities of one run.

    It answers the run-identity read with what the rows it holds say, and
    records every INSERT so the test can read the identity it was written
    under. The tenant-registry reads answer nothing, so the house tenant is
    stamped, as in every other runner test in this repo.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.db = MagicMock(spec=AsyncpgAdapter)
        self.db.fetchval = AsyncMock(return_value=None)
        self.db.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, sql: str, *args: Any, **_: Any) -> list[dict[str, Any]]:
        if sql.lstrip().upper().startswith("SELECT") and "savings_estimates" in sql:
            if "session_id = $1" in sql:
                return [
                    {
                        "event_timestamp": row["event_timestamp"],
                        "model_local": row["model_local"],
                        "model_cloud_baseline": row["model_cloud_baseline"],
                    }
                    for row in self.rows
                    if row["session_id"] == args[0]
                ]
            return []
        if "INSERT INTO savings_estimates" in sql:
            key = (args[1], args[0], args[2], args[3])
            if not any(
                (
                    r["session_id"],
                    r["event_timestamp"],
                    r["model_local"],
                    r["model_cloud_baseline"],
                )
                == key
                for r in self.rows
            ):
                self.rows.append(
                    {
                        "session_id": args[1],
                        "event_timestamp": args[0],
                        "model_local": args[2],
                        "model_cloud_baseline": args[3],
                    }
                )
            return []
        return []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            (DELEGATE_SKILL_TOPIC, _delegate_skill_payload(FIRST)),
            (CANONICAL_TOPIC, _canonical_payload(SECOND)),
        ),
        (
            (CANONICAL_TOPIC, _canonical_payload(FIRST)),
            (DELEGATE_SKILL_TOPIC, _delegate_skill_payload(SECOND)),
        ),
    ],
    ids=["delegate_skill_then_canonical", "canonical_then_delegate_skill"],
)
async def test_async_runner_writes_one_row_per_run(
    first: tuple[str, dict[str, object]], second: tuple[str, dict[str, object]]
) -> None:
    store = _StoredSavingsDb()
    runner = SavingsProjectionRunner(publish_fn=AsyncMock())
    runner._db = store.db  # noqa: SLF001 — the runner's pool, replaced by the double

    for offset, (topic, payload) in enumerate((first, second)):
        data = {k: v for k, v in payload.items() if k != "_event_type"}
        meta = MessageMeta(partition=0, offset=offset, fallback_id=RUN, topic=topic)
        assert await runner.project_event(topic, data, meta)

    assert len(store.rows) == 1
    assert store.rows[0]["event_timestamp"] == datetime(
        2026, 10, 1, 12, 23, 58, tzinfo=UTC
    )
