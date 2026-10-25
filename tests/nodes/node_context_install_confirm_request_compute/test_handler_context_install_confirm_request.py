# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Captured compact packages feed the boundary algorithm and receipts fold (TA.13)."""

from __future__ import annotations

import hashlib
import importlib
import json
import random
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from pydantic import ValidationError

from omnimarket.models.context_install_confirm import (
    EnumInstallFact,
    EnumInstallFactReason,
    ModelCapturedTranscriptRecord,
    ModelPublishedActivation,
)
from omnimarket.models.context_install_confirm_request import (
    ModelCapturedCompactChunk,
    ModelInstallConfirmAssemblyRequest,
    ModelInstallConfirmAssemblyResult,
)
from omnimarket.models.context_pack_render import ModelContextPackRenderMessage
from omnimarket.nodes.node_context_install_confirm_compute.handlers.handler_context_install_confirm import (
    HandlerContextInstallConfirm,
    observed_messages_hash,
)
from omnimarket.nodes.node_context_install_confirm_request_compute.handlers.handler_context_install_confirm_request import (
    HandlerContextInstallConfirmRequest,
)
from omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_projection_compaction_receipts import (
    HandlerProjectionCompactionReceipts,
)
from omnimarket.nodes.node_projection_compaction_receipts.models import (
    ModelCompactionReceiptsProjectionRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_NAME = "node_context_install_confirm_request_compute"
NODE_DIR = ROOT / "src/omnimarket/nodes" / NODE_NAME
SESSION = "captured-session"
BOUNDARY = "783e1d6e-1111-2222-3333-444444444444"
ACTIVATION = UUID("9f3c0000-0000-4000-8000-000000000001")
T0 = datetime(2026, 10, 17, 12, tzinfo=UTC)
CAPTURED = T0 + timedelta(seconds=20)
MESSAGES = (
    ModelContextPackRenderMessage(role="user", text="stable blocks — captured"),
    ModelContextPackRenderMessage(role="assistant", text="acknowledged"),
    ModelContextPackRenderMessage(
        role="user", text=f"identity trailer\nactivation_id: {ACTIVATION}"
    ),
)


def activation() -> ModelPublishedActivation:
    return ModelPublishedActivation(
        activation_id=ACTIVATION,
        installed_messages_hash=observed_messages_hash(MESSAGES),
        published_at=T0 + timedelta(seconds=5),
    )


def package() -> str:
    records = [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": BOUNDARY,
            "parentUuid": None,
            "logicalParentUuid": "before-compaction",
            "timestamp": (T0 + timedelta(seconds=10)).isoformat(),
        }
    ]
    parent = BOUNDARY
    for index, message in enumerate(MESSAGES):
        uuid = f"replacement-{index}"
        records.append(
            {
                "type": message.role,
                "uuid": uuid,
                "parentUuid": parent,
                "timestamp": (T0 + timedelta(seconds=10)).isoformat(),
                "message": {"role": message.role, "content": message.text},
            }
        )
        parent = uuid
    records.append(
        {
            "type": "user",
            "uuid": "compact-caveat",
            "parentUuid": parent,
            "timestamp": (T0 + timedelta(seconds=11)).isoformat(),
            "isMeta": True,
            "message": {
                "role": "user",
                "content": "<local-command-caveat>x</local-command-caveat>",
            },
        }
    )
    return (
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    )


def chunks(text: str, count: int = 1) -> tuple[ModelCapturedCompactChunk, ...]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return tuple(
        ModelCapturedCompactChunk(
            event_id=f"{digest}-{index}",
            session_id=SESSION,
            chunk_index=index,
            chunk_count=count,
            content=text[len(text) * index // count : len(text) * (index + 1) // count],
            content_sha256=digest,
            emitted_at=CAPTURED,
        )
        for index in range(count)
    )


def assemble(
    captured: tuple[ModelCapturedCompactChunk, ...],
    activations: tuple[ModelPublishedActivation, ...] | None = None,
    newest_captured_at: datetime | None = None,
) -> ModelInstallConfirmAssemblyResult:
    return HandlerContextInstallConfirmRequest().handle(
        ModelInstallConfirmAssemblyRequest(
            session_id=SESSION,
            chunks=captured,
            activations=(activation(),) if activations is None else activations,
            newest_captured_at=newest_captured_at,
        )
    )


def test_single_chunk_installed_custom_compaction() -> None:
    text = package()
    result = assemble(chunks(text))
    expected = tuple(
        sorted(
            (
                ModelCapturedTranscriptRecord.from_transcript_line(line)
                for line in text.splitlines()
            ),
            key=lambda record: (record.timestamp, record.uuid),
        )
    )
    assert result.request.session_id == SESSION
    assert result.request.records == expected
    assert result.request.activations == (activation(),)
    assert result.request.newest_captured_at == CAPTURED
    assert result.packages_read == 1
    assert result.packages_unreadable == ()
    (fact,) = HandlerContextInstallConfirm().handle(result.request).facts
    assert fact.fact is EnumInstallFact.INSTALLED
    assert fact.boundary_uuid == BOUNDARY


def test_three_chunks_and_duplicate_equal_single_chunk_and_fifty_shuffles() -> None:
    text = package()
    other = activation().model_copy(
        update={"activation_id": UUID("9f3c0000-0000-4000-8000-000000000002")}
    )
    activations = [other, activation(), activation()]
    expected = assemble(chunks(text), tuple(activations)).model_dump_json()
    parts = chunks(text, 3)
    captured = [
        parts[2],
        parts[0],
        parts[1],
        parts[1].model_copy(update={"event_id": "copy"}),
    ]
    for seed in range(50):
        rng = random.Random(seed)
        rng.shuffle(captured)
        rng.shuffle(activations)
        assert (
            assemble(tuple(captured), tuple(activations)).model_dump_json() == expected
        )


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "truncated",
        "sha_mismatch",
        "count_disagreement",
        "invalid_json",
        "missing_uuid",
        "invalid_timestamp",
        "null_sha",
        "conflicting_duplicate",
        "truncated_duplicate",
        "count_duplicate",
    ],
)
def test_unreadable_package_preserves_capture_gap(failure: str) -> None:
    text = package()
    parts = list(chunks(text, 3))
    if failure == "missing":
        parts.pop(1)
    elif failure == "truncated":
        parts[1] = parts[1].model_copy(update={"truncated": True})
    elif failure == "sha_mismatch":
        parts[1] = parts[1].model_copy(update={"content": parts[1].content + "!"})
    elif failure == "count_disagreement":
        parts[1] = parts[1].model_copy(update={"chunk_count": 4})
    elif failure in {"invalid_json", "missing_uuid", "invalid_timestamp"}:
        bad_line = {
            "invalid_json": "not-json",
            "missing_uuid": '{"type":"user"}',
            "invalid_timestamp": '{"uuid":"bad","timestamp":"not-a-date"}',
        }[failure]
        parts = list(chunks(text + bad_line + "\n", 3))
    elif failure == "null_sha":
        parts = [chunks(text)[0].model_copy(update={"content_sha256": None})]
    else:
        updates = {
            "conflicting_duplicate": {"content": "different"},
            "truncated_duplicate": {"truncated": True},
            "count_duplicate": {"chunk_count": 4},
        }[failure]
        parts.append(parts[1].model_copy(update=updates))
    key = parts[0].content_sha256 or parts[0].event_id
    for captured in (tuple(parts), tuple(reversed(parts))):
        result = assemble(captured)
        assert result.packages_read == 0
        assert result.packages_unreadable == (key,)
        assert result.request.records == ()
        assert result.request.newest_captured_at == CAPTURED
        confirmed = HandlerContextInstallConfirm().handle(result.request)
        (fact,) = confirmed.facts
        assert fact.fact is EnumInstallFact.UNCERTAIN
        assert fact.reason is EnumInstallFactReason.CAPTURE_GAP
        assert confirmed.pending_activation_ids == ()


def test_mixed_session_chunk_raises() -> None:
    part = chunks(package())[0].model_copy(update={"session_id": "other-session"})
    with pytest.raises(ValueError, match="session"):
        assemble((part,))


def test_conflicting_activation_hashes_raise() -> None:
    published = activation()
    conflict = published.model_copy(update={"installed_messages_hash": "0" * 64})
    for copies in ((published, conflict), (conflict, published)):
        with pytest.raises(ValueError, match="activation"):
            assemble((), copies)


def test_republished_activation_uses_earliest_publication_deterministically() -> None:
    published = activation()
    later = published.model_copy(update={"published_at": CAPTURED})
    assert assemble((), (later, published)).request.activations == (published,)
    assert assemble((), (published, later)).request.activations == (published,)


def test_equal_instants_with_different_offsets_serialize_deterministically() -> None:
    offset = timezone(timedelta(hours=-4))
    part = chunks(package())[0]
    offset_part = part.model_copy(
        update={"emitted_at": part.emitted_at.astimezone(offset)}
    )
    published = activation()
    offset_activation = published.model_copy(
        update={"published_at": published.published_at.astimezone(offset)}
    )
    expected = assemble((part, offset_part), (published, offset_activation))
    actual = assemble((offset_part, part), (offset_activation, published))
    assert actual.model_dump_json() == expected.model_dump_json()


def test_records_deduplicate_by_newest_package_then_package_key() -> None:
    original = package()
    changed = original.replace("acknowledged", "newer acknowledgement")
    old_parts = chunks(original, 3)
    new_part = chunks(changed)[0].model_copy(
        update={"emitted_at": CAPTURED + timedelta(seconds=1)}
    )
    for parts in ((*old_parts, new_part), (new_part, *reversed(old_parts))):
        result = assemble(parts)
        assert result.packages_read == 2
        assert len(result.request.records) == 5
        assert (
            next(r for r in result.request.records if r.uuid == "replacement-1").text
            == "newer acknowledgement"
        )
        assert result.request.newest_captured_at == new_part.emitted_at
    tied = chunks(changed)[0]
    winner = max((old_parts[0], tied), key=lambda chunk: chunk.content_sha256 or "")
    expected_text = "newer acknowledgement" if winner == tied else "acknowledged"
    for parts in ((*old_parts, tied), (tied, *reversed(old_parts))):
        result = assemble(parts)
        assert (
            next(r for r in result.request.records if r.uuid == "replacement-1").text
            == expected_text
        )


def test_readable_and_unreadable_packages_are_isolated_and_keys_sorted() -> None:
    valid = chunks(package())
    bad = valid[0].model_copy(update={"content_sha256": "0" * 64})
    null = valid[0].model_copy(
        update={"event_id": "null-package", "content_sha256": None}
    )
    later_null = null.model_copy(
        update={
            "event_id": "another-null",
            "emitted_at": CAPTURED + timedelta(seconds=2),
        }
    )
    result = assemble((null, *valid, bad, later_null))
    assert result.packages_read == 1
    assert len(result.request.records) == 5
    assert result.packages_unreadable == ("0" * 64, "another-null", "null-package")
    assert result.request.newest_captured_at == later_null.emitted_at


def test_empty_request_and_explicit_capture_timestamp() -> None:
    result = assemble((), ())
    assert result.request.records == result.request.activations == ()
    assert result.request.newest_captured_at is None
    assert result.packages_read == 0
    assert result.packages_unreadable == ()
    later = CAPTURED + timedelta(seconds=10)
    assert (
        assemble(chunks(package()), newest_captured_at=later).request.newest_captured_at
        == later
    )
    assert assemble((), newest_captured_at=later).request.newest_captured_at == later
    assert (
        assemble(chunks(package()), newest_captured_at=T0).request.newest_captured_at
        == CAPTURED
    )


def test_empty_lines_are_skipped() -> None:
    assert (
        assemble(chunks("\n" + package() + "\n")).request
        == assemble(chunks(package())).request
    )


def test_unicode_line_separator_inside_a_record_is_not_a_record_break() -> None:
    # Transcripts are written with raw Unicode; U+2028 inside a JSON string is
    # one record, though str.splitlines() would break the line there.
    text = package().replace("acknowledged", "acknowledged\u2028still one record")
    assembled = assemble(chunks(text, 2), activations=())
    assert assembled.packages_unreadable == ()
    assert assembled.packages_read == 1
    assert len(assembled.request.records) == len(package().splitlines())


@pytest.mark.parametrize(
    "updates",
    [
        {"event_id": ""},
        {"session_id": ""},
        {"chunk_index": -1},
        {"chunk_count": 0},
        {"chunk_index": 1},
    ],
)
def test_chunk_constraints(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelCapturedCompactChunk.model_validate(
            chunks(package())[0].model_dump() | updates
        )


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    models = (
        chunks(package())[0],
        ModelInstallConfirmAssemblyRequest(session_id=SESSION),
        assemble(()),
    )
    for model in models:
        assert model.model_config["frozen"] is True
        with pytest.raises(ValidationError):
            type(model).model_validate(model.model_dump() | {"unexpected": True})
    with pytest.raises(ValidationError):
        ModelInstallConfirmAssemblyRequest(session_id="")


def test_contract_metadata_handler_and_entry_point() -> None:
    path = NODE_DIR / "contract.yaml"
    contract = yaml.safe_load(path.read_text())
    discovery = discover_contracts_from_paths([path])
    assert not discovery.errors
    assert [node.name for node in discovery.contracts] == [NODE_NAME]
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["runtime_profiles"] == ["main"]
    assert contract["descriptor"]["purity"] == "pure"
    handler = contract["handler"]
    assert handler["class"] == HandlerContextInstallConfirmRequest.__name__
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerContextInstallConfirmRequest
    )
    assert (
        contract["input_model"]["name"] == ModelInstallConfirmAssemblyRequest.__name__
    )
    assert (
        contract["output_model"]["name"] == ModelInstallConfirmAssemblyResult.__name__
    )
    command = (
        "onex.cmd.omnimarket.context-install-confirm-request-assembly-requested.v1"
    )
    terminal = "onex.evt.omnimarket.context-install-confirm-request-assembled.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["runtime_dispatch"]["command_topic"] == command
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["terminal_event"] == terminal
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == terminal
    assert contract["metadata"]["ticket"] == "OMN-21336"
    assert contract["metadata"]["tags"] == ["context-compaction", "install-confirm"]
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == NODE_NAME
    assert metadata["tickets"] == ["OMN-21336"]
    entries = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert entries[NODE_NAME] == f"omnimarket.nodes.{NODE_NAME}"
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == entries[NODE_NAME]


def test_assembled_request_feeds_compaction_receipts_projection_writer() -> None:
    assembled = assemble(chunks(package(), 3))
    result = HandlerContextInstallConfirm().handle(assembled.request)
    projected = HandlerProjectionCompactionReceipts().handle(
        ModelCompactionReceiptsProjectionRequest(event=result)
    )
    (row,) = projected.rows
    boundary = next(
        record for record in assembled.request.records if record.is_boundary
    )
    assert row.receipt_key == f"boundary:{boundary.uuid}"
    assert row.boundary_uuid == boundary.uuid == BOUNDARY
    assert row.verdict is EnumInstallFact.INSTALLED
    assert row.activation_id == activation().activation_id
