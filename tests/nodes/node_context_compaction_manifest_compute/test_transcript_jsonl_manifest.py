# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Case manifests from transcript files: the `transcript_jsonl` source (plan task CM.1).

Every transcript, ledger row and summary below is synthetic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import context_compaction_bundle as bundle_io
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    ManifestRefusalError,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_transcript_manifest import (
    HandlerTranscriptManifest,
    MissingRequiredVariableError,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_compaction_manifest import (
    ModelLedgerFile,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_transcript_manifest import (
    EnumCaseExclusion,
    ModelObligationCount,
    ModelSummaryPin,
    ModelTranscriptFile,
    ModelTranscriptManifestRequest,
)

pytestmark = pytest.mark.unit

SESSION = "11111111-aaaa-4bbb-8ccc-000000000001"
START = datetime(2026, 10, 20, 8, 0, 0, tzinfo=UTC)
NODE = Path(__file__).resolve().parents[3] / (
    "src/omnimarket/nodes/node_context_compaction_manifest_compute"
)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True) + "\n").encode()


def user(uuid: str, moment: datetime, text: str, session: str = SESSION) -> bytes:
    return line(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": session,
            "timestamp": stamp(moment),
            "message": {"role": "user", "content": text},
        }
    )


def assistant(uuid: str, moment: datetime, text: str, session: str = SESSION) -> bytes:
    return line(
        {
            "type": "assistant",
            "uuid": uuid,
            "sessionId": session,
            "timestamp": stamp(moment),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def boundary(
    uuid: str,
    moment: datetime,
    *,
    trigger: str = "auto",
    pre_tokens: int = 180_000,
    session: str = SESSION,
) -> bytes:
    return line(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": uuid,
            "sessionId": session,
            "timestamp": stamp(moment),
            "compactMetadata": {
                "trigger": trigger,
                "preTokens": pre_tokens,
                "postTokens": 9_000,
            },
        }
    )


def summary(uuid: str, moment: datetime, text: str, session: str = SESSION) -> bytes:
    return line(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": session,
            "timestamp": stamp(moment),
            "isCompactSummary": True,
            "message": {"role": "user", "content": text},
        }
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


OPERATOR_WORDS = "Do not merge anything until the landing controller is ready"
LEDGER = (
    "2026-10-20T08:05:00Z | RULING | lane=orchestrator-test | ticket=OMN-1 | "
    f'"{OPERATOR_WORDS}" | recorded\n'
    "2026-10-20T08:10:00Z | MSG | lane=orchestrator-test | text\n"
).encode()


def summary_text(index: int) -> str:
    return f"This session is being continued. Summary number {index}."


def three_boundary_transcript(*, tamper: bool = False) -> bytes:
    """Boundaries at +60m and +120m (two cases) and +122m (a re-compaction)."""
    at = START
    body = user("u1", at, OPERATOR_WORDS) + assistant(
        "a1", at + timedelta(minutes=1), "ok"
    )
    body += user("u2", at + timedelta(minutes=30), "keep going")
    body += boundary("b1", at + timedelta(minutes=60), trigger="auto")
    body += summary("s1", at + timedelta(minutes=60), summary_text(1))
    body += user("u3", at + timedelta(minutes=90), "next step")
    body += boundary("b2", at + timedelta(minutes=120), trigger="manual")
    body += summary(
        "s2", at + timedelta(minutes=120), "tampered" if tamper else summary_text(2)
    )
    body += boundary("b3", at + timedelta(minutes=122), trigger="auto")
    body += summary("s3", at + timedelta(minutes=122), summary_text(3))
    return body


def pins(count: int = 3) -> tuple[ModelSummaryPin, ...]:
    return tuple(
        ModelSummaryPin(
            session_id=SESSION,
            boundary_uuid=f"b{i}",
            summary_length=len(summary_text(i)),
            summary_sha256=sha(summary_text(i)),
        )
        for i in range(1, count + 1)
    )


def request(
    transcripts: tuple[ModelTranscriptFile, ...],
    **overrides: Any,
) -> ModelTranscriptManifestRequest:
    values: dict[str, Any] = {
        "transcripts": transcripts,
        "ledger_files": [{"name": "ROLLING_WORK_LEDGER.md", "content": LEDGER}],
        "summary_pins": pins(),
        "obligation_counts": tuple(
            ModelObligationCount(boundary_uuid=f"b{i}", must_survive_items=5)
            for i in range(1, 10)
        ),
        "seed": "seed-1",
    }
    values.update(overrides)
    return ModelTranscriptManifestRequest.model_validate(values)


def transcript(body: bytes, name: str = "proj/one.jsonl") -> ModelTranscriptFile:
    return ModelTranscriptFile(relative_path=name, content=body)


def test_three_boundaries_one_recompaction_yields_two_cases() -> None:
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),))
    )
    assert [case.boundary_uuid for case in result.cases] == ["b1", "b2"]
    assert [(item.boundary_uuid, item.reason) for item in result.exclusions] == [
        ("b3", EnumCaseExclusion.RECOMPACTION)
    ]
    assert dict(result.exclusion_counts) == {"RECOMPACTION": 1}


def test_every_boundary_is_in_exactly_one_list() -> None:
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),))
    )
    listed = sorted(
        [case.boundary_uuid for case in result.cases]
        + [item.boundary_uuid for item in result.exclusions]
    )
    [counts] = result.transcripts
    assert counts.boundary_records == 3
    assert counts.lines_mentioning_boundary == 3
    assert listed == ["b1", "b2", "b3"]
    assert counts.cases + counts.exclusions == counts.boundary_records


def test_recompaction_by_small_window_alone() -> None:
    body = user("u1", START, OPERATOR_WORDS)
    body += boundary("b1", START + timedelta(minutes=60))
    body += summary("s1", START + timedelta(minutes=60), summary_text(1))
    body += boundary("b2", START + timedelta(minutes=180), pre_tokens=60_000)
    body += summary("s2", START + timedelta(minutes=180), summary_text(2))
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    assert [case.boundary_uuid for case in result.cases] == ["b1"]
    assert [item.reason for item in result.exclusions] == [
        EnumCaseExclusion.RECOMPACTION
    ]


def test_tampered_summary_yields_summary_unverified() -> None:
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript(tamper=True)),))
    )
    reasons = {item.boundary_uuid: item.reason for item in result.exclusions}
    assert reasons["b2"] is EnumCaseExclusion.SUMMARY_UNVERIFIED
    assert [case.boundary_uuid for case in result.cases] == ["b1"]


def test_missing_summary_record_yields_summary_unverified() -> None:
    body = user("u1", START, OPERATOR_WORDS)
    body += boundary("b1", START + timedelta(minutes=60))
    body += user("u2", START + timedelta(minutes=61), "no summary followed")
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    assert [item.reason for item in result.exclusions] == [
        EnumCaseExclusion.SUMMARY_UNVERIFIED
    ]
    assert result.cases == ()


def test_unpinned_summary_is_excluded_as_unverified() -> None:
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),), summary_pins=())
    )
    assert result.cases == ()
    assert dict(result.exclusion_counts) == {
        "SUMMARY_UNVERIFIED": 2,
        "RECOMPACTION": 1,
    }
    pinned = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),))
    )
    assert {case.summary_check for case in pinned.cases} == {"PINNED"}


def test_same_bytes_give_identical_case_keys_and_manifest() -> None:
    first = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),))
    )
    second = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),))
    )
    assert [c.case_key for c in first.cases] == [c.case_key for c in second.cases]
    assert len({c.case_key for c in first.cases}) == 2
    assert first.manifest_yaml == second.manifest_yaml


def test_changed_prefix_bytes_change_the_case_key() -> None:
    base = three_boundary_transcript()
    changed = base.replace(b"keep going", b"keep goin9")
    first = HandlerTranscriptManifest().handle(request((transcript(base),)))
    second = HandlerTranscriptManifest().handle(request((transcript(changed),)))
    assert first.cases[0].case_key != second.cases[0].case_key
    assert first.cases[0].prefix_sha256 != second.cases[0].prefix_sha256


def test_case_pins_cover_boundary_summary_and_prefix_bytes() -> None:
    body = three_boundary_transcript()
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    first = result.cases[0]
    boundary_line = next(
        raw for raw in body.splitlines(keepends=True) if b'"uuid": "b1"' in raw
    )
    summary_line = next(
        raw for raw in body.splitlines(keepends=True) if b'"uuid": "s1"' in raw
    )
    offset = body.index(boundary_line)
    assert first.prefix_bytes == offset
    assert first.prefix_sha256 == hashlib.sha256(body[:offset]).hexdigest()
    assert first.boundary_sha256 == hashlib.sha256(boundary_line).hexdigest()
    assert first.summary_record_sha256 == hashlib.sha256(summary_line).hexdigest()
    assert first.summary_sha256 == sha(summary_text(1))
    assert first.summary_length == len(summary_text(1))
    assert (first.trigger, first.pre_tokens, first.post_tokens) == (
        "auto",
        180_000,
        9_000,
    )


def test_lane_family_attribution_records_its_evidence() -> None:
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),))
    )
    attribution = result.cases[0].lane_attribution
    assert attribution.lanes == ("orchestrator-test",)
    [evidence] = attribution.evidence
    assert evidence.ruling_stamp == "2026-10-20T08:05:00Z"
    assert evidence.quote_sha256 == sha(OPERATOR_WORDS)
    assert "RULING quote" in attribution.rule


def test_no_matching_ruling_leaves_the_lane_unattributed_with_that_stated() -> None:
    body = user("u1", START, "something else entirely, unrelated to any ruling")
    body += boundary("b1", START + timedelta(minutes=60))
    body += summary("s1", START + timedelta(minutes=60), summary_text(1))
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    attribution = result.cases[0].lane_attribution
    assert attribution.lanes == ()
    assert attribution.evidence == ()


def test_secret_in_prefix_is_excluded() -> None:
    body = user("u1", START, "token sk-" + "a1" * 15)
    body += boundary("b1", START + timedelta(minutes=60))
    body += summary("s1", START + timedelta(minutes=60), summary_text(1))
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    assert [item.reason for item in result.exclusions] == [
        EnumCaseExclusion.SECRET_IN_PREFIX
    ]
    assert "a1" * 15 not in result.manifest_yaml


def test_ledger_unpinnable_without_a_row_before_the_boundary() -> None:
    late = b"2026-10-21T07:30:00Z | MSG | lane=x | later\n"
    result = HandlerTranscriptManifest().handle(
        request(
            (transcript(three_boundary_transcript()),),
            ledger_files=[{"name": "ROLLING_WORK_LEDGER.md", "content": late}],
        )
    )
    assert result.cases == ()
    assert {item.reason for item in result.exclusions} == {
        EnumCaseExclusion.LEDGER_UNPINNABLE,
        EnumCaseExclusion.RECOMPACTION,
    }


def test_no_obligation_when_supplied_count_is_below_five() -> None:
    counts = (
        ModelObligationCount(boundary_uuid="b1", must_survive_items=4),
        ModelObligationCount(boundary_uuid="b2", must_survive_items=5),
    )
    result = HandlerTranscriptManifest().handle(
        request((transcript(three_boundary_transcript()),), obligation_counts=counts)
    )
    assert [case.boundary_uuid for case in result.cases] == ["b2"]
    reasons = {item.boundary_uuid: item.reason for item in result.exclusions}
    assert reasons["b1"] is EnumCaseExclusion.NO_OBLIGATION


def long_transcript(boundaries: int) -> bytes:
    body = user("u1", START, OPERATOR_WORDS)
    for index in range(1, boundaries + 1):
        moment = START + timedelta(hours=index)
        body += boundary(
            f"b{index}",
            moment,
            trigger="manual" if index % 2 else "auto",
            pre_tokens=150_000 + index * 1_000,
        )
        body += summary(f"s{index}", moment, summary_text(index))
    return body


def test_seeded_draw_caps_boundaries_per_session_and_is_reproducible() -> None:
    body = long_transcript(7)
    first = HandlerTranscriptManifest().handle(
        request((transcript(body),), summary_pins=pins(7))
    )
    again = HandlerTranscriptManifest().handle(
        request((transcript(body),), summary_pins=pins(7))
    )
    assert len(first.cases) == 3
    assert (
        len([i for i in first.exclusions if i.reason is EnumCaseExclusion.NOT_DRAWN])
        == 4
    )
    assert [c.boundary_uuid for c in first.cases] == [
        c.boundary_uuid for c in again.cases
    ]
    triggers = {c.trigger for c in first.cases}
    assert triggers == {"manual", "auto"}


def test_seed_changes_the_draw_but_not_the_case_keys_of_a_boundary() -> None:
    body = long_transcript(9)
    keys: dict[str, str] = {}
    drawn: set[tuple[str, ...]] = set()
    for seed in ("s-a", "s-b", "s-c", "s-d", "s-e"):
        result = HandlerTranscriptManifest().handle(
            request((transcript(body),), summary_pins=pins(9), seed=seed)
        )
        drawn.add(tuple(c.boundary_uuid for c in result.cases))
        for case in result.cases:
            assert keys.setdefault(case.boundary_uuid, case.case_key) == case.case_key
    assert len(drawn) > 1


def test_draw_does_not_depend_on_transcript_order() -> None:
    one = transcript(long_transcript(6), "p/one.jsonl")
    two_body = long_transcript(6).replace(
        SESSION.encode(), b"22222222-aaaa-4bbb-8ccc-000000000002"
    )
    two = transcript(two_body, "p/two.jsonl")
    forward = HandlerTranscriptManifest().handle(request((one, two), summary_pins=()))
    backward = HandlerTranscriptManifest().handle(request((two, one), summary_pins=()))
    assert forward.manifest_yaml == backward.manifest_yaml


def test_unparseable_lines_are_counted_not_fatal() -> None:
    body = three_boundary_transcript() + b"{not json\n"
    result = HandlerTranscriptManifest().handle(request((transcript(body),)))
    [counts] = result.transcripts
    assert counts.unparseable_lines == 1
    assert counts.boundary_records == 3
    assert counts.lines_mentioning_boundary == 3


def test_unset_transcript_root_names_the_variable() -> None:
    with pytest.raises(MissingRequiredVariableError) as caught:
        bundle_io.transcript_root_from_env({})
    assert caught.value.variable == "ONEX_TRANSCRIPT_ROOT"
    assert "ONEX_TRANSCRIPT_ROOT" in str(caught.value)
    assert isinstance(caught.value, ManifestRefusalError)


def test_unset_replay_root_names_the_variable() -> None:
    with pytest.raises(MissingRequiredVariableError) as caught:
        bundle_io.replay_root_from_env({})
    assert caught.value.variable == "ONEX_CONTEXT_REPLAY_ROOT"
    with pytest.raises(MissingRequiredVariableError):
        bundle_io.replay_root_from_env({"ONEX_CONTEXT_REPLAY_ROOT": "  "})


def test_unset_replay_root_refuses_the_whole_run_before_reading(tmp_path: Path) -> None:
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "one.jsonl").write_bytes(three_boundary_transcript())
    with pytest.raises(MissingRequiredVariableError) as caught:
        bundle_io.build_transcript_manifest_from_env(
            {"ONEX_TRANSCRIPT_ROOT": str(transcripts)},
            ledger_files=(),
            seed="s",
        )
    assert caught.value.variable == "ONEX_CONTEXT_REPLAY_ROOT"
    with pytest.raises(MissingRequiredVariableError) as caught:
        bundle_io.build_transcript_manifest_from_env(
            {"ONEX_CONTEXT_REPLAY_ROOT": str(tmp_path / "replay")},
            ledger_files=(),
            seed="s",
        )
    assert caught.value.variable == "ONEX_TRANSCRIPT_ROOT"


def test_env_driven_run_reads_transcripts_and_writes_the_manifest(
    tmp_path: Path,
) -> None:
    transcripts = tmp_path / "transcripts"
    (transcripts / "proj").mkdir(parents=True)
    (transcripts / "proj" / "one.jsonl").write_bytes(three_boundary_transcript())
    (transcripts / "proj" / "note.txt").write_text("not a transcript")
    replay = tmp_path / "replay"
    env = {
        "ONEX_TRANSCRIPT_ROOT": str(transcripts),
        "ONEX_CONTEXT_REPLAY_ROOT": str(replay),
    }
    ledger = (ModelLedgerFile(name="ROLLING_WORK_LEDGER.md", content=LEDGER),)
    result = bundle_io.build_transcript_manifest_from_env(
        env,
        ledger_files=ledger,
        seed="seed-1",
        summary_pins=pins(),
        obligation_counts=tuple(
            ModelObligationCount(boundary_uuid=f"b{i}", must_survive_items=5)
            for i in range(1, 4)
        ),
    )
    assert [case.boundary_uuid for case in result.cases] == ["b1", "b2"]
    written = replay / "transcript_manifest.yaml"
    assert written.read_text() == result.manifest_yaml
    document = yaml.safe_load(written.read_text())
    assert document["transcripts"][0]["relative_path"] == "proj/one.jsonl"
    assert [case["case_key"] for case in document["cases"]] == [
        case.case_key for case in result.cases
    ]


def test_contract_routes_the_transcript_operation() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    operations = {
        item["operation"]: item for item in contract["handler_routing"]["handlers"]
    }
    entry = operations["build_transcript_manifest"]
    assert entry["handler"]["name"] == "HandlerTranscriptManifest"
    assert entry["handler"]["module"].endswith("handlers.handler_transcript_manifest")
    assert entry["input_model"].endswith("ModelTranscriptManifestRequest")
    assert entry["output_model"].endswith("ModelTranscriptManifestResult")
