# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session context sources, pinned mode (OMN-21108, plan task TA.6).

The carry rule over a fixture table, and pinned reads of a replay bundle that the T0.2 manifest
builder writes. Every session, prompt and ledger row below is synthetic.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.handlers import context_compaction_bundle as bundle_io
from omnimarket.nodes.node_context_compaction_manifest_compute.handlers.handler_compaction_manifest import (
    HandlerReplayManifestBuilder,
)
from omnimarket.nodes.node_context_compaction_manifest_compute.models import (
    EnumBoundaryHook,
    ModelBuildManifestRequest,
    ModelCapturedRecord,
    ModelCaseSpec,
    ModelExpectedStatement,
    ModelHookBoundaryEvent,
    ModelLedgerFile,
    ModelPostBoundaryEvidence,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.carry_rule import (
    carry,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.handler_session_context_sources import (
    REPLAY_ROOT_ENV,
    HandlerSessionContextSources,
)
from omnimarket.nodes.node_session_context_sources_effect.models import (
    EnumCarryDisposition,
    EnumPromptOrigin,
    EnumSessionSourceFailure,
    ModelPromptCitation,
    ModelReviewedRange,
    ModelSessionPrompt,
)

pytestmark = pytest.mark.unit

SESSION = "session-a"
OWU = EnumContextFactor.OPERATOR_WORDS_UNRECORDED
SUMMARY = "This session is being continued from a previous conversation. Summary: ..."
UNRECORDED = "please preserve the retry budget across compaction"


def at(clock: str) -> datetime:
    hour, minute, second = (int(part) for part in clock.split(":"))
    return datetime(2025, 2, 15, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# The carry rule, as a fixture table


def prompt(prompt_id: str, sequence: int, text: str) -> ModelSessionPrompt:
    return ModelSessionPrompt(
        prompt_id=prompt_id,
        capture_sequence=sequence,
        text=text,
        said_at=at("00:00:00").replace(minute=sequence),
        origin=EnumPromptOrigin.OPERATOR_PROMPT,
        source_ref=f"chain#{prompt_id}",
    )


def cite(
    prompt_id: str, start: int, end: int, row: str = "row-1"
) -> ModelPromptCitation:
    return ModelPromptCitation(prompt_id=prompt_id, start=start, end=end, row_ref=row)


TWO = "merge the release PR. then rotate nothing"
CARRY_TABLE = [
    # (case, prompt text, citations as (start, end), expected disposition)
    ("two instructions, one ruled", TWO, [(0, 20)], EnumCarryDisposition.CARRIED),
    (
        "cited, whitespace residual",
        "  ship it  \n",
        [(2, 9)],
        EnumCarryDisposition.RECORDED,
    ),
    ("cited, fully covered", "ship it", [(0, 7)], EnumCarryDisposition.RECORDED),
    (
        "cited by two rows, union covers all",
        "ship it",
        [(0, 4), (4, 7)],
        EnumCarryDisposition.RECORDED,
    ),
    ("uncited", "ship it", [], EnumCarryDisposition.CARRIED),
    ("uncited checkmark", "✓", [], EnumCarryDisposition.CARRIED),
    ("uncited cross", "✗", [], EnumCarryDisposition.CARRIED),
    ("uncited thumbs-up emoji", "\U0001f44d", [], EnumCarryDisposition.CARRIED),
    (
        "cited, lone symbol residual",
        "ship it !",
        [(0, 8)],
        EnumCarryDisposition.CARRIED,
    ),
    (
        "cited, lone emoji residual",
        "ship it \U0001f44d",
        [(0, 8)],
        EnumCarryDisposition.CARRIED,
    ),
    ("span past the end covers nothing", "ok", [(0, 9)], EnumCarryDisposition.CARRIED),
]


@pytest.mark.parametrize(
    ("case", "text", "spans", "expected"),
    CARRY_TABLE,
    ids=[row[0] for row in CARRY_TABLE],
)
def test_carry_rule_fixture_table(
    case: str, text: str, spans: list[tuple[int, int]], expected: EnumCarryDisposition
) -> None:
    (decision,) = carry([prompt("p1", 0, text)], [cite("p1", s, e) for s, e in spans])
    assert decision.disposition is expected, case
    # Carried means whole and verbatim, annotated with the covering rows.
    assert decision.prompt.text == text
    assert [(c.start, c.end) for c in decision.covered_by] == sorted(spans)


def test_a_citation_of_another_prompt_does_not_drop_this_one() -> None:
    (decision,) = carry([prompt("p1", 0, "ship it")], [cite("p2", 0, 7)])
    assert decision.disposition is EnumCarryDisposition.CARRIED


FIVE = [prompt(f"p{n}", n, f"instruction {n}") for n in range(1, 6)]


def test_reviewed_range_removes_the_second_to_the_fourth_of_five() -> None:
    decisions = carry(
        FIVE,
        [],
        [ModelReviewedRange(first_prompt_id="p2", last_prompt_id="p4", row_ref="r")],
    )
    assert [d.disposition for d in decisions] == [
        EnumCarryDisposition.CARRIED,
        EnumCarryDisposition.REVIEWED,
        EnumCarryDisposition.REVIEWED,
        EnumCarryDisposition.REVIEWED,
        EnumCarryDisposition.CARRIED,
    ]
    assert {d.reviewed_by for d in decisions if d.reviewed_by} == {"r"}


@pytest.mark.parametrize(
    ("first", "last"),
    [("p4", "p2"), ("p2", "p-unknown"), ("p-unknown", "p4")],
    ids=["reversed", "unknown last", "unknown first"],
)
def test_a_malformed_review_range_names_nothing(first: str, last: str) -> None:
    decisions = carry(
        FIVE,
        [],
        [ModelReviewedRange(first_prompt_id=first, last_prompt_id=last, row_ref="r")],
    )
    assert all(d.disposition is EnumCarryDisposition.CARRIED for d in decisions)


def test_carry_is_in_capture_order_whatever_the_input_order() -> None:
    decisions = carry(list(reversed(FIVE)), [])
    assert [d.prompt.prompt_id for d in decisions] == ["p1", "p2", "p3", "p4", "p5"]


# ---------------------------------------------------------------------------------------------
# Pinned reads of a replay bundle


def boundary(
    event_id: str, hook: EnumBoundaryHook, moment: datetime
) -> ModelHookBoundaryEvent:
    post = hook is EnumBoundaryHook.POST
    return ModelHookBoundaryEvent(
        event_id=event_id,
        session_id=SESSION,
        hook_event_name=hook,
        emitted_at=moment,
        trigger="manual",
        model="synthetic-model",
        summary_length=len(SUMMARY) if post else None,
        summary_sha256=hashlib.sha256(SUMMARY.encode()).hexdigest() if post else None,
        summary_record_id=f"rec-{event_id}" if post else None,
    )


def record(
    event_id: str, kind: str, moment: datetime, content: str
) -> ModelCapturedRecord:
    return ModelCapturedRecord(
        event_id=event_id,
        session_id=SESSION,
        content_kind=kind,
        emitted_at=moment,
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


LEDGER = (
    b"# synthetic ledger\n"
    b"2025-02-15T00:05:00Z | STATUS | lane=synthetic-lane-a | started\n"
    b"{ruled}"
    b"2025-02-15T01:00:00Z | RULING | lane=synthetic-lane-a | prompt_id=op-mid | covers=0:1 | written after the last boundary\n"
)


def world(ruled: bytes = b"") -> ModelBuildManifestRequest:
    pre, post = EnumBoundaryHook.PRE, EnumBoundaryHook.POST
    events = (
        boundary("pre-1", pre, at("00:40:00")),
        boundary("post-1", post, at("00:41:00")),
        boundary("pre-2", pre, at("00:50:00")),
        boundary("post-2", post, at("00:51:00")),
    )
    records = (
        # Said at 00:10 (UserPromptSubmit), journalled at the 00:11 Stop as operator-typed.
        record("p-early", "prompt", at("00:10:00"), UNRECORDED),
        record("op-early", "operator_prompt", at("00:11:00"), UNRECORDED),
        # A lane brief: a Stop read after it classified other words and not these.
        record("p-brief", "prompt", at("00:12:00"), "# Lane synthetic brief"),
        record("p-ok", "prompt", at("00:12:30"), "ok"),
        record("op-ok", "operator_prompt", at("00:13:00"), "ok"),
        # Typed while a turn ran: only the Stop record exists.
        record("op-during", "operator_prompt", at("00:20:00"), "also keep the canary"),
        record("a-1", "assistant_reply", at("00:21:00"), "Synthetic acknowledgement."),
        # The installed compaction summary, delivered as a prompt-kind record.
        record("p-summary", "prompt", at("00:41:30"), SUMMARY),
        record("op-mid", "operator_prompt", at("00:45:00"), "✓"),
        # A turn that never reached Stop before the boundary.
        record("p-tail", "prompt", at("00:48:00"), "unconfirmed tail words"),
        record("p-late", "prompt", at("00:55:00"), "please repeat the canary decision"),
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
                prompt_event_id="op-early",
                required_spans=("preserve the retry budget",),
            ),
        ),
        post_boundary_evidence=(
            ModelPostBoundaryEvidence(
                event_id="p-late", kind="RESTATED", required_spans=("canary",)
            ),
        ),
        question="Which instruction should the synthetic replay retain?",
        expected_answer="Preserve the retry budget.",
    )
    return ModelBuildManifestRequest(
        specs=(spec,),
        events=events,
        records=records,
        ledger_files=(
            ModelLedgerFile(
                name="synthetic-ledger.md", content=LEDGER.replace(b"{ruled}", ruled)
            ),
        ),
        census_ref="synthetic census",
    )


def write_bundle(root: Path, ruled: bytes = b"") -> str:
    """Write manifest.yaml and the bundle files; return case-a's transcript-chain sha256."""
    built = HandlerReplayManifestBuilder().handle(world(ruled))
    bundle_io.write_bundle_files(root, built.bundle_files)
    (root / "manifest.yaml").write_text(built.manifest_yaml, encoding="utf-8")
    manifest: dict[str, Any] = yaml.safe_load(built.manifest_yaml)
    digest: str = manifest["cases"][0]["transcript_chain"]["sha256"]
    return digest


def read(
    root: Path | None,
    revision: str,
    *,
    factor: EnumContextFactor = OWU,
    scopes: tuple[str, ...] = ("case:case-a",),
) -> ModelContextSourceResult:
    env = {} if root is None else {REPLAY_ROOT_ENV: str(root)}
    return HandlerSessionContextSources(env=env).handle(
        ModelContextSourceRequest(
            factor=factor,
            scopes=scopes,
            access=ModelContextAccess(tenant_id="tenant-a"),
            revision=revision,
        )
    )


def failure_of(result: ModelContextSourceResult) -> str:
    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumContextSourceErrorCode.INVALID_REQUEST
    assert result.error_message is not None
    return result.error_message.split(":", 1)[0]


def texts(result: ModelContextSourceResult) -> list[str]:
    return [entry.text for entry in result.entries]


def test_pinned_read_carries_operator_words_across_both_boundaries(
    tmp_path: Path,
) -> None:
    result = read(tmp_path, write_bundle(tmp_path))
    assert result.status is EnumContextSourceStatus.RESOLVED
    assert texts(result) == [
        UNRECORDED,
        "ok",
        "also keep the canary",
        "✓",
        "unconfirmed tail words",
    ]
    assert dict(result.entry_counts) == {
        "carried": 5,
        "recorded": 0,
        "reviewed": 0,
        "unconfirmed_origin": 1,
    }


def test_two_boundary_case_has_the_early_instruction_and_no_summary_text(
    tmp_path: Path,
) -> None:
    result = read(tmp_path, write_bundle(tmp_path))
    assert UNRECORDED in texts(result)
    assert all(SUMMARY not in text for text in texts(result))
    assert all("Lane synthetic brief" not in text for text in texts(result))
    # Words after the case's last boundary are not in the pinned chain.
    assert all("repeat the canary" not in text for text in texts(result))


def test_entries_are_verbatim_with_source_span_and_said_instant(tmp_path: Path) -> None:
    result = read(tmp_path, write_bundle(tmp_path))
    first = result.entries[0]
    assert first.factor is OWU
    assert first.verbatim_verified is True
    assert first.source_ref == "case-a/transcript_chain.jsonl#op-early"
    assert first.span is not None
    assert (first.span.start, first.span.end) == (0, len(UNRECORDED))
    assert first.text_sha256 == hashlib.sha256(UNRECORDED.encode()).hexdigest()
    assert first.as_of == at("00:10:00")


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    digest = write_bundle(tmp_path)
    assert (
        read(tmp_path, digest).model_dump_json()
        == read(tmp_path, digest).model_dump_json()
    )


def test_a_pinned_ruling_covering_a_whole_prompt_drops_it(tmp_path: Path) -> None:
    ruled = (
        b"2025-02-15T00:30:00Z | RULING | lane=synthetic-lane-a | prompt_id=op-during | "
        b"covers=0:20 | also keep the canary\n"
    )
    result = read(tmp_path, write_bundle(tmp_path, ruled))
    assert "also keep the canary" not in texts(result)
    assert result.entry_counts["recorded"] == 1


def test_a_pinned_review_row_removes_its_range(tmp_path: Path) -> None:
    ruled = (
        b"2025-02-15T00:30:00Z | RULING | lane=synthetic-lane-a | "
        b"reviewed=op-ok..op-during | reviewed with nothing further to record\n"
    )
    result = read(tmp_path, write_bundle(tmp_path, ruled))
    assert texts(result) == [UNRECORDED, "✓", "unconfirmed tail words"]
    assert result.entry_counts["reviewed"] == 2


def test_a_ruling_written_after_the_boundary_is_absent(tmp_path: Path) -> None:
    # The fixture ledger's 01:00 row cites op-mid and covers all of it; it is past the cut.
    result = read(tmp_path, write_bundle(tmp_path))
    assert "✓" in texts(result)
    assert result.entry_counts["recorded"] == 0


@pytest.mark.parametrize("env_value", [None, ""], ids=["unset", "empty"])
def test_missing_replay_root_is_a_typed_error(env_value: str | None) -> None:
    env = {} if env_value is None else {REPLAY_ROOT_ENV: env_value}
    result = HandlerSessionContextSources(env=env).handle(
        ModelContextSourceRequest(
            factor=OWU,
            scopes=("case:case-a",),
            access=ModelContextAccess(tenant_id="tenant-a"),
            revision="0" * 64,
        )
    )
    assert failure_of(result) == EnumSessionSourceFailure.REPLAY_ROOT_MISSING


def test_a_record_with_no_timestamp_fails_no_source_timestamp(tmp_path: Path) -> None:
    write_bundle(tmp_path)
    manifest_path = tmp_path / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text("utf-8"))
    pin = manifest["cases"][0]["transcript_chain"]
    chain_path = tmp_path / pin["bundle_path"]
    chain = chain_path.read_bytes().replace(
        b'"emitted_at":"2025-02-15T00:20:00', b'"emitted_at":"', 1
    )
    chain_path.write_bytes(chain)
    pin["bytes"], pin["sha256"] = len(chain), hashlib.sha256(chain).hexdigest()
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert (
        failure_of(read(tmp_path, pin["sha256"]))
        == EnumSessionSourceFailure.NO_SOURCE_TIMESTAMP
    )


def test_a_revision_that_is_not_the_pin_is_refused(tmp_path: Path) -> None:
    write_bundle(tmp_path)
    assert failure_of(read(tmp_path, "f" * 64)) == EnumSessionSourceFailure.PIN_MISMATCH


def test_a_tampered_chain_is_refused(tmp_path: Path) -> None:
    digest = write_bundle(tmp_path)
    chain_path = tmp_path / "case-a" / "transcript_chain.jsonl"
    chain_path.write_bytes(chain_path.read_bytes().replace(b"ok", b"no"))
    assert failure_of(read(tmp_path, digest)) == EnumSessionSourceFailure.PIN_MISMATCH


def test_an_unknown_case_is_refused(tmp_path: Path) -> None:
    digest = write_bundle(tmp_path)
    result = read(tmp_path, digest, scopes=("case:case-z",))
    assert failure_of(result) == EnumSessionSourceFailure.CASE_NOT_FOUND


def test_pinned_repo_state_is_unavailable_where_the_case_excludes_it(
    tmp_path: Path,
) -> None:
    result = read(tmp_path, write_bundle(tmp_path), factor=EnumContextFactor.REPO_STATE)
    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.entries == ()
    assert result.error_code is None


def test_live_repo_state_is_unavailable_and_never_read_from_a_cwd() -> None:
    result = read(
        None,
        "watermark-1",
        factor=EnumContextFactor.REPO_STATE,
        scopes=("session:s-1",),
    )
    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.entries == ()


def test_live_operator_words_are_refused_until_the_live_reader_exists() -> None:
    result = read(None, "watermark-1", scopes=("session:s-1",))
    assert failure_of(result) == EnumSessionSourceFailure.LIVE_MODE_NOT_BUILT


@pytest.mark.parametrize(
    "scopes",
    [(), ("case:",), ("case:a", "session:b"), ("repo:x",)],
    ids=["none", "empty id", "two modes", "other scope only"],
)
def test_a_request_without_exactly_one_mode_scope_is_refused(
    scopes: tuple[str, ...],
) -> None:
    assert (
        failure_of(read(None, "r", scopes=scopes))
        == EnumSessionSourceFailure.SCOPE_INVALID
    )


def test_a_factor_pinned_mode_does_not_serve_is_refused(tmp_path: Path) -> None:
    result = read(tmp_path, write_bundle(tmp_path), factor=EnumContextFactor.OPEN_WORK)
    assert failure_of(result) == EnumSessionSourceFailure.FACTOR_NOT_SERVED


def test_a_session_with_no_operator_words_is_unresolved_not_empty_resolved(
    tmp_path: Path,
) -> None:
    ruled = (
        b"2025-02-15T00:30:00Z | RULING | lane=synthetic-lane-a | "
        b"reviewed=op-early..p-tail | reviewed with nothing further to record\n"
    )
    result = read(tmp_path, write_bundle(tmp_path, ruled))
    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.entries == ()
    assert result.entry_counts["reviewed"] == 5
