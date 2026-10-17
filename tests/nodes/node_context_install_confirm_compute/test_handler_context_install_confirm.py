# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture table for the TA.13 boundary algorithm (OMN-21115).

The record shapes follow the P6 recordings: a hook-supplied boundary has
``parentUuid: null``, ``logicalParentUuid`` set and no ``preservedMessages``,
its replacement records chain from it by ``parentUuid`` and ``/compact`` then
appends a meta ``<local-command-caveat>`` record; a native boundary carries
``preservedMessages.uuids``, attachments and an ``isCompactSummary`` record.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    EnumInstallFact,
    EnumInstallFactReason,
    ModelCapturedTranscriptRecord,
    ModelInstallationFact,
    ModelInstallConfirmRequest,
    ModelInstallConfirmResult,
    ModelPublishedActivation,
)
from omnimarket.models.context_pack_render import ModelContextPackRenderMessage
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
    observed_messages_hash,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SESSION = "844d62fa-0000-4000-8000-000000000001"
A1 = UUID("9f3c0000-0000-4000-8000-000000000001")
A2 = UUID("9f3c0000-0000-4000-8000-000000000002")
A3 = UUID("9f3c0000-0000-4000-8000-000000000003")
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_install_confirm_compute"
)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def rec(
    uuid: str,
    parent: str | None,
    t: int,
    record_type: str = "user",
    text: str | None = "turn",
    **fields: object,
) -> ModelCapturedTranscriptRecord:
    role = record_type if record_type in {"user", "assistant"} else None
    return ModelCapturedTranscriptRecord.model_validate(
        {
            "uuid": uuid,
            "parent_uuid": parent,
            "record_type": record_type,
            "role": role,
            "text": text,
            "timestamp": at(t),
            **fields,
        }
    )


def package(activation_id: UUID, label: str) -> list[tuple[str, str]]:
    return [
        ("user", f"{label} legend and stable blocks"),
        ("assistant", f"{label} acknowledged"),
        ("user", f"{label} identity trailer\nactivation_id: {activation_id}"),
    ]


def package_hash(messages: list[tuple[str, str]]) -> str:
    return observed_messages_hash(
        ModelContextPackRenderMessage(role=role, text=text) for role, text in messages
    )


def activation(
    activation_id: UUID, messages: list[tuple[str, str]], t: int
) -> ModelPublishedActivation:
    return ModelPublishedActivation(
        activation_id=activation_id,
        installed_messages_hash=package_hash(messages),
        published_at=at(t),
    )


def custom(
    boundary: str, logical_parent: str, t: int, messages: list[tuple[str, str]]
) -> list[ModelCapturedTranscriptRecord]:
    """A hook-supplied compaction as P6 records it, with the /compact records after it."""
    records = [
        rec(
            boundary,
            None,
            t,
            "system",
            None,
            subtype="compact_boundary",
            logical_parent_uuid=logical_parent,
        )
    ]
    parent = boundary
    for index, (role, text) in enumerate(messages):
        uuid = f"{boundary}-r{index}"
        records.append(rec(uuid, parent, t, role, text, prompt_source=None))
        parent = uuid
    records.append(
        rec(
            f"{boundary}-caveat",
            parent,
            t + 1,
            "user",
            "<local-command-caveat>x</local-command-caveat>",
            is_meta=True,
        )
    )
    records.append(
        rec(
            f"{boundary}-cmd",
            f"{boundary}-caveat",
            t + 1,
            "user",
            "<command-name>/compact</command-name>",
        )
    )
    return records


def native(
    boundary: str, logical_parent: str, t: int, preserved: list[tuple[str, str]]
) -> list[ModelCapturedTranscriptRecord]:
    """A native compaction: attachments, a summary record, then the preserved messages."""
    preserved_uuids = tuple(f"{boundary}-p{i}" for i in range(len(preserved)))
    records = [
        rec(
            boundary,
            None,
            t,
            "system",
            None,
            subtype="compact_boundary",
            logical_parent_uuid=logical_parent,
            preserved_uuids=preserved_uuids,
        ),
        rec(f"{boundary}-att0", boundary, t, "attachment", None),
        rec(f"{boundary}-att1", f"{boundary}-att0", t, "attachment", None),
        rec(
            f"{boundary}-sum",
            f"{boundary}-att1",
            t,
            "user",
            "native summary",
            is_compact_summary=True,
        ),
    ]
    parent = f"{boundary}-sum"
    for uuid, (role, text) in zip(preserved_uuids, preserved, strict=True):
        records.append(rec(uuid, parent, t, role, text))
        parent = uuid
    return records


def prompt(uuid: str, parent: str, t: int) -> ModelCapturedTranscriptRecord:
    return rec(uuid, parent, t, "user", "operator prompt", prompt_source="sdk")


def run(
    records: list[ModelCapturedTranscriptRecord],
    activations: list[ModelPublishedActivation],
    newest_captured_at: datetime | None = None,
) -> ModelInstallConfirmResult:
    return HandlerContextInstallConfirm().handle(
        ModelInstallConfirmRequest(
            session_id=SESSION,
            records=tuple(records),
            activations=tuple(activations),
            newest_captured_at=newest_captured_at,
        )
    )


def fact_of(
    result: ModelInstallConfirmResult, activation_id: UUID
) -> ModelInstallationFact:
    matches = [f for f in result.facts if f.activation_id == activation_id]
    assert len(matches) == 1, result.facts
    return matches[0]


PRE = [
    rec("p0", None, 0, prompt_source="sdk"),
    rec("p1", "p0", 1, "assistant", "reply"),
]


def test_custom_compaction_is_installed_at_its_boundary() -> None:
    msgs = package(A1, "one")
    result = run([*PRE, *custom("b1", "p1", 10, msgs)], [activation(A1, msgs, 5)])
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.INSTALLED
    assert fact.boundary_uuid == "b1"
    assert fact.observed_hash == package_hash(msgs)
    assert result.boundaries[0].replacement_uuids == ("b1-r0", "b1-r1", "b1-r2")
    assert result.current.boundary_uuid == "b1"
    assert result.current.activation_id == A1


def test_tampered_copy_is_install_mismatch() -> None:
    msgs = package(A1, "one")
    tampered = [msgs[0], ("assistant", "edited in transit"), msgs[2]]
    result = run([*PRE, *custom("b1", "p1", 10, tampered)], [activation(A1, msgs, 5)])
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.INSTALL_MISMATCH
    assert fact.boundary_uuid == "b1"
    assert fact.observed_hash != fact.installed_messages_hash
    assert result.current.activation_id is None


def test_custom_then_native_keeps_installed_and_retires_it() -> None:
    msgs = package(A1, "one")
    records = [
        *PRE,
        *custom("b1", "p1", 10, msgs),
        prompt("u2", "b1-cmd", 20),
        *native("b2", "u2", 30, [("user", "kept turn")]),
    ]
    result = run(records, [activation(A1, msgs, 5)])
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.INSTALLED
    assert fact.boundary_uuid == "b1"
    assert result.current.boundary_uuid == "b2"
    assert result.current.kind is EnumBoundaryKind.NATIVE
    assert result.current.activation_id is None
    assert result.current.chain_boundary_uuids == ("b1", "b2")
    [retired] = result.current.retirements
    assert (
        retired.activation_id,
        retired.installed_at_boundary_uuid,
        retired.retired_at_boundary_uuid,
    ) == (A1, "b1", "b2")


def test_activation_before_native_boundary_is_not_installed_native() -> None:
    msgs = package(A1, "one")
    records = [*PRE, *native("b1", "p1", 10, [("user", "kept turn")])]
    fact = fact_of(run(records, [activation(A1, msgs, 5)]), A1)
    assert fact.fact is EnumInstallFact.NOT_INSTALLED
    assert fact.reason is EnumInstallFactReason.NATIVE_COMPACTION
    assert fact.boundary_uuid == "b1"


def test_native_then_custom_installs_the_custom_one() -> None:
    msgs = package(A2, "two")
    records = [
        *PRE,
        *native("b1", "p1", 10, [("user", "kept turn")]),
        prompt("u2", "b1-p0", 20),
        *custom("b2", "u2", 30, msgs),
    ]
    result = run(records, [activation(A2, msgs, 25)])
    fact = fact_of(result, A2)
    assert fact.fact is EnumInstallFact.INSTALLED
    assert fact.boundary_uuid == "b2"
    assert result.current.activation_id == A2
    assert result.current.retirements == ()


def test_discarded_precompute_is_not_installed_not_selected() -> None:
    kept = package(A2, "two")
    discarded = package(A1, "one")
    records = [*PRE, *custom("b1", "p1", 10, kept)]
    result = run(records, [activation(A1, discarded, 3), activation(A2, kept, 6)])
    assert fact_of(result, A2).fact is EnumInstallFact.INSTALLED
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.NOT_INSTALLED
    assert fact.reason is EnumInstallFactReason.NOT_SELECTED
    assert fact.boundary_uuid == "b1"


def test_preserved_message_with_older_activation_line_is_never_selected() -> None:
    old = package(A1, "one")
    records = [
        *PRE,
        *custom("b1", "p1", 10, old),
        prompt("u2", "b1-cmd", 20),
        *native("b2", "u2", 30, [("user", f"quoted trailer\nactivation_id: {A1}")]),
    ]
    result = run(records, [activation(A1, old, 5)])
    reading = result.boundaries[1]
    assert reading.kind is EnumBoundaryKind.NATIVE
    assert reading.activation_id is None
    assert "b2-p0" not in reading.replacement_uuids
    assert result.current.activation_id is None
    assert fact_of(result, A1).boundary_uuid == "b1"


def test_truncated_replacement_record_is_uncertain() -> None:
    msgs = package(A1, "one")
    records = [*PRE, *custom("b1", "p1", 10, msgs)]
    records = [
        r.model_copy(update={"text": None}) if r.uuid == "b1-r2" else r for r in records
    ]
    result = run(records, [activation(A1, msgs, 5)])
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.UNCERTAIN
    assert fact.reason is EnumInstallFactReason.UNPARSEABLE_RECORD
    assert result.boundaries[0].kind is EnumBoundaryKind.UNREADABLE


def test_truncated_non_trailer_record_keeps_the_named_activation_uncertain() -> None:
    msgs = package(A1, "one")
    records = [*PRE, *custom("b1", "p1", 10, msgs)]
    records = [
        r.model_copy(update={"text": None}) if r.uuid == "b1-r1" else r for r in records
    ]
    fact = fact_of(run(records, [activation(A1, msgs, 5)]), A1)
    assert fact.fact is EnumInstallFact.UNCERTAIN
    assert fact.reason is EnumInstallFactReason.UNPARSEABLE_RECORD


def test_resumed_and_forked_sessions_select_their_own_active_chain() -> None:
    first = package(A1, "one")
    second = package(A2, "two")
    shared = [*PRE, *custom("b1", "p1", 10, first), prompt("u2", "b1-cmd", 20)]
    original = [*custom("b2", "u2", 30, second), prompt("a1", "b2-cmd", 40)]
    fork = [rec("f1", "u2", 50, "assistant", "fork reply")]
    activations = [activation(A1, first, 5), activation(A2, second, 25)]

    resumed = run([*shared, *original], activations)
    assert resumed.current.boundary_uuid == "b2"
    assert resumed.current.activation_id == A2

    forked = run([*shared, *original, *fork], activations)
    assert forked.current.boundary_uuid == "b1"
    assert forked.current.activation_id == A1
    assert forked.current.chain_boundary_uuids == ("b1",)
    assert fact_of(forked, A2).fact is EnumInstallFact.INSTALLED


def test_boundary_whose_logical_parent_was_never_captured_ends_the_chain() -> None:
    msgs = package(A1, "one")
    result = run(
        custom("b1", "absent-from-capture", 10, msgs), [activation(A1, msgs, 5)]
    )
    assert result.current.chain_boundary_uuids == ("b1",)
    assert fact_of(result, A1).fact is EnumInstallFact.INSTALLED


def test_later_captured_turn_without_a_boundary_is_capture_gap() -> None:
    msgs = package(A1, "one")
    result = run(list(PRE), [activation(A1, msgs, 5)], newest_captured_at=at(60))
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.UNCERTAIN
    assert fact.reason is EnumInstallFactReason.CAPTURE_GAP
    assert fact.boundary_uuid is None


def test_activation_with_nothing_captured_after_it_stays_pending() -> None:
    msgs = package(A1, "one")
    result = run(list(PRE), [activation(A1, msgs, 5)], newest_captured_at=at(5))
    assert result.facts == ()
    assert result.pending_activation_ids == (A1,)


def _mixed_session() -> tuple[
    list[ModelCapturedTranscriptRecord], list[ModelPublishedActivation]
]:
    one, two, three = package(A1, "one"), package(A2, "two"), package(A3, "three")
    records = [
        *PRE,
        *custom("b1", "p1", 10, one),
        prompt("u2", "b1-cmd", 20),
        *native("b2", "u2", 30, [("user", f"old\nactivation_id: {A1}")]),
        prompt("u3", "b2-p0", 40),
        *custom("b3", "u3", 50, three),
    ]
    activations = [
        activation(A1, one, 5),
        activation(A2, two, 25),
        activation(A3, three, 45),
    ]
    return records, activations


def test_every_permutation_with_duplicates_folds_to_one_result() -> None:
    records, activations = _mixed_session()
    expected = run(records, activations).model_dump_json()
    rng = random.Random(21115)
    for _ in range(200):
        shuffled_records = records + rng.sample(records, k=5)
        shuffled_activations = activations + rng.sample(activations, k=2)
        rng.shuffle(shuffled_records)
        rng.shuffle(shuffled_activations)
        assert run(shuffled_records, shuffled_activations).model_dump_json() == expected


def test_mixed_session_fixture_table() -> None:
    records, activations = _mixed_session()
    result = run(records, activations)
    table = {(f.activation_id, f.fact, f.reason, f.boundary_uuid) for f in result.facts}
    assert table == {
        (A1, EnumInstallFact.INSTALLED, None, "b1"),
        (
            A2,
            EnumInstallFact.NOT_INSTALLED,
            EnumInstallFactReason.NATIVE_COMPACTION,
            "b2",
        ),
        (A3, EnumInstallFact.INSTALLED, None, "b3"),
    }
    assert result.current.boundary_uuid == "b3"
    assert result.current.activation_id == A3
    assert [
        (r.activation_id, r.retired_at_boundary_uuid)
        for r in result.current.retirements
    ] == [(A1, "b2")]


def test_appending_a_later_boundary_never_changes_an_earlier_fact() -> None:
    records, activations = _mixed_session()
    before = {
        f.activation_id: f.model_dump_json() for f in run(records, activations).facts
    }
    later = [
        prompt("u4", "b3-cmd", 60),
        *native("b4", "u4", 70, [("user", "kept")]),
    ]
    after = {
        f.activation_id: f.model_dump_json()
        for f in run(records + later, activations).facts
    }
    assert after == before


def test_p6_shaped_transcript_lines_parse_and_install() -> None:
    msgs = package(A1, "probe")
    lines = [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "783e1d6e",
            "parentUuid": None,
            "logicalParentUuid": "e61b705c",
            "timestamp": "2026-10-17T12:00:10Z",
            "compactMetadata": {
                "trigger": "manual",
                "preTokens": 2712,
                "durationMs": 4,
                "postTokens": 59,
                "cumulativeDroppedTokens": 2653,
            },
        },
        {
            "type": "user",
            "uuid": "1c523b67",
            "parentUuid": "783e1d6e",
            "promptId": "13aa4872",
            "timestamp": "2026-10-17T12:00:10Z",
            "message": {"role": "user", "content": msgs[0][1]},
        },
        {
            "type": "assistant",
            "uuid": "4a93b224",
            "parentUuid": "1c523b67",
            "timestamp": "2026-10-17T12:00:10Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": msgs[1][1]}],
            },
        },
        {
            "type": "user",
            "uuid": "d0fc9ae7",
            "parentUuid": "4a93b224",
            "promptId": "13aa4872",
            "timestamp": "2026-10-17T12:00:10Z",
            "message": {"role": "user", "content": msgs[2][1]},
        },
        {
            "type": "user",
            "uuid": "c1",
            "parentUuid": "d0fc9ae7",
            "isMeta": True,
            "promptId": "13aa4872",
            "timestamp": "2026-10-17T12:00:11Z",
            "message": {
                "role": "user",
                "content": "<local-command-caveat>x</local-command-caveat>",
            },
        },
    ]
    records = [
        ModelCapturedTranscriptRecord.from_transcript_line(json.dumps(x)) for x in lines
    ]
    result = run(records, [activation(A1, msgs, 5)])
    fact = fact_of(result, A1)
    assert fact.fact is EnumInstallFact.INSTALLED
    assert fact.boundary_uuid == "783e1d6e"
    assert result.boundaries[0].replacement_uuids == (
        "1c523b67",
        "4a93b224",
        "d0fc9ae7",
    )


def test_non_text_content_block_parses_as_unreadable() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "x",
            "parentUuid": "y",
            "timestamp": "2026-10-17T12:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "tool_use"}]},
        }
    )
    assert ModelCapturedTranscriptRecord.from_transcript_line(line).text is None


def test_contract_names_the_handler_and_models() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["handler"]["class"] == HandlerContextInstallConfirm.__name__
    assert contract["input_model"]["name"] == ModelInstallConfirmRequest.__name__
    assert contract["output_model"]["name"] == ModelInstallConfirmResult.__name__
    completed = "onex.evt.omnimarket.context-install-confirm-completed.v1"
    assert contract["terminal_event"] == completed
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == completed
    assert contract["event_bus"]["publish_topics"] == [completed]
