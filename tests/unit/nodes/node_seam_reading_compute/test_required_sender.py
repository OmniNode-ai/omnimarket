# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A required automatic seam with no bound sender reads NOT WORKING (base-first piece 3, OMN-22909).

The plan's CI part for piece 3: every automatic seam declares a sender. A seam nobody sends on
never crosses, so the reading names it instead of waiting for a crossing that cannot come.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_required_sender import (
    HandlerRequiredSender,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_required_sender import (
    EnumSeamTrigger,
    ModelDemandedVerdict,
    ModelRequiredSenderRequest,
    ModelSeamDeclaration,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)


def _read(
    *declarations: ModelSeamDeclaration, demanded: tuple[ModelDemandedVerdict, ...] = ()
):
    return HandlerRequiredSender().handle(
        ModelRequiredSenderRequest(
            declarations=declarations, demanded=demanded, now=NOW
        )
    )


def _automatic(seam: str, sender: str | None, required: bool = True):
    return ModelSeamDeclaration(
        seam=seam, trigger=EnumSeamTrigger.AUTOMATIC, required=required, sender=sender
    )


def test_a_required_automatic_seam_with_no_bound_sender_reads_not_working() -> None:
    result = _read(_automatic("review-dispatch", None))

    assert result.verdicts[0].verdict is EnumReadingVerdict.NOT_WORKING
    assert result.verdicts[0].reason == "no-bound-sender"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.counts.required_without_sender == 1


def test_the_same_seam_with_a_sender_reads_working() -> None:
    result = _read(_automatic("review-dispatch", "node_review_sender_effect"))

    assert result.verdicts[0].verdict is EnumReadingVerdict.WORKING
    assert result.verdicts[0].reason == "sender-bound"
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.counts.required_without_sender == 0


def test_a_blank_sender_is_not_a_bound_sender() -> None:
    with pytest.raises(ValidationError):
        _automatic("review-dispatch", "  ")


def test_an_optional_automatic_seam_with_no_sender_is_not_a_failure() -> None:
    result = _read(_automatic("review-dispatch", None, required=False))

    assert result.verdicts[0].verdict is EnumReadingVerdict.WORKING
    assert result.verdicts[0].reason == "optional-unbound"
    assert result.counts.required_without_sender == 0


def test_an_on_demand_seam_reads_the_verdict_its_demand_returned() -> None:
    declaration = ModelSeamDeclaration(
        seam="pr-triage", trigger=EnumSeamTrigger.ON_DEMAND, required=True
    )

    working = _read(
        declaration,
        demanded=(
            ModelDemandedVerdict(seam="pr-triage", verdict=EnumReadingVerdict.WORKING),
        ),
    )
    failing = _read(
        declaration,
        demanded=(
            ModelDemandedVerdict(
                seam="pr-triage", verdict=EnumReadingVerdict.NOT_WORKING
            ),
        ),
    )
    never_demanded = _read(declaration)

    assert working.verdicts[0].verdict is EnumReadingVerdict.WORKING
    assert failing.verdicts[0].verdict is EnumReadingVerdict.NOT_WORKING
    assert never_demanded.verdicts[0].verdict is EnumReadingVerdict.UNKNOWN
    assert never_demanded.verdicts[0].reason == "not-demanded"


def test_the_counts_name_each_kind_of_seam() -> None:
    result = _read(
        _automatic("a", "sender-a"),
        _automatic("b", None),
        _automatic("c", None),
        ModelSeamDeclaration(
            seam="d", trigger=EnumSeamTrigger.ON_DEMAND, required=True
        ),
        demanded=(ModelDemandedVerdict(seam="d", verdict=EnumReadingVerdict.WORKING),),
    )

    assert result.counts.automatic == 3
    assert result.counts.on_demand == 1
    assert result.counts.required_without_sender == 2
    assert result.counts.working == 2
    assert result.counts.not_working == 2
    assert result.counts.unknown == 0


def test_a_seam_declared_twice_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRequiredSenderRequest(
            declarations=(_automatic("a", "x"), _automatic("a", None)), now=NOW
        )


def test_a_demand_for_an_undeclared_seam_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRequiredSenderRequest(
            declarations=(_automatic("a", "x"),),
            demanded=(
                ModelDemandedVerdict(seam="zzz", verdict=EnumReadingVerdict.WORKING),
            ),
            now=NOW,
        )


def _command(*declarations: ModelSeamDeclaration) -> bytes:
    return (
        ModelRequiredSenderRequest(declarations=declarations, now=NOW)
        .model_dump_json()
        .encode()
    )


def test_golden_chain_every_seam_with_a_sender_ends_in_the_completed_event() -> None:
    topics = load_topics()

    out = answer_command(
        _command(_automatic("a", "sender-a")), HandlerRequiredSender(), topics
    )

    assert [t for t, _ in out] == [topics["required_sender_read_completed"]]
    body = json.loads(out[0][1])
    assert body["verdict"] == "WORKING"
    assert body["counts"]["required_without_sender"] == 0


def test_error_chain_a_required_seam_with_no_sender_ends_in_the_failed_event() -> None:
    topics = load_topics()

    out = answer_command(
        _command(_automatic("a", "sender-a"), _automatic("b", None)),
        HandlerRequiredSender(),
        topics,
    )

    assert [t for t, _ in out] == [topics["required_sender_read_failed"]]
    body = json.loads(out[0][1])
    assert body["verdict"] == "NOT_WORKING"
    assert body["failure"] == "b:no-bound-sender"
    assert body["counts"]["required_without_sender"] == 1


def test_error_chain_a_bad_payload_is_a_typed_failure_never_dropped() -> None:
    topics = load_topics()

    out = answer_command(b"{}", HandlerRequiredSender(), topics)

    assert out[0][0] == topics["required_sender_read_failed"]
    assert json.loads(out[0][1])["failure"].startswith("bad-payload:")
