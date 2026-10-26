# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic compaction pairing, prompt windows, replay pins and scoring (OMN-19998).

Every session, prompt, ledger row and case below is synthetic. No captured fixtures are used.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import context_compaction_bundle as bundle_io
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers import (
    handler_compaction_manifest as handlers,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models import (
    EnumBoundaryHook,
    EnumRepeatClass,
    EnumSummaryStatus,
    ModelBoundaryPairingRequest,
    ModelBuildManifestRequest,
    ModelBundleFile,
    ModelCapturedRecord,
    ModelCaseSpec,
    ModelExpectedStatement,
    ModelHookBoundaryEvent,
    ModelLedgerFile,
    ModelPostBoundaryEvidence,
    ModelPromptWindowRequest,
    ModelRepeatRequest,
    ModelVerifyBundleRequest,
)

pytestmark = pytest.mark.unit

SESSION = "session-a"
OTHER = "session-b"


def at(day: int, clock: str) -> datetime:
    """An instant on 2025-02-<day>, `HH:MM:SS`."""
    hour, minute, second = (int(part) for part in clock.split(":"))
    return datetime(2025, 2, day, hour, minute, second, 500000, tzinfo=UTC)


def boundary(
    event_id: str,
    hook: EnumBoundaryHook,
    moment: datetime,
    *,
    session: str = SESSION,
    record_id: str | None = None,
) -> ModelHookBoundaryEvent:
    summary = hook is EnumBoundaryHook.POST
    return ModelHookBoundaryEvent(
        event_id=event_id,
        session_id=session,
        hook_event_name=hook,
        emitted_at=moment,
        trigger="manual",
        model="synthetic-model",
        summary_length=9623 if summary else None,
        summary_sha256="d" * 64 if summary else None,
        summary_record_id=record_id if summary else None,
    )


def record(
    event_id: str,
    kind: str,
    moment: datetime,
    content: str,
    *,
    session: str = SESSION,
) -> ModelCapturedRecord:
    return ModelCapturedRecord(
        event_id=event_id,
        session_id=session,
        content_kind=kind,
        emitted_at=moment,
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


class TestPairing:
    """Synthetic pairing cases: the unpaired PreCompact is reported, and windows skip it."""

    def events(self) -> tuple[ModelHookBoundaryEvent, ...]:
        pre, post = EnumBoundaryHook.PRE, EnumBoundaryHook.POST
        return (
            boundary("pre-0", pre, at(14, "21:38:45")),
            boundary("post-0", post, at(14, "21:39:32"), record_id="r0"),
            boundary("pre-1", pre, at(15, "03:43:53")),
            boundary("pre-2", pre, at(15, "04:00:18")),
            boundary("post-2", post, at(15, "04:00:21"), record_id="r2"),
            boundary("pre-3", pre, at(15, "04:00:35")),
            boundary("post-3", post, at(15, "04:01:16"), record_id="r3"),
        )

    def pairing(self, resolvable: tuple[str, ...] = ()) -> Any:
        return handlers.HandlerBoundaryPairing().handle(
            ModelBoundaryPairingRequest(
                events=self.events(), resolvable_record_ids=resolvable
            )
        )

    def test_unpaired_pre_is_reported_not_dropped(self) -> None:
        result = self.pairing()
        assert result.unpaired_pre == ("pre-1",)
        assert result.unpaired_post == ()
        assert [pair.pre_event_id for pair in result.pairs if pair.paired] == [
            "pre-0",
            "pre-2",
            "pre-3",
        ]
        unpaired = next(pair for pair in result.pairs if pair.pre_event_id == "pre-1")
        assert unpaired.paired is False
        assert unpaired.post_event_id is None

    def test_window_starts_at_the_previous_paired_postcompact(self) -> None:
        by_id = {pair.pre_event_id: pair for pair in self.pairing().pairs}
        assert by_id["pre-0"].window_start is None
        # The unpaired PreCompact at 03:43:53 neither ends nor starts a window.
        assert by_id["pre-2"].window_start == at(14, "21:39:32")
        assert by_id["pre-3"].window_start == at(15, "04:00:21")
        assert by_id["pre-1"].window_start == at(14, "21:39:32")

    def test_windows_do_not_cross_sessions(self) -> None:
        pre, post = EnumBoundaryHook.PRE, EnumBoundaryHook.POST
        events = (
            boundary("a-pre", pre, at(10, "03:45:28"), session=OTHER),
            boundary("a-post", post, at(10, "03:46:51"), session=OTHER, record_id="ra"),
            boundary("b-pre", pre, at(13, "08:33:38")),
            boundary("b-post", post, at(13, "08:34:38"), record_id="rb"),
        )
        result = handlers.HandlerBoundaryPairing().handle(
            ModelBoundaryPairingRequest(events=events)
        )
        by_id = {pair.pre_event_id: pair for pair in result.pairs}
        assert by_id["b-pre"].window_start is None

    def test_a_summary_id_that_resolves_to_no_row_is_unavailable_not_empty(
        self,
    ) -> None:
        by_id = {pair.pre_event_id: pair for pair in self.pairing(("r2",)).pairs}
        assert by_id["pre-2"].summary_status is EnumSummaryStatus.RESOLVED
        assert by_id["pre-3"].summary_status is EnumSummaryStatus.UNAVAILABLE
        assert by_id["pre-1"].summary_status is EnumSummaryStatus.NOT_CAPTURED
        assert by_id["pre-3"].summary_sha256 == "d" * 64

    def test_a_postcompact_with_no_precompact_is_reported(self) -> None:
        events = (
            boundary(
                "orphan", EnumBoundaryHook.POST, at(12, "01:45:29"), record_id="r"
            ),
        )
        result = handlers.HandlerBoundaryPairing().handle(
            ModelBoundaryPairingRequest(events=events)
        )
        assert result.unpaired_post == ("orphan",)
        assert result.pairs == ()


class TestPromptWindow:
    """Synthetic prompt windows: `operator_prompt` where it exists; the heuristic reports its accuracy."""

    CAPTURE_START = datetime(2025, 2, 11, tzinfo=UTC)

    def window(
        self, records: tuple[ModelCapturedRecord, ...], start: datetime | None
    ) -> Any:
        return handlers.HandlerPromptWindow().handle(
            ModelPromptWindowRequest(
                records=records,
                window_start=start,
                window_end=at(15, "04:00:18"),
                operator_prompt_capture_start=self.CAPTURE_START,
            )
        )

    def test_operator_prompt_kind_is_exact_and_counts_once(self) -> None:
        records = (
            record(
                "p1",
                "prompt",
                at(15, "03:37:27"),
                "please keep the compaction summary decision",
            ),
            record(
                "o1",
                "operator_prompt",
                at(15, "03:37:50"),
                "please keep the compaction summary decision",
            ),
            record(
                "tick",
                "prompt",
                at(15, "03:40:00"),
                "Synthetic scheduled reminder: inspect the sample result",
            ),
        )
        result = self.window(records, at(14, "21:39:32"))
        assert result.exact is True
        assert result.operator_count == 1
        assert result.heuristic_precision is None

    def test_before_the_kind_existed_the_count_is_a_heuristic_with_its_measured_accuracy(
        self,
    ) -> None:
        tick = "Synthetic scheduled reminder: inspect the sample result"
        records = (
            record(
                "p1",
                "prompt",
                at(10, "01:15:56"),
                "please keep the merge queue decision",
            ),
            record("t1", "prompt", at(10, "02:00:00"), tick),
            record("t2", "prompt", at(10, "02:10:00"), tick),
            record("t3", "prompt", at(10, "02:20:00"), tick),
            record(
                "m1", "prompt", at(10, "02:30:00"), "<command-name>/loop</command-name>"
            ),
            record("s1", "prompt", at(10, "02:40:00"), "/compact"),
            record("big", "prompt", at(10, "02:50:00"), "x" * 1501),
        )
        result = handlers.HandlerPromptWindow().handle(
            ModelPromptWindowRequest(
                records=records,
                window_start=None,
                window_end=at(10, "03:45:28"),
                operator_prompt_capture_start=self.CAPTURE_START,
            )
        )
        assert result.exact is False
        assert [p.event_id for p in result.prompts if p.operator] == ["p1"]
        assert result.operator_count == 1
        assert (result.heuristic_precision, result.heuristic_recall) == (0.59, 0.93)

    def test_records_outside_the_window_are_not_counted(self) -> None:
        records = (
            record("early", "operator_prompt", at(14, "20:00:00"), "before the window"),
            record("in", "operator_prompt", at(15, "03:00:00"), "inside the window"),
            record("late", "operator_prompt", at(15, "05:00:00"), "after the boundary"),
        )
        result = self.window(records, at(14, "21:39:32"))
        assert [p.event_id for p in result.prompts] == ["in"]


class TestRepeatClassification:
    """Synthetic repeat controls: a repeat with no boundary between is a control, never compaction loss."""

    def events(self) -> tuple[ModelHookBoundaryEvent, ...]:
        pre, post = EnumBoundaryHook.PRE, EnumBoundaryHook.POST
        return (
            boundary("pre-a", pre, at(14, "16:22:39")),
            boundary("post-a", post, at(14, "16:23:28"), record_id="a"),
            boundary("pre-b", pre, at(14, "21:38:45")),
            boundary("post-b", post, at(14, "21:39:32"), record_id="b"),
            boundary("pre-c", pre, at(13, "23:27:48")),
            boundary("post-c", post, at(13, "23:29:14"), record_id="c"),
        )

    def classify(self, first: datetime, repeat: datetime) -> Any:
        return handlers.HandlerRepeatClassifier().handle(
            ModelRepeatRequest(
                session_id=SESSION,
                first_at=first,
                repeat_at=repeat,
                events=self.events(),
            )
        )

    def test_repeat_with_no_boundary_between_is_the_control(self) -> None:
        # Both synthetic statements fall between adjacent boundaries.
        verdict = self.classify(at(14, "19:41:52"), at(14, "21:30:38"))
        assert verdict.verdict is EnumRepeatClass.NO_BOUNDARY_BETWEEN
        assert verdict.boundaries_between == ()

    def test_repeat_after_a_boundary_names_it(self) -> None:
        # One paired boundary falls between the two synthetic statements.
        verdict = self.classify(at(13, "22:42:38"), at(13, "23:38:00"))
        assert verdict.verdict is EnumRepeatClass.AFTER_BOUNDARY
        assert verdict.boundaries_between == ("pre-c",)

    def test_another_sessions_boundary_does_not_count(self) -> None:
        verdict = handlers.HandlerRepeatClassifier().handle(
            ModelRepeatRequest(
                session_id=OTHER,
                first_at=at(13, "22:42:38"),
                repeat_at=at(13, "23:38:00"),
                events=self.events(),
            )
        )
        assert verdict.verdict is EnumRepeatClass.NO_BOUNDARY_BETWEEN


# ---------------------------------------------------------------------------------------------
# The builder and the verifier, over a synthetic world.

LEDGER_ROLL = (
    b"2025-02-12T00:00:01Z | STATUS | lane=synthetic-lane-a | an old row\n"
    b"2025-02-12T00:00:02Z | RULING | lane=synthetic-lane-a | ticket=OMN-19998 | "
    b'"retain the synthetic archive decision"\n'
)
FIRST_RULING = "2025-02-14T21:00:00Z"
LEDGER_LIVE = (
    f"{FIRST_RULING} | RULING | lane=synthetic-lane-a | ticket=OMN-19998 | "
    '"please keep the merge queue decision" | note\n'
    "2025-02-14T21:30:00Z | STATUS | lane=synthetic-lane-b | a status row\n"
    "2025-02-15T00:30:00Z | RULING | lane=synthetic-lane-b | ticket=OMN-19998 | "
    '"please keep the compaction summary decision" | note\n'
    "2025-02-15T01:00:00Z | STATUS | lane=synthetic-lane-b | written after the boundary\n"
).encode()


def world() -> tuple[ModelBuildManifestRequest, ModelCaseSpec]:
    pre, post = EnumBoundaryHook.PRE, EnumBoundaryHook.POST
    events = (
        boundary("pre-1", pre, at(15, "00:40:00")),
        boundary("post-1", post, at(15, "00:41:00"), record_id="rec-1"),
        boundary("pre-2", pre, at(15, "00:50:00")),
        boundary("post-2", post, at(15, "00:51:00"), record_id="rec-2"),
        boundary("pre-other", pre, at(15, "00:45:00"), session=OTHER),
    )
    records = (
        record(
            "p-merge",
            "prompt",
            at(14, "20:59:10"),
            "please keep the merge queue decision for this example",
        ),
        record(
            "p-mod",
            "prompt",
            at(15, "00:30:05"),
            "please keep the compaction summary decision",
        ),
        record(
            "a-1",
            "assistant_reply",
            at(15, "00:30:30"),
            "Synthetic acknowledgement, proceeding.",
        ),
        record(
            "p-late",
            "prompt",
            at(15, "00:55:00"),
            "please repeat the compaction summary decision",
        ),
        record(
            "p-unrecorded",
            "prompt",
            at(14, "22:00:00"),
            "please preserve the retry budget decision across compaction",
        ),
        record(
            "p-other",
            "prompt",
            at(15, "00:44:00"),
            "another session's words",
            session=OTHER,
        ),
    )
    spec = ModelCaseSpec(
        case_id="case-a",
        finding="synthetic instruction loss",
        census_status="CONFIRMED",
        census_ref="synthetic census, case-a",
        session_id=SESSION,
        boundary_pre_event_ids=("pre-1", "pre-2"),
        expected_statements=(
            ModelExpectedStatement(
                statement_id="S1",
                prompt_event_id="p-mod",
                ledger_stamp="2025-02-15T00:30:00Z",
                quote="please keep the compaction summary decision",
            ),
            ModelExpectedStatement(
                statement_id="S2",
                prompt_event_id="p-unrecorded",
                required_spans=("please preserve the retry budget",),
            ),
        ),
        post_boundary_evidence=(
            ModelPostBoundaryEvidence(
                event_id="p-late",
                kind="RESTATED",
                required_spans=("repeat the compaction summary decision",),
            ),
        ),
        question="Which decision should the synthetic replay retain?",
        expected_answer="Keep the compaction summary decision.",
    )
    request = ModelBuildManifestRequest(
        specs=(spec,),
        events=events,
        records=records,
        ledger_files=(
            ModelLedgerFile(name="synthetic-archive.md", content=LEDGER_ROLL),
            ModelLedgerFile(name="synthetic-ledger.md", content=LEDGER_LIVE),
        ),
        resolvable_record_ids=(),
        census_ref="synthetic census",
    )
    return request, spec


def build(request: ModelBuildManifestRequest | None = None) -> Any:
    return handlers.HandlerReplayManifestBuilder().handle(request or world()[0])


def case_of(result: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = yaml.safe_load(result.manifest_yaml)
    case: dict[str, Any] = manifest["cases"][0]
    return case


def bundle_file(result: Any, suffix: str) -> bytes:
    (found,) = [f for f in result.bundle_files if f.path.endswith(suffix)]
    content: bytes = found.content
    return content


def native_world(*, chunks: int = 1) -> tuple[ModelBuildManifestRequest, bytes]:
    request, _ = world()
    summary = "Synthetic native summary: preserve the retry budget. ✓"
    package = (
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in (
                {"type": "system", "subtype": "compact_boundary", "uuid": "boundary-2"},
                {
                    "type": "user",
                    "isCompactSummary": True,
                    "message": {"content": summary},
                },
            )
        )
        + "\n"
    )
    split = len(package) // chunks
    records = tuple(
        record("compact-2", "compact", at(15, "00:51:01"), part).model_copy(
            update={
                "chunk_index": i,
                "chunk_count": chunks,
                "content_sha256": hashlib.sha256(package.encode()).hexdigest(),
            }
        )
        for i, part in enumerate(
            (package,) if chunks == 1 else (package[:split], package[split:])
        )
    )
    request = request.model_copy(
        update={
            "events": tuple(
                event.model_copy(
                    update={
                        "summary_length": len(summary),
                        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
                    }
                )
                if event.event_id == "post-2"
                else event
                for event in request.events
            ),
            "records": request.records + records,
        }
    )
    return request, summary.encode()


def test_native_summary_reassembles_real_projection_rows_with_distinct_ids() -> None:
    from omnimarket.nodes.node_projection_session_content.handlers.session_content_fold import (
        fold_session_content,
    )
    from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
        ModelSessionContentRecord,
    )

    request, summary = native_world(chunks=2)
    captures = [r for r in request.records if r.content_kind == "compact"]
    rows = [
        fold_session_content(
            ModelSessionContentRecord.model_validate(
                {
                    "session_id": r.session_id,
                    "content_kind": r.content_kind,
                    "chunk_index": r.chunk_index,
                    "chunk_count": r.chunk_count,
                    "emitted_at": r.emitted_at,
                    "content": r.content,
                    "content_sha256": r.content_sha256,
                }
            )
        )
        for r in captures
    ]
    assert len({row.event_id for row in rows}) == 2
    projected = tuple(
        r.model_copy(update={"event_id": row.event_id})
        for r, row in zip(captures, rows, strict=True)
    )
    result = build(
        request.model_copy(
            update={
                "records": tuple(
                    r for r in request.records if r.content_kind != "compact"
                )
                + projected,
            }
        )
    )
    pin = case_of(result)["boundaries"][-1]["summary"]
    assert pin["status"] == "RESOLVED"
    assert {file.path: file.content for file in result.bundle_files}[
        pin["bundle_path"]
    ] == summary


@pytest.mark.parametrize("chunks", [1, 2])
def test_native_summary_is_exported_after_the_last_precompact(
    chunks: int, tmp_path: Path
) -> None:
    request, summary = native_world(chunks=chunks)
    result = build(request)
    pin = case_of(result)["boundaries"][-1]["summary"]
    assert pin["status"] == "RESOLVED"
    assert pin["bytes"] == len(summary)
    assert pin["sha256"] == hashlib.sha256(summary).hexdigest()
    assert {f.path: f.content for f in result.bundle_files}[
        pin["bundle_path"]
    ] == summary
    assert summary not in bundle_file(result, "transcript_chain.jsonl")
    bundle_io.write_bundle_files(tmp_path, result.bundle_files)
    assert (tmp_path / pin["bundle_path"]).read_bytes() == summary
    assert (
        handlers.HandlerReplayBundleVerifier()
        .handle(
            ModelVerifyBundleRequest(
                manifest_yaml=result.manifest_yaml,
                bundle_files=bundle_io.read_bundle_files(tmp_path),
            )
        )
        .ok
    )


def test_resolvable_id_without_summary_bytes_is_still_unavailable() -> None:
    request, _ = world()
    result = build(request.model_copy(update={"resolvable_record_ids": ("rec-2",)}))
    assert (
        case_of(result)["boundaries"][-1]["summary"]["status"] == "SUMMARY_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "failure", ["wrong_session", "incomplete", "wrong_hash", "duplicate", "malformed"]
)
def test_native_summary_export_requires_complete_matching_capture(failure: str) -> None:
    request, _ = native_world(chunks=2)
    records = list(request.records)
    if failure == "wrong_session":
        records = [
            r.model_copy(update={"session_id": OTHER})
            if r.content_kind == "compact"
            else r
            for r in records
        ]
    elif failure == "incomplete":
        records.pop()
    elif failure == "duplicate":
        records.append(records[-1])
    elif failure == "malformed":
        records[-1] = records[-1].model_copy(update={"content": "not json\n"})
    else:
        records[-1] = records[-1].model_copy(
            update={"content": records[-1].content.replace("retry", "other")}
        )
    result = build(request.model_copy(update={"records": tuple(records)}))
    assert (
        case_of(result)["boundaries"][-1]["summary"]["status"] == "SUMMARY_UNAVAILABLE"
    )


@pytest.mark.parametrize("tamper", ["missing", "content", "pin"])
def test_verifier_refuses_resolved_native_summary_without_valid_bytes(
    tamper: str,
) -> None:
    request, summary = native_world()
    result = build(request)
    manifest = yaml.safe_load(result.manifest_yaml)
    pin = manifest["cases"][0]["boundaries"][-1]["summary"]
    # Construct the expected producer output so the verifier regression fails independently.
    path = "native/test-summary.txt"
    pin.update(status="RESOLVED", bundle_path=path, bytes=len(summary))
    files = tuple(f for f in result.bundle_files if f.path != pin.get("bundle_path"))
    if tamper != "missing":
        files += (
            ModelBundleFile(
                path=path, content=summary if tamper == "pin" else b"tampered"
            ),
        )
    if tamper == "pin":
        pin.pop("bundle_path")
    verdict = handlers.HandlerReplayBundleVerifier().handle(
        ModelVerifyBundleRequest(
            manifest_yaml=yaml.safe_dump(manifest), bundle_files=files
        )
    )
    assert not verdict.ok
    assert any("native summary" in failure for failure in verdict.failures)


class TestBuilder:
    def test_transcript_chain_is_pinned_to_the_last_boundary_and_one_session(
        self,
    ) -> None:
        result = build()
        chain = bundle_file(result, "transcript_chain.jsonl")
        lines = [json.loads(line) for line in chain.decode().splitlines()]
        ids = [line["event_id"] for line in lines]
        assert "p-late" not in ids  # after the last PreCompact
        assert "p-other" not in ids  # another session
        assert ids[-1] == "pre-2"  # the chain ends at the boundary, included
        assert "post-1" in ids
        assert "p-mod" in ids
        pin = case_of(result)["transcript_chain"]
        assert pin["sha256"] == hashlib.sha256(chain).hexdigest()
        assert pin["bytes"] == len(chain)
        assert pin["records"] == len(lines)

    def test_ledger_prefix_stops_before_the_boundary_second(self) -> None:
        result = build()
        files = {f["name"]: f for f in case_of(result)["ledger"]["files"]}
        roll = files["synthetic-archive.md"]
        live = files["synthetic-ledger.md"]
        assert roll["prefix_bytes"] == len(LEDGER_ROLL)
        assert roll["prefix_sha256"] == hashlib.sha256(LEDGER_ROLL).hexdigest()
        # The first boundary is 00:40:00 on 02-15, the last 00:50:00; the pin is the last one.
        expected = LEDGER_LIVE[: LEDGER_LIVE.index(b"2025-02-15T01:00:00Z")]
        assert live["prefix_bytes"] == len(expected)
        assert live["prefix_sha256"] == hashlib.sha256(expected).hexdigest()
        assert live["last_stamp"] == "2025-02-15T00:30:00Z"

    def test_a_row_in_the_boundary_second_is_not_in_the_prefix(self) -> None:
        marker = LEDGER_LIVE.index(b"2025-02-15T01:00:00Z")
        same_second = b"2025-02-15T00:50:00Z | STATUS | lane=synthetic-lane-a | written in the boundary second\n"
        ledger = LEDGER_LIVE[:marker] + same_second + LEDGER_LIVE[marker:]
        request, _spec = world()
        request = request.model_copy(
            update={
                "ledger_files": (
                    ModelLedgerFile(name="synthetic-ledger.md", content=ledger),
                )
            }
        )
        live = case_of(build(request))["ledger"]["files"][0]
        assert live["prefix_bytes"] == marker

    def test_an_instruction_recorded_only_after_the_first_boundary_counts_as_unrecorded(
        self,
    ) -> None:
        request, spec = world()
        late = spec.expected_statements[1].model_copy(
            update={"ledger_stamp": "2025-02-15T00:30:00Z"}
        )
        changed = spec.model_copy(
            update={"expected_statements": (spec.expected_statements[0], late)}
        )
        # The ruling row is stamped 00:30, before the first boundary at 00:40: recorded in time.
        assert (
            case_of(build(request.model_copy(update={"specs": (changed,)})))[
                "unrecorded_instruction_before_first_boundary"
            ]
            is False
        )
        # Recorded only at the second boundary: the first boundary had no row for it.
        moved = request.model_copy(
            update={
                "events": tuple(
                    e.model_copy(
                        update={"emitted_at": e.emitted_at.replace(hour=0, minute=20)}
                    )
                    if e.event_id == "pre-1"
                    else e
                    for e in request.events
                )
            }
        )
        flag = case_of(build(moved.model_copy(update={"specs": (changed,)})))
        assert flag["unrecorded_instruction_before_first_boundary"] is True

    def test_both_boundaries_and_the_unrecorded_instruction_are_marked(self) -> None:
        case = case_of(build())
        assert [b["pre_event_id"] for b in case["boundaries"]] == ["pre-1", "pre-2"]
        assert case["two_boundaries"] is True
        assert case["unrecorded_instruction_before_first_boundary"] is True
        assert case["boundaries"][0]["summary"]["status"] == "SUMMARY_UNAVAILABLE"

    def test_scoring_is_in_the_manifest_with_exact_words(self) -> None:
        scoring = case_of(build())["scoring"]
        first, second = scoring["expected_statements"]
        assert first["quote"] == "please keep the compaction summary decision"
        assert (
            first["prompt_sha256"]
            == hashlib.sha256(first["quote"].encode()).hexdigest()
        )
        assert second["ledger_stamp"] is None
        assert scoring["question"].startswith("Which decision")
        assert scoring["expected_answer"] == "Keep the compaction summary decision."

    def test_one_session_carrying_two_lanes_keeps_both(self) -> None:
        # Synthetic ruling quotes match prompts of one session under two synthetic lanes.
        assert case_of(build())["lanes"] == ["synthetic-lane-a", "synthetic-lane-b"]

    def test_checkpoint_and_branch_are_excluded_with_their_reason(self) -> None:
        case = case_of(build())
        assert case["checkpoint"]["status"] == "EXCLUDED"
        assert case["checkpoint"]["factor"] == "ORCHESTRATION_STATE"
        assert case["branch"]["status"] == "EXCLUDED"
        assert case["branch"]["factor"] == "REPO_STATE"
        assert case["model_id"] == "synthetic-model"
        assert case["profile_version"] is None

    def test_build_is_deterministic(self) -> None:
        first, second = build(), build()
        assert first.manifest_yaml == second.manifest_yaml
        assert {(f.path, f.content) for f in first.bundle_files} == {
            (f.path, f.content) for f in second.bundle_files
        }

    def test_a_quote_that_is_not_in_the_prompt_is_refused(self) -> None:
        request, spec = world()
        bad = spec.model_copy(
            update={
                "expected_statements": (
                    spec.expected_statements[0].model_copy(
                        update={"quote": "words nobody said"}
                    ),
                )
            }
        )
        with pytest.raises(handlers.ManifestRefusalError, match="words nobody said"):
            build(request.model_copy(update={"specs": (bad,)}))

    def test_a_ruling_stamp_missing_from_the_prefix_is_refused(self) -> None:
        request, spec = world()
        bad = spec.model_copy(
            update={
                "expected_statements": (
                    spec.expected_statements[0].model_copy(
                        update={"ledger_stamp": "2025-02-15T01:00:00Z"}
                    ),
                )
            }
        )
        with pytest.raises(handlers.ManifestRefusalError, match="2025-02-15T01:00:00Z"):
            build(request.model_copy(update={"specs": (bad,)}))

    def test_a_statement_made_after_the_last_boundary_is_refused(self) -> None:
        request, spec = world()
        bad = spec.model_copy(
            update={
                "expected_statements": (
                    spec.expected_statements[0].model_copy(
                        update={
                            "prompt_event_id": "p-late",
                            "quote": "repeat the compaction summary decision",
                        }
                    ),
                )
            }
        )
        with pytest.raises(handlers.ManifestRefusalError, match="p-late"):
            build(request.model_copy(update={"specs": (bad,)}))

    def test_an_unknown_boundary_event_is_refused(self) -> None:
        request, spec = world()
        bad = spec.model_copy(update={"boundary_pre_event_ids": ("pre-nope",)})
        with pytest.raises(handlers.ManifestRefusalError, match="pre-nope"):
            build(request.model_copy(update={"specs": (bad,)}))


class TestVerifier:
    def verify(self, files: tuple[ModelBundleFile, ...], manifest_yaml: str) -> Any:
        return handlers.HandlerReplayBundleVerifier().handle(
            ModelVerifyBundleRequest(manifest_yaml=manifest_yaml, bundle_files=files)
        )

    def test_a_fresh_bundle_reproduces_every_pin(self) -> None:
        result = build()
        verdict = self.verify(result.bundle_files, result.manifest_yaml)
        assert verdict.failures == ()
        assert verdict.ok is True
        assert verdict.cases_checked == 1

    def test_a_changed_byte_in_the_chain_is_a_hash_mismatch(self) -> None:
        # The positive control for the zero above: the same check fails on a planted change.
        result = build()
        planted = tuple(
            f.model_copy(
                update={
                    "content": f.content.replace(b"acknowledgement", b"acknowledgemenx")
                }
            )
            if f.path.endswith("transcript_chain.jsonl")
            else f
            for f in result.bundle_files
        )
        verdict = self.verify(planted, result.manifest_yaml)
        assert verdict.ok is False
        assert any(
            "case-a" in failure and "sha256" in failure for failure in verdict.failures
        )

    def test_a_missing_ledger_prefix_file_fails(self) -> None:
        result = build()
        kept = tuple(f for f in result.bundle_files if not f.path.endswith(".prefix"))
        verdict = self.verify(kept, result.manifest_yaml)
        assert verdict.ok is False
        assert any("missing" in failure for failure in verdict.failures)

    def test_a_ruling_stamp_absent_from_the_pinned_prefix_fails(self) -> None:
        result = build()
        manifest = yaml.safe_load(result.manifest_yaml)
        manifest["cases"][0]["scoring"]["expected_statements"][0]["ledger_stamp"] = (
            "2025-02-15T00:30:01Z"
        )
        verdict = self.verify(
            result.bundle_files, yaml.safe_dump(manifest, sort_keys=False)
        )
        assert verdict.ok is False
        assert any("2025-02-15T00:30:01Z" in failure for failure in verdict.failures)

    def test_a_quote_not_in_the_pinned_chain_fails(self) -> None:
        result = build()
        manifest = yaml.safe_load(result.manifest_yaml)
        manifest["cases"][0]["scoring"]["expected_statements"][0]["quote"] = (
            "words nobody said"
        )
        verdict = self.verify(
            result.bundle_files, yaml.safe_dump(manifest, sort_keys=False)
        )
        assert verdict.ok is False
        assert any("words nobody said" in failure for failure in verdict.failures)


class TestReplayRoot:
    def test_the_root_is_required_with_no_default(self) -> None:
        with pytest.raises(
            handlers.ManifestRefusalError, match="ONEX_CONTEXT_REPLAY_ROOT"
        ):
            bundle_io.replay_root_from_env({})
        with pytest.raises(
            handlers.ManifestRefusalError, match="ONEX_CONTEXT_REPLAY_ROOT"
        ):
            bundle_io.replay_root_from_env({"ONEX_CONTEXT_REPLAY_ROOT": ""})

    def test_a_bundle_written_to_the_root_reads_back_to_the_same_files(
        self, tmp_path: Path
    ) -> None:
        result = build()
        bundle_io.write_bundle_files(tmp_path, result.bundle_files)
        read = bundle_io.read_bundle_files(
            bundle_io.replay_root_from_env({"ONEX_CONTEXT_REPLAY_ROOT": str(tmp_path)})
        )
        assert {(f.path, f.content) for f in read} == {
            (f.path, f.content) for f in result.bundle_files
        }

    def test_a_path_that_leaves_the_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(handlers.ManifestRefusalError, match="outside"):
            bundle_io.write_bundle_files(
                tmp_path, (ModelBundleFile(path="../escape", content=b"x"),)
            )


def synthetic_manifest_request() -> ModelBuildManifestRequest:
    """Three independent synthetic sessions, each with scoring fixed before building."""
    original, _ = world()
    specs = []
    events = []
    records = []
    for label in ("a", "b", "c"):
        prefix = f"{label}-"
        session_id = f"session-{label}"
        spec = original.specs[0]
        specs.append(
            spec.model_copy(
                update={
                    "case_id": f"case-{label}",
                    "session_id": session_id,
                    "boundary_pre_event_ids": tuple(
                        prefix + event_id for event_id in spec.boundary_pre_event_ids
                    ),
                    "expected_statements": tuple(
                        statement.model_copy(
                            update={
                                "prompt_event_id": prefix + statement.prompt_event_id
                            }
                        )
                        for statement in spec.expected_statements
                    ),
                    "post_boundary_evidence": tuple(
                        evidence.model_copy(
                            update={"event_id": prefix + evidence.event_id}
                        )
                        for evidence in spec.post_boundary_evidence
                    ),
                }
            )
        )
        events.extend(
            event.model_copy(
                update={"event_id": prefix + event.event_id, "session_id": session_id}
            )
            for event in original.events
            if event.session_id == SESSION
        )
        records.extend(
            row.model_copy(
                update={"event_id": prefix + row.event_id, "session_id": session_id}
            )
            for row in original.records
            if row.session_id == SESSION
        )
    return original.model_copy(
        update={
            "specs": tuple(specs),
            "events": tuple(events),
            "records": tuple(records),
        }
    )


class TestSyntheticManifest:
    """Acceptance invariants over an in-test manifest built solely from synthetic inputs."""

    def manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = yaml.safe_load(
            build(synthetic_manifest_request()).manifest_yaml
        )
        return manifest

    def test_at_least_three_cases_with_a_confirmed_lost_or_contradicted_ruling(
        self,
    ) -> None:
        confirmed = [
            c for c in self.manifest()["cases"] if c["census_status"] == "CONFIRMED"
        ]
        assert len(confirmed) >= 3
        assert {case["session_id"] for case in confirmed} == {
            "session-a",
            "session-b",
            "session-c",
        }

    def test_one_case_has_two_boundaries_and_an_unrecorded_instruction_before_the_first(
        self,
    ) -> None:
        assert any(
            case["two_boundaries"]
            and case["unrecorded_instruction_before_first_boundary"]
            for case in self.manifest()["cases"]
        )

    def test_every_case_carries_its_scoring_and_its_pins(self) -> None:
        for case in self.manifest()["cases"]:
            assert case["scoring"]["question"]
            assert case["scoring"]["expected_answer"]
            assert case["scoring"]["expected_statements"]
            assert len(case["transcript_chain"]["sha256"]) == 64
            assert case["ledger"]["files"]
            assert case["checkpoint"]["status"] in {"PINNED", "EXCLUDED"}
            assert case["branch"]["status"] in {"PINNED", "EXCLUDED"}

    def test_every_listed_ruling_stamp_precedes_the_last_boundary(self) -> None:
        for case in self.manifest()["cases"]:
            last_pre = case["boundaries"][-1]["pre_at"]
            for statement in case["scoring"]["expected_statements"]:
                stamp = statement["ledger_stamp"]
                if stamp is not None:
                    assert stamp[:19] < last_pre[:19]

    def test_the_scoring_is_the_hand_written_spec_unchanged(self) -> None:
        request = synthetic_manifest_request()
        cases = {case["case_id"]: case for case in self.manifest()["cases"]}
        assert [spec.case_id for spec in request.specs] == list(cases)
        for spec in request.specs:
            scoring = cases[spec.case_id]["scoring"]
            assert scoring["question"] == spec.question
            assert scoring["expected_answer"] == spec.expected_answer
            assert [s["statement_id"] for s in scoring["expected_statements"]] == [
                s.statement_id for s in spec.expected_statements
            ]
            assert [s["quote"] for s in scoring["expected_statements"]] == [
                s.quote for s in spec.expected_statements
            ]
            assert [e["event_id"] for e in scoring["post_boundary_evidence"]] == [
                e.event_id for e in spec.post_boundary_evidence
            ]
            assert [
                b["pre_event_id"] for b in cases[spec.case_id]["boundaries"]
            ] == sorted(
                spec.boundary_pre_event_ids,
                key=lambda eid: next(
                    b["pre_at"]
                    for b in cases[spec.case_id]["boundaries"]
                    if b["pre_event_id"] == eid
                ),
            )

    def test_no_absolute_directory_path_and_no_transcript_read(self) -> None:
        result = build(synthetic_manifest_request())
        manifest = yaml.safe_load(result.manifest_yaml)
        assert all(not Path(file.path).is_absolute() for file in result.bundle_files)
        assert manifest["source"]["transcripts_read"] is False

    def test_second_build_is_byte_identical_and_all_cases_verify(
        self, tmp_path: Path
    ) -> None:
        request = synthetic_manifest_request()
        first, second = build(request), build(request)
        assert first.manifest_yaml.encode() == second.manifest_yaml.encode()
        assert first.bundle_files == second.bundle_files
        bundle_io.write_bundle_files(tmp_path, first.bundle_files)
        verdict = handlers.HandlerReplayBundleVerifier().handle(
            ModelVerifyBundleRequest(
                manifest_yaml=first.manifest_yaml,
                bundle_files=bundle_io.read_bundle_files(tmp_path),
            )
        )
        assert verdict.ok
        assert verdict.cases_checked == 3
        assert verdict.failures == ()
