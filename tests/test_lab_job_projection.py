# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20604 M2: the pure fold and strict, seq-ordered writer seam."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.events.topics import LAB_JOB_TRANSITIONED_TOPIC_V1
from omnimarket.models.lab_job import (
    EnumLabJobKind,
    ModelLabJobRow,
    ModelLabJobSpec,
    ModelLabJobTransition,
    ModelLabJobTransitioned,
)
from omnimarket.models.lab_job import (
    EnumLabJobState as S,
)
from omnimarket.nodes.node_projection_lab_job.handlers import (
    HandlerProjectionLabJob,
    LabJobProjectionWriter,
)
from omnimarket.nodes.node_projection_lab_job.models import ModelLabJobProjectionRequest

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 5, tzinfo=UTC)
JOB = "lj-0123456789abcdef"


def transitioned(seq: int, state: S = S.RUNNING, **changes: Any) -> dict[str, Any]:
    row = ModelLabJobRow(
        job_id=JOB,
        kind=EnumLabJobKind.ADOPTED,
        state=state,
        seq=seq,
        entered_state_at=T0 + timedelta(seconds=seq),
        **changes,
    )
    transition = ModelLabJobTransition(
        job_id=JOB,
        seq=seq,
        from_state=S.DISPATCHED,
        to_state=state,
        attempt=row.attempt,
        at=row.entered_state_at,
        reason="taken",
    )
    event = ModelLabJobTransitioned(row=row, transition=transition).model_dump(
        mode="json"
    )
    return {
        **event,
        "_topic": LAB_JOB_TRANSITIONED_TOPIC_V1,
        "_partition": 0,
        "_offset": seq,
    }


def life() -> list[dict[str, Any]]:
    return [
        transitioned(seq, state)
        for seq, state in enumerate(
            (S.QUEUED, S.DISPATCHED, S.RUNNING, S.CHECKING, S.DONE), start=1
        )
    ]


def test_a_transition_folds_to_the_complete_state_and_transition_dicts() -> None:
    event = transitioned(
        9,
        owner_runtime="lab-1",
        claimed_at=T0,
        attempt=2,
        continuation="next",
        parent_lane="lane-1",
        ticket="OMN-20604",
    )
    result = HandlerProjectionLabJob().handle(
        LabJobProjectionWriter.build_request(LAB_JOB_TRANSITIONED_TOPIC_V1, event)
    )
    assert result.state_row == ModelLabJobRow.model_validate(event["row"]).model_dump(
        mode="python"
    )
    assert result.transition_row == ModelLabJobTransition.model_validate(
        event["transition"]
    ).model_dump(mode="python")
    assert result.state_row["entered_state_at"] == T0 + timedelta(seconds=9)
    assert result.state_row["spec"] is None


def test_folding_the_same_event_twice_is_identical() -> None:
    fold = HandlerProjectionLabJob()
    for event in life():
        request = LabJobProjectionWriter.build_request(
            LAB_JOB_TRANSITIONED_TOPIC_V1, event
        )
        assert fold.handle(request) == fold.handle(request)


@pytest.mark.parametrize("field", ["job_id", "seq"])
def test_transitioned_refuses_mismatched_job_id_or_seq(field: str) -> None:
    event = transitioned(2)
    event["transition"][field] = "another-job" if field == "job_id" else 3
    with pytest.raises(ValidationError, match=field):
        LabJobProjectionWriter.build_request(LAB_JOB_TRANSITIONED_TOPIC_V1, event)


def test_payload_is_frozen_and_forbids_extra_fields() -> None:
    payload = ModelLabJobTransitioned.model_validate(
        {k: v for k, v in transitioned(1).items() if not k.startswith("_")}
    )
    with pytest.raises(ValidationError, match="frozen"):
        payload.row = payload.row
    event = transitioned(1)
    event["future_field"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LabJobProjectionWriter.build_request(LAB_JOB_TRANSITIONED_TOPIC_V1, event)


def test_the_request_takes_exactly_one_event() -> None:
    with pytest.raises(ValidationError):
        ModelLabJobProjectionRequest()


def test_a_topic_the_node_does_not_consume_is_refused() -> None:
    with pytest.raises(ValueError, match="does not consume"):
        LabJobProjectionWriter.build_request("unknown", transitioned(1))


class MemoryAdapter:
    """Reduced database: records bindings and applies the declared SQL guards.

    The real-Postgres tests prove these guards in the database; this double
    exercises the writer's row counts and the fold-to-bindings seam.
    """

    def __init__(self) -> None:
        self.state: dict[str, tuple[Any, ...]] = {}
        self.transitions: dict[tuple[str, int], tuple[Any, ...]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.dsn = "postgresql://recording/double"
        self.connects = 0
        self.closes = 0

    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        self.closes += 1

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "lab_job_transitions" in query:
            assert "ON CONFLICT (job_id, seq) DO NOTHING" in query
            assert "UPDATE" not in query
            key = (params[0], params[1])
            if key in self.transitions:
                return []
            self.transitions[key] = params
        else:
            guard = query.split("WHERE", 1)[1].split("RETURNING", 1)[0].strip()
            assert guard == "EXCLUDED.seq > omninode_internal.lab_job_state.seq"
            # Parameter order mirrors the row's columns, with seq at index 5.
            current = self.state.get(params[0])
            if current is not None and params[5] <= current[5]:
                return []
            self.state[params[0]] = params
        return [{"job_id": params[0]}]


def writer_with_memory() -> tuple[LabJobProjectionWriter, MemoryAdapter]:
    writer = LabJobProjectionWriter()
    adapter = MemoryAdapter()
    vars(writer)["_db"] = adapter
    vars(writer)["_adapter_for_one_message"] = lambda: adapter
    return writer, adapter


def test_an_older_transition_never_replaces_a_newer_row() -> None:
    writer, db = writer_with_memory()
    assert writer.handle(transitioned(5, S.DONE))["rows_upserted"] == 2
    snapshot = dict(db.state)
    older = writer.handle(transitioned(4, S.CHECKING))
    assert db.state == snapshot
    assert older["rows_upserted"] == 1
    assert older["rows_refused_by_ordering_guard"] == 1
    assert older["state_write_refused"] is True
    assert sorted(seq for _, seq in db.transitions) == [4, 5]


def test_a_redelivered_transition_writes_nothing() -> None:
    writer, db = writer_with_memory()
    event = transitioned(3)
    assert writer.handle(dict(event))["rows_upserted"] == 2
    snapshot = (dict(db.state), dict(db.transitions))
    duplicate = writer.handle(dict(event))
    assert duplicate["rows_upserted"] == 0
    assert duplicate["rows_refused_by_ordering_guard"] == 2
    assert (db.state, db.transitions) == snapshot
    assert db.connects == db.closes == 2


def test_an_equal_seq_with_different_values_is_refused() -> None:
    writer, db = writer_with_memory()
    writer.handle(transitioned(3, S.RUNNING))
    snapshot = (dict(db.state), dict(db.transitions))
    assert writer.handle(transitioned(3, S.FAILED))["rows_upserted"] == 0
    assert (db.state, db.transitions) == snapshot


def test_spec_is_bound_as_canonical_json_and_times_as_datetimes() -> None:
    spec = ModelLabJobSpec(
        job_id=JOB,
        kind=EnumLabJobKind.LANE,
        brief="original brief",
        repo="OmniNode-ai/omnimarket",
        ref="a" * 40,
        engine="codex",
        time_box_min=10,
        parent_lane="lane-1",
        ticket="OMN-20604",
        done_criteria=({"kind": "exit_zero"},),
    )
    writer, db = writer_with_memory()
    writer.handle(transitioned(1, spec=spec, claimed_at=T0))
    params = db.state[JOB]
    decoded = json.loads(params[-1])
    assert decoded == spec.model_dump(mode="json")
    assert params[-1] == json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    assert params[6] == T0 + timedelta(seconds=1)
    assert params[7] == T0
    assert next(iter(db.transitions.values()))[-2] == T0 + timedelta(seconds=1)
