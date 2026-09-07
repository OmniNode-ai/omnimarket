# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17810 — the reducer's runtime bus dispatch projects instead of DLQ'ing.

RED before the fix: `handle_dict` read `input_data.get("state", {})`, so the
runtime projection arm's dict (a producer payload plus the injected `_db` /
`_topic` / `_event_type` keys, with no `"state"` key) built
`ModelPrLifecycleState(**{})` and raised

    1 validation error for ModelPrLifecycleState
    correlation_id
      Field required [type=missing, input_value={}, input_type=dict]

on 100% of events. Measured on the .201 dev lane (compose project
`omnibase-infra`, container `omninode-runtime`): 11 quarantine routings between
2026-09-07T19:33:52Z and 21:25:27Z, all from
`onex.evt.omnimarket.pr-lifecycle-fix-completed.v1`, which is the ONLY one of
the reducer's eight subscribed topics that has ever carried a record
(HIGH-WATERMARK 5064; the other seven sit at 0).

`_FIX_COMPLETED_WIRE_PAYLOAD` below is a verbatim copy of a real record read
off that topic with `rpk topic consume` — not a hand-written approximation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
    PrLifecycleReducerInputShapeError,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_bus_observation import (
    PR_LIFECYCLE_FIX_COMPLETED_TOPIC,
)
from omnimarket.projection.pr_ledger_projection import (
    PR_LEDGER_PROJECTION_TABLE,
    EnumPrLedgerAction,
    EnumPrLedgerFinalState,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

# Verbatim from onex.evt.omnimarket.pr-lifecycle-fix-completed.v1 on the .201
# dev lane, envelope_id 5b667b35-279e-577b-97b0-c5ca3f5ae7cf,
# envelope_timestamp 2026-09-07T21:25:27.699966Z.
_FIX_COMPLETED_WIRE_PAYLOAD: dict[str, Any] = {
    "correlation_id": "bcd8c240-39a9-4095-86d7-34b9109c8120",
    "pr_number": 3297,
    "repo": "OmniNode-ai/omnibase_infra",
    "block_reason": "receipt_evidence_source_autobind",
    "fix_applied": True,
    "fix_action": (
        "authored OCC companion Evidence-Source: OCC#8607 for OMN-17896 on "
        "OmniNode-ai/omnibase_infra#3297"
    ),
    "occ_companion_verified": False,
    "error": None,
    "completed_at": "2026-09-07T21:25:27.699265Z",
    "delegated": False,
    "delegation_model": None,
    "delegation_outcome": None,
    "delegation_cost_usd": None,
}


def _projection_arm_input(
    payload: dict[str, Any],
    topic: str,
    database: InmemoryDatabaseAdapter,
) -> dict[str, Any]:
    """Rebuild exactly what `_make_projection_dispatch_callback` passes.

    See `omnibase_infra/src/omnibase_infra/runtime/auto_wiring/handler_wiring.py`
    `_make_projection_dispatch_callback`: the payload dict, then `_db`,
    `_event_type` and `_topic` injected on top.
    """
    return {
        **payload,
        "_db": database,
        "_event_type": "omnimarket.pr-lifecycle-fix-completed",
        "_topic": topic,
    }


@pytest.mark.unit
class TestBusObservationProjection:
    """The one live topic materializes a durable ledger row."""

    def test_real_fix_completed_payload_projects_one_ledger_row(self) -> None:
        """RED before OMN-17810: this raised ValidationError for ModelPrLifecycleState."""
        handler = HandlerPrLifecycleStateReducer()
        database = InmemoryDatabaseAdapter()

        result = handler.handle_dict(
            _projection_arm_input(
                _FIX_COMPLETED_WIRE_PAYLOAD,
                PR_LIFECYCLE_FIX_COMPLETED_TOPIC,
                database,
            )
        )

        assert result["rows_upserted"] == 1
        assert result["table"] == PR_LEDGER_PROJECTION_TABLE
        rows = database.query(PR_LEDGER_PROJECTION_TABLE)
        assert len(rows) == 1
        row = rows[0]
        assert row["sweep_id"] == "bcd8c240-39a9-4095-86d7-34b9109c8120"
        assert row["repo"] == "OmniNode-ai/omnibase_infra"
        assert row["pr_number"] == 3297
        assert row["iteration"] == 0
        assert row["initial_state"] == "receipt_evidence_source_autobind"
        assert row["action_taken"] == EnumPrLedgerAction.FIX.value
        assert row["final_state"] == EnumPrLedgerFinalState.FIX_DISPATCHED.value
        assert "OCC#8607" in str(row["evidence"])

    def test_redelivery_upserts_onto_the_same_conflict_key(self) -> None:
        """Kafka redelivery must not append a duplicate ledger row."""
        handler = HandlerPrLifecycleStateReducer()
        database = InmemoryDatabaseAdapter()
        dispatch = _projection_arm_input(
            _FIX_COMPLETED_WIRE_PAYLOAD,
            PR_LIFECYCLE_FIX_COMPLETED_TOPIC,
            database,
        )

        handler.handle_dict(dispatch)
        handler.handle_dict(dispatch)

        assert len(database.query(PR_LEDGER_PROJECTION_TABLE)) == 1
        assert database.upsert_count == 2

    def test_not_applied_fix_records_skipped_not_dispatched(self) -> None:
        """`fix_applied=False` is recorded as SKIPPED, read from the wire only."""
        handler = HandlerPrLifecycleStateReducer()
        database = InmemoryDatabaseAdapter()
        payload = {
            **_FIX_COMPLETED_WIRE_PAYLOAD,
            "fix_applied": False,
            "error": "no RED-derivable check",
        }

        handler.handle_dict(
            _projection_arm_input(payload, PR_LIFECYCLE_FIX_COMPLETED_TOPIC, database)
        )

        row = database.query(PR_LEDGER_PROJECTION_TABLE)[0]
        assert row["final_state"] == EnumPrLedgerFinalState.SKIPPED.value
        assert "error: no RED-derivable check" in str(row["evidence"])


@pytest.mark.unit
class TestBusDispatchFailsFast:
    """No silent defaults: every rejected shape names what was wrong."""

    def test_unprojectable_topic_raises_validation_error_naming_the_topic(
        self,
    ) -> None:
        """A subscribed-but-unprojectable topic is a CONTENT failure, named.

        A pydantic ValidationError is what
        `_is_projection_content_failure` classifies as the event's own defect,
        so the record is DLQ'd and the offset advances — never an infinite
        redelivery of a record no producer can repair.
        """
        handler = HandlerPrLifecycleStateReducer()
        database = InmemoryDatabaseAdapter()
        unprojectable = "onex.evt.omnimarket.repo-health-classified.v1"

        with pytest.raises(ValidationError) as excinfo:
            handler.handle_dict(
                _projection_arm_input(
                    _FIX_COMPLETED_WIRE_PAYLOAD, unprojectable, database
                )
            )

        assert unprojectable in str(excinfo.value)
        assert database.query(PR_LEDGER_PROJECTION_TABLE) == []

    def test_missing_producer_field_raises_validation_error(self) -> None:
        """A payload without `pr_number` cannot make a row and says so."""
        handler = HandlerPrLifecycleStateReducer()
        database = InmemoryDatabaseAdapter()
        payload = {
            key: value
            for key, value in _FIX_COMPLETED_WIRE_PAYLOAD.items()
            if key != "pr_number"
        }

        with pytest.raises(ValidationError) as excinfo:
            handler.handle_dict(
                _projection_arm_input(
                    payload, PR_LIFECYCLE_FIX_COMPLETED_TOPIC, database
                )
            )

        assert "pr_number" in str(excinfo.value)

    def test_runtime_dispatch_without_topic_names_the_missing_key(self) -> None:
        """`_db` but no `_topic` is a WIRING defect, raised as such."""
        handler = HandlerPrLifecycleStateReducer()
        dispatch: dict[str, Any] = {
            **_FIX_COMPLETED_WIRE_PAYLOAD,
            "_db": InmemoryDatabaseAdapter(),
        }

        with pytest.raises(PrLifecycleReducerInputShapeError) as excinfo:
            handler.handle_dict(dispatch)

        assert "_topic" in str(excinfo.value)

    def test_in_process_envelope_missing_state_key_names_the_key(self) -> None:
        """OMN-17810 AC3: no `.get("state", {})` default stands on this path."""
        handler = HandlerPrLifecycleStateReducer()

        with pytest.raises(PrLifecycleReducerInputShapeError) as excinfo:
            handler.handle_dict({"event": {}})

        message = str(excinfo.value)
        assert "'state'" in message
        assert "keys_present=['event']" in message


@pytest.mark.unit
class TestInProcessEnvelopeUnchanged:
    """The orchestrator / RuntimeLocal path keeps its existing contract."""

    def test_in_process_envelope_still_reduces(self) -> None:
        handler = HandlerPrLifecycleStateReducer()
        correlation_id = str(uuid4())

        result = handler.handle_dict(
            {
                "state": {"correlation_id": correlation_id, "phase": "idle"},
                "event": {
                    "correlation_id": correlation_id,
                    "source_phase": "idle",
                    "trigger": "start_received",
                    "success": True,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                },
            }
        )

        assert result["state"]["phase"] == "inventorying"
        assert len(result["intents"]) == 1
