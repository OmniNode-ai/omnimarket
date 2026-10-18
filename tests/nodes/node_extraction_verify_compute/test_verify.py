# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The extraction verifier admits only verbatim operator words, widened to sentences (OMN-21139, TE.2)."""

from __future__ import annotations

import builtins
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.models.transcript_extraction import (
    EnumCapturedPromptOrigin,
    EnumExtractionRejectionReason,
    ModelCapturedPrompt,
    ModelExtractionVerifyRequest,
    ModelExtractionVerifyResult,
    ModelProposedExtractionEvent,
)
from omnimarket.nodes.node_extraction_verify_compute.handlers.handler_extraction_verify import (
    SPLITTER_VERSION,
    HandlerExtractionVerify,
    compute_candidate_digest,
    sentence_spans,
)

pytestmark = pytest.mark.unit

R = EnumExtractionRejectionReason
OPERATOR = EnumCapturedPromptOrigin.OPERATOR_TYPED
DEPLOY = "Ship the docs today. Do not deploy without approval. Then report."
SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/models/transcript_extraction/proposed_extraction_event.schema.json"
)


def prompt(
    text: str,
    prompt_id: str = "p-1",
    event_id: str = "e-1",
    origin: EnumCapturedPromptOrigin = OPERATOR,
) -> ModelCapturedPrompt:
    return ModelCapturedPrompt(
        event_id=event_id, prompt_id=prompt_id, text=text, origin=origin
    )


def proposal(
    quote: str,
    start: int,
    end: int,
    prompt_id: str = "p-1",
    event_id: str = "e-1",
    kind: str = "ruling",
    **extra: Any,
) -> str:
    body: dict[str, Any] = {
        "kind": kind,
        "quote": quote,
        "event_id": event_id,
        "prompt_id": prompt_id,
        "covers_start": start,
        "covers_end": end,
        **extra,
    }
    return json.dumps(body)


def at(text: str, quote: str) -> tuple[int, int]:
    start = text.index(quote)
    return start, start + len(quote)


def verify(
    prompts: tuple[ModelCapturedPrompt, ...], *proposals: str
) -> ModelExtractionVerifyResult:
    return HandlerExtractionVerify().handle(
        ModelExtractionVerifyRequest(
            segment_id="seg-1", prompts=prompts, proposals=proposals
        )
    )


def only_reason(result: ModelExtractionVerifyResult) -> EnumExtractionRejectionReason:
    assert result.admitted == ()
    assert len(result.rejections) == 1
    reason = result.rejections[0].reason
    assert result.rejection_counts[reason] == 1
    assert sum(result.rejection_counts.values()) == 1
    return reason


# Admission rule (criterion 1)


def test_an_exact_quote_at_its_offsets_is_admitted() -> None:
    sentence = "Do not deploy without approval."
    result = verify((prompt(DEPLOY),), proposal(sentence, *at(DEPLOY, sentence)))
    assert [a.quote for a in result.admitted] == [sentence]
    assert result.rejections == ()
    assert set(result.rejection_counts.values()) == {0}


def test_a_quote_absent_from_the_cited_prompt_is_rejected_as_absent() -> None:
    fabricated = "Deploy without approval."
    assert fabricated not in DEPLOY
    result = verify((prompt(DEPLOY),), proposal(fabricated, 0, len(fabricated)))
    assert only_reason(result) is R.ABSENT


def test_a_quote_present_at_other_offsets_is_rejected_as_wrong_offsets() -> None:
    sentence = "Then report."
    start, end = at(DEPLOY, sentence)
    result = verify((prompt(DEPLOY),), proposal(sentence, start - 5, end - 5))
    assert only_reason(result) is R.WRONG_OFFSETS


def test_offsets_past_the_end_of_the_prompt_are_wrong_offsets() -> None:
    result = verify((prompt(DEPLOY),), proposal("Then report.", 500, 512))
    assert only_reason(result) is R.WRONG_OFFSETS


@pytest.mark.parametrize(
    "origin",
    [
        EnumCapturedPromptOrigin.COMPACT_SUMMARY,
        EnumCapturedPromptOrigin.TOOL_RESULT,
        EnumCapturedPromptOrigin.HARNESS_INJECTED,
    ],
)
def test_a_quote_present_only_in_a_synthetic_record_is_rejected(
    origin: EnumCapturedPromptOrigin,
) -> None:
    sentence = "Do not deploy without approval."
    synthetic = prompt(DEPLOY, prompt_id="p-s", event_id="e-s", origin=origin)
    operator = prompt("Unrelated words.", prompt_id="p-1", event_id="e-1")
    result = verify(
        (operator, synthetic),
        proposal(sentence, *at(DEPLOY, sentence), prompt_id="p-s", event_id="e-s"),
    )
    assert only_reason(result) is R.SYNTHETIC_SOURCE


def test_a_quote_that_is_in_a_synthetic_record_but_cites_an_operator_prompt_is_absent() -> (
    None
):
    sentence = "Do not deploy without approval."
    synthetic = prompt(DEPLOY, "p-s", "e-s", EnumCapturedPromptOrigin.COMPACT_SUMMARY)
    result = verify(
        (prompt("Something else entirely."), synthetic),
        proposal(sentence, 0, len(sentence)),
    )
    assert only_reason(result) is R.ABSENT


def test_a_quote_spanning_a_whitespace_difference_is_admitted() -> None:
    text = "Ship it.  Do not\n\tdeploy   without approval.  Thanks."
    raw = "Do not\n\tdeploy   without approval."
    start, end = at(text, raw)
    result = verify(
        (prompt(text),), proposal("Do not deploy without approval.", start, end)
    )
    assert [a.quote for a in result.admitted] == [raw]
    assert result.admitted[0].proposed_start == start


def test_an_unknown_prompt_or_mismatched_event_is_unknown_source() -> None:
    sentence = "Then report."
    span = at(DEPLOY, sentence)
    for raw in (
        proposal(sentence, *span, prompt_id="p-404"),
        proposal(sentence, *span, event_id="e-404"),
    ):
        assert only_reason(verify((prompt(DEPLOY),), raw)) is R.UNKNOWN_SOURCE


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "Here are the rulings: []",
        json.dumps({"kind": "ruling", "quote": "x"}),
        proposal("Then report.", 10, 5),
        proposal("   ", 0, 3),
        proposal("Then report.", 0, 12, kind="opinion"),
        proposal("Then report.", 0, 12, confidence=0.9),
    ],
)
def test_a_proposal_that_does_not_parse_is_counted_as_parse_failure(raw: str) -> None:
    result = verify((prompt(DEPLOY),), raw)
    assert only_reason(result) is R.PARSE_FAILURE
    assert result.rejections[0].prompt_id is None


def test_every_reason_is_counted_and_rejections_keep_proposal_order() -> None:
    sentence = "Then report."
    span = at(DEPLOY, sentence)
    result = verify(
        (
            prompt(DEPLOY),
            prompt(DEPLOY, "p-s", "e-s", EnumCapturedPromptOrigin.TOOL_RESULT),
        ),
        "garbage",
        proposal("Never.", 0, 6),
        proposal(sentence, *span),
        proposal(sentence, span[0] + 1, span[1] + 1),
        proposal(sentence, *span, prompt_id="p-s", event_id="e-s"),
        proposal(sentence, *span, prompt_id="p-9"),
    )
    assert [r.proposal_index for r in result.rejections] == [0, 1, 3, 4, 5]
    assert result.rejection_counts == {
        R.PARSE_FAILURE: 1,
        R.ABSENT: 1,
        R.WRONG_OFFSETS: 1,
        R.SYNTHETIC_SOURCE: 1,
        R.UNKNOWN_SOURCE: 1,
    }
    assert len(result.admitted) == 1


# Sentence widening (criterion 2)


def test_deploy_from_do_not_deploy_without_approval_is_widened_to_the_sentence() -> (
    None
):
    text = "Do not deploy without approval."
    start, end = at(text, "deploy")
    (admitted,) = verify((prompt(text),), proposal("deploy", start, end)).admitted
    assert admitted.quote == text
    assert (admitted.proposed_start, admitted.proposed_end) == (start, end)
    assert (admitted.admitted_start, admitted.admitted_end) == (0, len(text))
    assert admitted.splitter_version == SPLITTER_VERSION


def test_a_fragment_inside_a_multi_sentence_prompt_widens_to_its_sentence_only() -> (
    None
):
    start, end = at(DEPLOY, "without approval")
    (admitted,) = verify(
        (prompt(DEPLOY),), proposal("without approval", start, end)
    ).admitted
    assert admitted.quote == "Do not deploy without approval."
    assert DEPLOY[admitted.admitted_start : admitted.admitted_end] == admitted.quote


def test_a_span_crossing_a_sentence_boundary_widens_to_both_sentences() -> None:
    quote = "today. Do not"
    start, end = at(DEPLOY, quote)
    (admitted,) = verify((prompt(DEPLOY),), proposal(quote, start, end)).admitted
    assert admitted.quote == "Ship the docs today. Do not deploy without approval."


def test_a_prompt_with_no_sentence_boundary_is_admitted_whole() -> None:
    text = "  dont merge anything on omnimarket until the queue clears  "
    start, end = at(text, "merge anything")
    (admitted,) = verify(
        (prompt(text),), proposal("merge anything", start, end)
    ).admitted
    assert admitted.quote == text.strip()


def test_every_admitted_span_starts_and_ends_on_sentence_boundaries() -> None:
    text = (
        "First, the queue.\nDon't touch the hot-fix branch! Is that clear? "
        'He said "stop now." Fine'
    )
    spans = sentence_spans(text)
    starts = {s for s, _ in spans}
    ends = {e for _, e in spans}
    words = ["queue", "hot-fix", "clear", "stop", "Fine"]
    raws = [proposal(w, *at(text, w)) for w in words]
    result = verify((prompt(text),), *raws)
    assert len(result.admitted) == len(words)
    for admitted in result.admitted:
        assert admitted.admitted_start in starts
        assert admitted.admitted_end in ends
        assert text[admitted.admitted_start : admitted.admitted_end] == admitted.quote
    assert [a.quote for a in result.admitted] == [
        "First, the queue.",
        "Don't touch the hot-fix branch!",
        "Is that clear?",
        'He said "stop now."',
        "Fine",
    ]


def test_a_curly_closing_quote_stays_with_its_sentence() -> None:
    text = "He wrote \u201chold the merge.\u201d Then go."
    (admitted,) = verify((prompt(text),), proposal("hold", *at(text, "hold"))).admitted
    assert admitted.quote == "He wrote \u201chold the merge.\u201d"


def test_the_splitter_partitions_every_non_whitespace_character() -> None:
    text = "One. Two!\n\nThree? four\nfive."
    spans = sentence_spans(text)
    covered = "".join(text[s:e] for s, e in spans)
    assert "".join(covered.split()) == "".join(text.split())
    assert all(text[s:e] == text[s:e].strip() for s, e in spans)


# Candidate digest (criteria 3 and 9)


def test_the_candidate_digest_reproduces_from_the_event() -> None:
    sentence = "Do not deploy without approval."
    (admitted,) = verify(
        (prompt(DEPLOY),), proposal(sentence, *at(DEPLOY, sentence))
    ).admitted
    canonical = json.dumps(
        {
            "covers_end": admitted.admitted_end,
            "covers_start": admitted.admitted_start,
            "event_id": admitted.source_ref,
            "kind": admitted.kind.value,
            "prompt_id": admitted.prompt_id,
            "quote": admitted.quote,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert admitted.candidate_digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert admitted.candidate_digest == compute_candidate_digest(admitted)
    assert admitted.quote_sha256 == hashlib.sha256(admitted.quote.encode()).hexdigest()


def test_events_equal_in_content_give_one_digest() -> None:
    """Different proposed spans and a scope widen to the same candidate: one digest."""
    first = verify((prompt(DEPLOY),), proposal("deploy", *at(DEPLOY, "deploy")))
    second = verify(
        (prompt(DEPLOY),),
        proposal(
            "without approval", *at(DEPLOY, "without approval"), scope="omnimarket"
        ),
    )
    assert first.admitted[0].candidate_digest == second.admitted[0].candidate_digest


def test_the_digest_changes_with_kind_and_source() -> None:
    sentence = "Then report."
    span = at(DEPLOY, sentence)
    prompts = (prompt(DEPLOY), prompt(DEPLOY, "p-2", "e-2"))
    result = verify(
        prompts,
        proposal(sentence, *span),
        proposal(sentence, *span, kind="constraint"),
        proposal(sentence, *span, prompt_id="p-2", event_id="e-2"),
    )
    assert len({a.candidate_digest for a in result.admitted}) == 3


# Artifact shape and no side effects (criteria 6 and 7)


def test_an_admitted_event_is_a_verified_transcript_extracts_artifact() -> None:
    sentence = "Do not deploy without approval."
    (admitted,) = verify(
        (prompt(DEPLOY, event_id="row-77"),),
        proposal(sentence, *at(DEPLOY, sentence), event_id="row-77", scope="deploys"),
    ).admitted
    assert admitted.factor is EnumContextFactor.TRANSCRIPT_EXTRACTS
    assert admitted.verbatim_verified is True
    assert admitted.label == "verbatim, proposed by model, verified"
    assert admitted.source_ref == "row-77"
    assert admitted.scope == "deploys"


def test_the_verifier_reads_no_file_and_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the verifier performed I/O")

    sentence = "Do not deploy without approval."
    request = ModelExtractionVerifyRequest(
        segment_id="seg-1",
        prompts=(prompt(DEPLOY),),
        proposals=(proposal(sentence, *at(DEPLOY, sentence)), "garbage"),
    )
    handler = HandlerExtractionVerify()
    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "write_text", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    result = handler.handle(request)
    assert len(result.admitted) == 1
    assert result.rejection_counts[R.PARSE_FAILURE] == 1


def test_the_handler_is_deterministic() -> None:
    raws = (proposal("deploy", *at(DEPLOY, "deploy")), "garbage")
    assert verify((prompt(DEPLOY),), *raws) == verify((prompt(DEPLOY),), *raws)


# The proposed-event JSON schema


def test_the_committed_json_schema_matches_the_model() -> None:
    committed = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert committed == ModelProposedExtractionEvent.model_json_schema()
