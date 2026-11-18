# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: node_overlay_drift_effect's per-overlay answer is this node's input (OMN-22809, piece 4).

The drift effect's runtime validator answers every pinned overlay through the models it has installed; this node
decides a change from those answers. The tests fail when either side changes shape: the answer's fields, the set of
verdicts, or an overlay verdict this node's shipped overlay does not rule on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_overlay_verdict import EnumOverlayVerdict
from omnimarket.models.model_overlay_check import ModelOverlayCheck
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_validator import (
    InProcessRuntimeValidator,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayPin,
)
from omnimarket.nodes.node_overlay_merge_order_compute.models.model_overlay_merge_order import (
    EnumMergeOrderVerdict,
    ModelOverlayMergeOrderCommand,
    ModelOverlayMergeOrderResult,
)
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    EXAMPLE_OVERLAY,
    MERGED_REF,
    handler,
    overlay,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
DRIFT = "node_overlay_drift_effect"
WRITER = "node_reading_writer_effect"
MODELS = {
    DRIFT: "omnimarket.nodes.node_overlay_drift_effect.overlay:ModelOverlayDriftOverlay",
    WRITER: "omnimarket.nodes.node_reading_writer_effect.overlay:ModelReadingOverlay",
}


def drift_answers(*, planted: bool) -> tuple[ModelOverlayCheck, ...]:
    """The drift effect's real validator over two real overlays; planted is the rung_order case."""
    entries = []
    for overlay_id, model in MODELS.items():
        text = (NODES / overlay_id / "overlay.example.yaml").read_text(encoding="utf-8")
        if planted and overlay_id == WRITER:
            text += "rung_order:\n- claude\n- opus\n"
        entries.append(
            (
                ModelOverlayPin(
                    overlay_id=overlay_id, revision="0123456789abcdef", model=model
                ),
                text,
            )
        )
    return InProcessRuntimeValidator().validate(tuple(entries)).results


def decide(changed: tuple[str, ...], *, planted: bool) -> ModelOverlayMergeOrderResult:
    return handler().handle(
        ModelOverlayMergeOrderCommand(
            correlation_id="seam-1",
            merged_ref=MERGED_REF,
            changed_overlays=changed,
            merged_checks=drift_answers(planted=planted),
        )
    )


def test_seam_the_drift_answer_has_the_shape_this_node_reads() -> None:
    assert set(ModelOverlayCheck.model_fields) == {
        "overlay_id",
        "verdict",
        "fields",
        "error_class",
    }
    assert {member.value for member in EnumOverlayVerdict} == {
        "accepted",
        "rejected",
        "model_missing",
        "unreadable",
        "overlay_missing",
    }


def test_seam_the_shipped_overlay_rules_on_every_verdict_the_drift_effect_can_give() -> (
    None
):
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    ruled = {EnumOverlayVerdict(name) for name in document["ahead_verdicts"]}
    assert ruled | {EnumOverlayVerdict.ACCEPTED} == set(EnumOverlayVerdict)


def test_seam_the_rung_order_case_the_drift_effect_rejects_is_ahead_of_code() -> None:
    result = decide((WRITER,), planted=True)
    assert result.verdict is EnumMergeOrderVerdict.AHEAD_OF_CODE
    assert result.ahead[0].overlay_id == WRITER
    assert result.ahead[0].verdict == "rejected"
    assert result.ahead[0].fields == ("rung_order",)


def test_seam_without_the_planted_key_the_same_change_is_order_ok() -> None:
    """The control: the red above is the planted key's."""
    result = decide((WRITER,), planted=False)
    assert result.verdict is EnumMergeOrderVerdict.ORDER_OK


def test_seam_an_overlay_the_change_does_not_touch_is_not_blamed_for_the_planted_key() -> (
    None
):
    result = decide((DRIFT,), planted=True)
    assert result.verdict is EnumMergeOrderVerdict.ORDER_OK


def test_seam_the_answers_survive_the_bus_as_json() -> None:
    answers = drift_answers(planted=True)
    wire = ModelOverlayMergeOrderCommand(
        correlation_id="seam-2",
        merged_ref=MERGED_REF,
        changed_overlays=(WRITER,),
        merged_checks=answers,
    ).model_dump_json()
    again = ModelOverlayMergeOrderCommand.model_validate_json(wire)
    assert again.merged_checks == answers
    assert overlay().max_named >= 1
