# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probes, leak audit and control contexts for the compaction measurement.

AC1 (``-k probe_leak``): a probe that shares five consecutive words with its
ruling's quote is refused ``PROBE_LEAK``; a clean probe is accepted.
AC2 (``-k inverted``): the inverted context carries each effective ruling's
predecessor text or a negation of its quote, holds none of the effective
rulings' required spans verbatim, and the judge fixture labels each
(predecessor, negation) entry ``CONTRADICTED``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_probe_compute.handlers.handler_compaction_probe import (
    HandlerCompactionProbes,
    HandlerProbeJudgeFixture,
)
from omnimarket.nodes.node_compaction_probe_compute.models.model_compaction_probe import (
    EnumInversionSource,
    EnumProbeAudit,
    EnumProbeVerdict,
    ModelCompactionProbeRequest,
    ModelEffectiveRuling,
    ModelProbeJudgeRequest,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "effective_rulings.json"


def _request(name: str) -> ModelCompactionProbeRequest:
    return ModelCompactionProbeRequest.model_validate(
        json.loads(FIXTURE.read_text())[name]
    )


def _judge(answer: str, ruling: ModelEffectiveRuling) -> EnumProbeVerdict:
    return (
        HandlerProbeJudgeFixture()
        .handle(ModelProbeJudgeRequest(answer=answer, ruling=ruling))
        .verdict
    )


# --- AC1: leak audit -------------------------------------------------------


def test_probe_leak_fixture_is_refused() -> None:
    result = HandlerCompactionProbes().handle(_request("leak"))
    (probe,) = result.probes
    assert probe.audit is EnumProbeAudit.PROBE_LEAK
    assert probe.leaked_words == "go with the friction plan"
    assert result.probes_refused_leak == 1
    assert result.probes_accepted == 0


def test_probe_leak_clean_fixture_passes() -> None:
    result = HandlerCompactionProbes().handle(_request("clean"))
    assert [p.audit for p in result.probes] == [EnumProbeAudit.ACCEPTED] * 2
    assert all(p.leaked_words is None for p in result.probes)
    assert result.probes_total == 2
    assert result.probes_refused_leak == 0


def test_probe_leak_ignores_case_and_punctuation() -> None:
    request = _request("clean")
    first = request.rulings[0].model_copy(
        update={"question": "GO, with the Friction-plan?"}
    )
    result = HandlerCompactionProbes().handle(
        request.model_copy(update={"rulings": (first,)})
    )
    assert result.probes[0].audit is EnumProbeAudit.PROBE_LEAK


def test_probe_leak_four_shared_words_is_not_a_leak() -> None:
    request = _request("clean")
    first = request.rulings[0].model_copy(
        update={"question": "Should we go with the friction?"}
    )
    result = HandlerCompactionProbes().handle(
        request.model_copy(update={"rulings": (first,)})
    )
    assert result.probes[0].audit is EnumProbeAudit.ACCEPTED


def test_probe_is_built_from_the_question_cell() -> None:
    request = _request("clean")
    result = HandlerCompactionProbes().handle(request)
    for ruling, probe in zip(request.rulings, result.probes, strict=True):
        assert probe.stamp == ruling.stamp
        assert probe.probe_text.endswith(ruling.question)
        assert ruling.quote not in probe.probe_text


# --- control contexts: oracle and empty -------------------------------------


def test_oracle_and_empty_contexts_are_deterministic() -> None:
    request = _request("clean")
    first = HandlerCompactionProbes().handle(request)
    reversed_request = request.model_copy(
        update={"rulings": tuple(reversed(request.rulings))}
    )
    second = HandlerCompactionProbes().handle(reversed_request)
    assert first.contexts == second.contexts
    assert first.contexts.empty.text == ""
    for ruling in request.rulings:
        assert ruling.quote in first.contexts.oracle.text
        for span in ruling.required_spans:
            assert span in first.contexts.oracle.text
    assert len(first.contexts.oracle.sha256) == 64


def test_oracle_answer_is_recalled_and_empty_answer_is_absent() -> None:
    for ruling in _request("clean").rulings:
        assert _judge(ruling.quote, ruling) is EnumProbeVerdict.RECALLED
        assert _judge("", ruling) is EnumProbeVerdict.ABSENT
        assert _judge("I do not know.", ruling) is EnumProbeVerdict.ABSENT


# --- AC2: inverted context --------------------------------------------------


def test_inverted_carries_predecessor_or_negation() -> None:
    request = _request("clean")
    result = HandlerCompactionProbes().handle(request)
    entries = {entry.stamp: entry for entry in result.contexts.inverted_entries}
    with_predecessor, without_predecessor = request.rulings
    assert entries[with_predecessor.stamp].source is EnumInversionSource.PREDECESSOR
    assert with_predecessor.predecessor_text is not None
    assert with_predecessor.predecessor_text in entries[with_predecessor.stamp].text
    assert entries[without_predecessor.stamp].source is EnumInversionSource.NEGATION
    for entry in result.contexts.inverted_entries:
        assert entry.text in result.contexts.inverted.text


def test_inverted_holds_no_required_span_verbatim() -> None:
    request = _request("clean")
    inverted = HandlerCompactionProbes().handle(request).contexts.inverted.text
    assert inverted
    for ruling in request.rulings:
        for span in ruling.required_spans:
            assert span.lower() not in inverted.lower()


def test_inverted_redacts_a_span_found_in_the_predecessor() -> None:
    request = _request("clean")
    first = request.rulings[0].model_copy(
        update={"predecessor_text": "Drop the friction plan and keep sending."}
    )
    inverted = (
        HandlerCompactionProbes()
        .handle(request.model_copy(update={"rulings": (first,)}))
        .contexts.inverted.text
    )
    assert "friction plan" not in inverted.lower()


def test_inverted_redaction_reaches_a_fixed_point() -> None:
    ruling = ModelEffectiveRuling(
        stamp="2026-11-01T17:00:00Z",
        question="Ship the abc release?",
        quote="Ship the abc release now.",
        required_spans=("b", "ac", "redacted"),
    )
    inverted = (
        HandlerCompactionProbes()
        .handle(ModelCompactionProbeRequest(correlation_id="x", rulings=(ruling,)))
        .contexts.inverted.text
    )
    for span in ruling.required_spans:
        assert span not in inverted.lower()


def test_inverted_fixture_pairs_are_contradicted_by_the_judge() -> None:
    request = _request("clean")
    result = HandlerCompactionProbes().handle(request)
    entries = {entry.stamp: entry for entry in result.contexts.inverted_entries}
    sources = set()
    for ruling in request.rulings:
        entry = entries[ruling.stamp]
        sources.add(entry.source)
        assert _judge(entry.text, ruling) is EnumProbeVerdict.CONTRADICTED
    assert sources == {EnumInversionSource.PREDECESSOR, EnumInversionSource.NEGATION}


def test_inverted_negation_of_a_negative_quote_is_contradicted() -> None:
    ruling = ModelEffectiveRuling(
        stamp="2026-11-01T18:00:00Z",
        question="May the lane merge the pull request itself?",
        quote="Do not merge it yourself; hand it to the landing controller.",
        required_spans=("2026-11-01T18:00:00Z",),
    )
    result = HandlerCompactionProbes().handle(
        ModelCompactionProbeRequest(correlation_id="neg", rulings=(ruling,))
    )
    (entry,) = result.contexts.inverted_entries
    assert entry.source is EnumInversionSource.NEGATION
    assert "not" not in entry.text.lower().split()
    assert _judge(entry.text, ruling) is EnumProbeVerdict.CONTRADICTED
    assert _judge(ruling.quote, ruling) is EnumProbeVerdict.RECALLED


# --- input validation -------------------------------------------------------


def test_a_ruling_without_a_question_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelEffectiveRuling(
            stamp="2026-11-01T14:06:53Z",
            question=" ",
            quote="Go.",
            required_spans=("Go",),
        )


def test_duplicate_stamps_are_refused() -> None:
    ruling = _request("clean").rulings[0]
    with pytest.raises(ValidationError):
        ModelCompactionProbeRequest(correlation_id="dup", rulings=(ruling, ruling))
