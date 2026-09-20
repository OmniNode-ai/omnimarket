# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18903 golden chain: contract topics, six cause classes, and the writer.

The fold tests prove the derivation. These prove the CHAIN: that the contract
declares the topics the writer subscribes, that one outcome per cause class
round-trips to six rows, and that the writer half is wired the one way the
shared runtime will actually dispatch.

The writer is dispatched IN-PROCESS, once per message, through a synchronous
entry that opens its own event loop. Two consecutive messages are driven here
because one cannot expose a loop-bound resource cached across calls -- the
defect that left a sibling projection at zero rows with "Event loop is closed"
on the dev lane.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode
from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers import (
    CiAttemptOutcomeProjectionWriter,
    HandlerProjectionCiAttemptOutcome,
)
from omnimarket.nodes.node_projection_ci_attempt_outcome.models import (
    ModelCiAttemptOutcomeProjectionRequest,
)

pytestmark = pytest.mark.unit

# Declared here as literals and asserted against the contract, so the two
# cannot drift. Each carries the allow comment the topic lint needs.
_IN_TOPIC = "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1"  # onex-topic-allow: asserted against the contract
_OUT_TOPIC = "onex.evt.omnimarket.projection-ci-attempt-outcome-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-ci-attempt-outcome-malformed.v1"  # onex-topic-allow: asserted against the contract

_T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_ci_attempt_outcome"
    / "contract.yaml"
)

#: The six members, one per cause class the row can carry.
_ALL_CAUSE_CODES: tuple[str, ...] = tuple(
    str(code) for code in EnumMergeCheckReasonCode
)


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for the asyncpg adapter and enforces its loop affinity."""

    def __init__(self, *, refuse_stale: bool = False) -> None:
        self._pool: _LoopBoundPool | None = None
        self.refuse_stale = refuse_stale
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.calls.append((query, params))
        if "RETURNING" in query:
            if self.refuse_stale:
                return []
            return [{"projection_cursor": len(self.calls), "first_seen_at": _T0}]
        return []


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _writer(**kwargs: Any) -> CiAttemptOutcomeProjectionWriter:
    instance = CiAttemptOutcomeProjectionWriter()
    instance._db = _RecordingAdapter(**kwargs)  # type: ignore[assignment]
    return instance


def _event_one_check_per_cause(observed_at: datetime = _T0) -> dict[str, Any]:
    """One pull request, one check per cause class, each on its own commit."""
    shas = [f"{index}" * 40 for index in range(1, len(_ALL_CAUSE_CODES) + 1)]
    checks = [
        {
            "name": f"check-{code}",
            "conclusion": "failure",
            "reason_code": code,
            "head_sha": sha,
            "run_attempt": 1,
            "run_id": "5500",
            "failed_step_name": "a step",
            "cause_affirmative": True,
        }
        for code, sha in zip(_ALL_CAUSE_CODES, shas, strict=True)
    ]
    return {
        "repo": "OmniNode-ai/omnimarket",
        "pr_states": [
            {
                "repo": "OmniNode-ai/omnimarket",
                "pr_number": 2726,
                "title": "feat(OMN-18903): a change",
                "head_sha_history": shas,
                "check_runs": checks,
            }
        ],
        "_envelope_timestamp": observed_at,
        "_topic": _IN_TOPIC,
        "_partition": 0,
        "_offset": 1,
        "_fallback_id": "golden-chain",
    }


# --------------------------------------------------------------------------
# The declared chain.
# --------------------------------------------------------------------------


def test_the_contract_declares_the_topics_the_writer_subscribes() -> None:
    contract = _load_contract()
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [_IN_TOPIC]
    assert bus["publish_topics"] == [_OUT_TOPIC]
    assert bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _OUT_TOPIC
    assert _writer().subscribe_topics == [_IN_TOPIC]


def test_the_contract_declares_the_table_read_write() -> None:
    """Without the table block the consumer advances offsets and never folds.

    `read_write`, not `write`: the upsert's conflict arm reads the stored
    event time to refuse a stale redelivery, and a write-only declaration is
    refused fail-closed at the runtime read seam.
    """
    tables = _load_contract()["db_io"]["db_tables"]
    assert len(tables) == 1
    assert tables[0]["name"] == "ci_attempt_outcome"
    assert tables[0]["schema"] == "omninode_internal"
    assert tables[0]["access"] == "read_write"


def test_the_contract_routes_both_halves_of_the_pair() -> None:
    """A projection is two classes and the contract has to name both."""
    handlers = _load_contract()["handler_routing"]["handlers"]
    names = {entry["handler"]["name"] for entry in handlers}
    assert names == {
        "HandlerProjectionCiAttemptOutcome",
        "CiAttemptOutcomeProjectionWriter",
    }


# --------------------------------------------------------------------------
# AC-3: one outcome per cause class, six rows.
# --------------------------------------------------------------------------


def test_one_outcome_per_cause_class_yields_six_rows() -> None:
    writer = _writer()
    result = writer.handle(_event_one_check_per_cause())

    assert result["rows_written"] == 6
    stored = [row["cause_code"] for row in result["attempt_rows"]]
    assert sorted(stored) == sorted(_ALL_CAUSE_CODES)
    assert len(_ALL_CAUSE_CODES) == 6


def test_every_enum_member_is_representable_in_the_row() -> None:
    """The check constraint lists six values; the enum must still have six.

    A seventh member added without widening the migration writes a row the
    database refuses, so this is pinned on the model side too.
    """
    request = ModelCiAttemptOutcomeProjectionRequest.model_validate(
        _event_one_check_per_cause()
    )
    rows = HandlerProjectionCiAttemptOutcome().handle(request).rows
    assert {row.cause_code for row in rows} == set(EnumMergeCheckReasonCode)


def test_the_stored_cause_codes_match_the_migration_check_constraint() -> None:
    """The schema and the vocabulary are pinned against each other."""
    migration = (
        _CONTRACT.parent / "migrations" / "0000_create_ci_attempt_outcome.sql"
    ).read_text()
    for code in _ALL_CAUSE_CODES:
        assert f"'{code}'" in migration, f"{code} missing from the check constraint"


# --------------------------------------------------------------------------
# AC-5: the writer is wired the one way the runtime will dispatch.
# --------------------------------------------------------------------------


def test_the_writer_declares_in_process_dispatch() -> None:
    """The runtime routes on a declared capability, never on a class name.

    Undeclared, the shared runtime treats this as a standalone runner and
    skips it. There is no dedicated writer deployment for this node, so
    undeclared means dispatched by nobody: zero rows, no error, every
    watermark healthy.
    """
    assert CiAttemptOutcomeProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_writer_entry_returns_a_row_count() -> None:
    """A truthy acknowledgement over a message that wrote nothing is a lie.

    The runtime's write-path guard reads this key; without it a projection
    that stored nothing is indistinguishable from one that stored everything.
    """
    result = _writer().handle(_event_one_check_per_cause())
    assert "rows_written" in result
    assert isinstance(result["rows_written"], int)


def test_the_writer_calls_the_fold_rather_than_deriving_its_own_rows() -> None:
    """One derivation, so the writer and the reducer cannot disagree."""
    assert isinstance(
        CiAttemptOutcomeProjectionWriter()._derive, HandlerProjectionCiAttemptOutcome
    )


def test_two_consecutive_messages_both_write() -> None:
    """One event loop per message: nothing loop-bound survives across calls."""
    writer = _writer()
    first = writer.handle(_event_one_check_per_cause())
    second = writer.handle(_event_one_check_per_cause(_T0 + timedelta(minutes=1)))
    assert first["rows_written"] == 6
    assert second["rows_written"] == 6


def test_a_stale_redelivery_is_refused_by_the_database_not_counted() -> None:
    """The guard is in the conflict arm, so two consumers cannot race it."""
    writer = _writer(refuse_stale=True)
    result = writer.handle(_event_one_check_per_cause())
    assert result["rows_written"] == 0
    upserts = [
        query
        for query, _ in writer._db.calls
        if "ON CONFLICT" in query  # type: ignore[attr-defined]
    ]
    assert len(upserts) == 6, "every row was attempted; the database refused them"


def test_the_upsert_carries_the_stale_write_guard_in_sql() -> None:
    """A read-then-write races; the WHERE clause on the conflict arm does not."""
    writer = _writer()
    writer.handle(_event_one_check_per_cause())
    upsert = next(
        query
        for query, _ in writer._db.calls
        if "ON CONFLICT" in query  # type: ignore[attr-defined]
    )
    assert "observed_at < EXCLUDED.observed_at" in upsert
    assert "RETURNING" in upsert
