# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen, synthetic transcript corpus for manifest decisions and reproducibility."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    ManifestRefusalError,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_transcript_manifest import (
    HandlerTranscriptManifest,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_transcript_manifest import (
    EnumCaseExclusion,
    ModelCheckpointSnapshot,
    ModelObligationCount,
    ModelTranscriptManifestRequest,
)

from .test_transcript_jsonl_manifest import (
    LEDGER,
    OPERATOR_WORDS,
    SESSION,
    START,
    boundary,
    long_transcript,
    pins,
    request,
    summary,
    summary_text,
    three_boundary_transcript,
    transcript,
    user,
)

pytestmark = pytest.mark.unit


def test_first_boundary_small_window_is_recompaction() -> None:
    moment = START + timedelta(hours=1)
    body = user("u1", START, OPERATOR_WORDS)
    body += boundary("b1", moment, pre_tokens=99_999)
    body += summary("s1", moment, summary_text(1))
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    assert result.cases == ()
    assert result.exclusions[0].reason is EnumCaseExclusion.RECOMPACTION


def test_recompaction_is_per_session_across_transcript_files() -> None:
    first = user("u1", START, OPERATOR_WORDS)
    first += boundary("b1", START + timedelta(hours=1))
    first += summary("s1", START + timedelta(hours=1), summary_text(1))
    second = boundary("b2", START + timedelta(hours=1, minutes=2))
    second += summary("s2", START + timedelta(hours=1, minutes=2), summary_text(2))
    # Lexical path order deliberately differs from boundary time order.
    result = HandlerTranscriptManifest().handle(
        request((transcript(second, "a.jsonl"), transcript(first, "z.jsonl")))
    )
    assert [case.boundary_uuid for case in result.cases] == ["b1"]
    assert [(item.boundary_uuid, item.reason) for item in result.exclusions] == [
        ("b2", EnumCaseExclusion.RECOMPACTION)
    ]


def test_missing_obligation_evidence_is_excluded() -> None:
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),), obligation_counts=())
    )
    assert result.cases == ()
    assert dict(result.exclusion_counts) == {"NO_OBLIGATION": 2, "RECOMPACTION": 1}


def test_ledger_secret_is_scanned_as_part_of_frozen_bundle() -> None:
    # Synthetic credential shape, constructed to avoid storing a real credential.
    ledger = LEDGER + b"2026-10-20T08:11:00Z | MSG | token sk-" + b"a1" * 15 + b"\n"
    result = HandlerTranscriptManifest().handle(
        request(
            (transcript(three_boundary_transcript()),),
            ledger_files=[{"name": "ledger.md", "content": ledger}],
        )
    )
    assert result.cases == ()
    assert dict(result.exclusion_counts) == {"SECRET_IN_PREFIX": 2, "RECOMPACTION": 1}
    assert "a1" * 15 not in result.manifest_yaml


def test_every_selected_case_has_reproducible_prefix_and_ledger_bytes() -> None:
    body = three_boundary_transcript()
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    files = {file.path: file.content for file in result.bundle_files}
    for case in result.cases:
        prefix = files[case.prefix_bundle_path]
        assert prefix == body[: case.prefix_bytes]
        assert hashlib.sha256(prefix).hexdigest() == case.prefix_sha256
        assert (
            hashlib.sha256(files[case.boundary_bundle_path]).hexdigest()
            == case.boundary_sha256
        )
        assert (
            hashlib.sha256(files[case.summary_record_bundle_path]).hexdigest()
            == case.summary_record_sha256
        )
        for cut in case.ledger_cuts:
            assert (
                hashlib.sha256(files[cut.bundle_path]).hexdigest() == cut.prefix_sha256
            )
            assert files[cut.bundle_path] == LEDGER[: cut.prefix_bytes]


def test_per_session_cap_cannot_exceed_three() -> None:
    with pytest.raises(ValidationError, match="per_session_cap"):
        ModelTranscriptManifestRequest(transcripts=(), seed="fixed", per_session_cap=4)


def test_duplicate_transcript_paths_are_refused_instead_of_losing_boundaries() -> None:
    file = transcript(three_boundary_transcript())
    with pytest.raises(ManifestRefusalError, match="duplicate transcript path"):
        HandlerTranscriptManifest().handle(request((file, file)))


def test_ac2_same_bytes_give_identical_keys_and_bundle() -> None:
    source = request((transcript(three_boundary_transcript()),))
    first = HandlerTranscriptManifest().handle(source)
    again = HandlerTranscriptManifest().handle(source)
    assert len(first.cases) == 2
    assert [case.case_key for case in first.cases] == [
        case.case_key for case in again.cases
    ]
    assert first.manifest_yaml == again.manifest_yaml
    assert first.bundle_files == again.bundle_files


def test_obligation_counts_can_be_scoped_to_session() -> None:
    other = "22222222-aaaa-4bbb-8ccc-000000000002"
    body = three_boundary_transcript()
    counts = tuple(
        ModelObligationCount(
            session_id=session,
            boundary_uuid=f"b{i}",
            must_survive_items=5 if session == SESSION else 4,
        )
        for session in (SESSION, other)
        for i in (1, 2, 3)
    )
    other_pins = tuple(pin.model_copy(update={"session_id": other}) for pin in pins())
    result = HandlerTranscriptManifest().handle(
        request(
            (
                transcript(body, "one.jsonl"),
                transcript(body.replace(SESSION.encode(), other.encode()), "two.jsonl"),
            ),
            summary_pins=(*pins(), *other_pins),
            obligation_counts=counts,
        )
    )
    assert {case.session_id for case in result.cases} == {SESSION}
    assert (
        sum(
            item.reason is EnumCaseExclusion.NO_OBLIGATION for item in result.exclusions
        )
        == 2
    )


def test_ac1_fixture_accounts_for_cases_and_every_exclusion_reason() -> None:
    sources = []
    summary_pins = []
    counts = []
    # Each source is a distinct synthetic session. The draw session also exercises NOT_DRAWN.
    kinds = ("eligible", "summary", "ledger", "obligation", "secret", "draw")
    for kind in kinds:
        session = f"session-{kind}"
        if kind == "eligible":
            body = three_boundary_transcript()
            size = 3
        elif kind == "draw":
            body = long_transcript(5)
            size = 5
        else:
            instant = (
                START - timedelta(days=1)
                if kind == "ledger"
                else START + timedelta(hours=1)
            )
            text = "synthetic sk-" + "x9" * 15 if kind == "secret" else OPERATOR_WORDS
            body = user("u1", START - timedelta(days=2), text)
            body += boundary("b1", instant)
            body += summary(
                "s1", instant, "tampered" if kind == "summary" else summary_text(1)
            )
            size = 1
        sources.append(
            transcript(
                body.replace(SESSION.encode(), session.encode()), f"{kind}.jsonl"
            )
        )
        summary_pins.extend(
            pin.model_copy(update={"session_id": session}) for pin in pins(size)
        )
        counts.extend(
            ModelObligationCount(
                session_id=session,
                boundary_uuid=f"b{i}",
                must_survive_items=4 if kind == "obligation" else 5,
            )
            for i in range(1, size + 1)
        )
    result = HandlerTranscriptManifest().handle(
        request(
            tuple(sources),
            summary_pins=tuple(summary_pins),
            obligation_counts=tuple(counts),
        )
    )
    assert len(result.cases) == 5
    assert dict(result.exclusion_counts) == {
        "RECOMPACTION": 1,
        "SUMMARY_UNVERIFIED": 1,
        "LEDGER_UNPINNABLE": 1,
        "NO_OBLIGATION": 1,
        "SECRET_IN_PREFIX": 1,
        "NOT_DRAWN": 2,
    }
    for source in result.transcripts:
        assert source.cases + source.exclusions == source.boundary_records
        assert source.boundary_records == source.lines_mentioning_boundary
    identities = [(case.session_id, case.boundary_uuid) for case in result.cases]
    identities.extend(
        (item.session_id, item.boundary_uuid) for item in result.exclusions
    )
    assert len(identities) == len(set(identities)) == 12


def test_checkpoint_snapshot_is_hashed_and_available_as_frozen_bytes() -> None:
    snapshot = ModelCheckpointSnapshot(
        session_id=SESSION,
        boundary_uuid="b1",
        captured_at=START + timedelta(minutes=59),
        content=b"synthetic checkpoint",
    )
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),), checkpoints=(snapshot,))
    )
    checkpoint = result.cases[0].checkpoint
    assert checkpoint.status == "PINNED"
    assert checkpoint.sha256 == hashlib.sha256(snapshot.content).hexdigest()
    files = {file.path: file.content for file in result.bundle_files}
    assert files[checkpoint.bundle_path] == snapshot.content
    assert result.cases[1].checkpoint.status == "UNAVAILABLE"


def test_future_checkpoint_snapshot_is_refused() -> None:
    snapshot = ModelCheckpointSnapshot(
        session_id=SESSION,
        boundary_uuid="b1",
        captured_at=START + timedelta(hours=2),
        content=b"future checkpoint",
    )
    with pytest.raises(ManifestRefusalError, match="after the boundary"):
        HandlerTranscriptManifest().handle(
            request((transcript(three_boundary_transcript()),), checkpoints=(snapshot,))
        )


def test_malformed_boundary_is_refused_instead_of_silently_omitted() -> None:
    with pytest.raises(ManifestRefusalError, match="unparseable compact_boundary"):
        HandlerTranscriptManifest().handle(
            request(
                (
                    transcript(
                        three_boundary_transcript() + b"{broken compact_boundary\n"
                    ),
                )
            )
        )


def test_summary_of_another_session_cannot_verify_a_boundary() -> None:
    instant = START + timedelta(hours=1)
    body = user("u1", START, OPERATOR_WORDS) + boundary("b1", instant)
    body += summary("s1", instant, summary_text(1), session="another-session")
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    assert result.cases == ()
    assert result.exclusions[0].reason is EnumCaseExclusion.SUMMARY_UNVERIFIED


def test_secret_after_boundary_is_neither_exported_nor_an_exclusion() -> None:
    moment = START + timedelta(hours=1)
    body = user("u1", START, OPERATOR_WORDS) + boundary("b1", moment)
    body += summary("s1", moment, summary_text(1))
    body += user("future", START + timedelta(hours=2), "token sk-" + "x9" * 15)
    ledger = LEDGER + b"2026-10-20T12:00:00Z | MSG | token sk-" + b"x9" * 15 + b"\n"
    result = HandlerTranscriptManifest().handle(
        request(
            (transcript(body),), ledger_files=[{"name": "ledger.md", "content": ledger}]
        )
    )
    assert len(result.cases) == 1
    assert result.exclusions == ()
    assert all(b"x9" * 15 not in file.content for file in result.bundle_files)


@pytest.mark.parametrize("field", ["uuid", "sessionId"])
def test_boundary_without_stable_identity_is_refused(field: str) -> None:
    record = json.loads(boundary("b1", START + timedelta(hours=1)))
    del record[field]
    with pytest.raises(ManifestRefusalError, match="requires sessionId and uuid"):
        HandlerTranscriptManifest().handle(
            request((transcript(json.dumps(record).encode() + b"\n"),))
        )


def test_json_credential_assignment_in_prefix_is_excluded() -> None:
    moment = START + timedelta(hours=1)
    text = json.dumps({"api_key": "x9" * 15})
    body = user("u1", START, text) + boundary("b1", moment)
    body += summary("s1", moment, summary_text(1))
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    assert result.cases == ()
    assert result.exclusions[0].reason is EnumCaseExclusion.SECRET_IN_PREFIX
