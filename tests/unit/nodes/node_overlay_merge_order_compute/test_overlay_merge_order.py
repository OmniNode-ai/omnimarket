# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay merge order decision (OMN-22809): the order, its refusals, its overlay and its failing controls."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_overlay_verdict import EnumOverlayVerdict
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_overlay_check import ModelOverlayCheck
from omnimarket.nodes.node_overlay_merge_order_compute.models.model_overlay_merge_order import (
    EnumMergeOrderVerdict,
    ModelOverlayMergeOrderResult,
)
from omnimarket.nodes.node_overlay_merge_order_compute.overlay import (
    NODE_NAME,
    ModelOverlayMergeOrderOverlay,
    load_overlay,
)
from tests.unit.nodes.node_overlay_merge_order_compute.merge_order_support import (
    EXAMPLE_OVERLAY,
    MERGED_REF,
    check,
    command,
    handler,
    overlay,
    whole_home,
)

pytestmark = pytest.mark.unit


def test_a_touched_overlay_the_merged_model_accepts_is_order_ok() -> None:
    result = handler().handle(command(("node_alpha_effect",), whole_home()))
    assert result.status == "completed"
    assert result.verdict is EnumMergeOrderVerdict.ORDER_OK
    assert result.accepted == ("node_alpha_effect",)
    assert result.ahead == ()
    assert result.merged_ref == MERGED_REF


def test_a_touched_overlay_the_merged_model_refuses_is_ahead_and_names_fields_not_values() -> (
    None
):
    result = handler().handle(
        command(("node_alpha_effect", "node_gamma_effect"), whole_home())
    )
    assert result.verdict is EnumMergeOrderVerdict.AHEAD_OF_CODE
    assert result.accepted == ("node_alpha_effect",)
    assert [item.overlay_id for item in result.ahead] == ["node_gamma_effect"]
    assert result.ahead[0].verdict == "rejected"
    assert result.ahead[0].fields == ("clone_branch", "overlay_ids")


def test_a_model_the_merged_tree_lacks_is_ahead_when_the_overlay_rules_it_so() -> None:
    result = handler().handle(command(("node_delta_effect",), whole_home()))
    assert result.verdict is EnumMergeOrderVerdict.AHEAD_OF_CODE
    assert result.ahead[0].verdict == "model_missing"


def test_an_overlay_the_change_does_not_touch_is_never_counted_against_it() -> None:
    """The whole home has a refused overlay and an absent model; a change to a sound one is still ORDER_OK."""
    result = handler().handle(command(("node_beta_compute",), whole_home()))
    assert result.verdict is EnumMergeOrderVerdict.ORDER_OK


def test_failing_control_a_blind_check_of_the_whole_home_blames_the_change() -> None:
    """The control: judging the whole home, not the touched overlays, reads the sound change ahead.

    It shows the test above can fail: the red is the break's (the touched set ignored), not the data's.
    """
    blind = handler().handle(
        command(
            tuple(item.overlay_id for item in whole_home()),
            whole_home(),
        )
    )
    assert blind.verdict is EnumMergeOrderVerdict.AHEAD_OF_CODE
    sound = handler().handle(command(("node_beta_compute",), whole_home()))
    assert sound.verdict is EnumMergeOrderVerdict.ORDER_OK


def test_the_overlay_decides_which_verdicts_are_ahead() -> None:
    """The set is the overlay's: with model_missing not listed, an absent model is refused as not ruled, not passed."""
    only_rejected = handler(ahead_verdicts=["rejected"])
    result = only_rejected.handle(command(("node_delta_effect",), whole_home()))
    assert result.status == "failed"
    assert result.failure == "verdict_not_ruled:node_delta_effect:model_missing"
    assert result.verdict is None


@pytest.mark.parametrize(
    ("changed", "checks", "failure"),
    [
        ((), whole_home(), "no_changed_overlays"),
        (
            ("node_zeta_effect",),
            whole_home(),
            "changed_overlay_not_judged:node_zeta_effect",
        ),
        (
            ("node_alpha_effect",),
            (*whole_home(), check("node_alpha_effect")),
            "duplicate_check:node_alpha_effect",
        ),
        (("Node-Alpha",), whole_home(), "bad_overlay_id:Node-Alpha"),
    ],
)
def test_a_check_that_cannot_be_judged_is_refused_never_passed(
    changed: tuple[str, ...],
    checks: tuple[ModelOverlayCheck, ...],
    failure: str,
) -> None:
    result = handler().handle(command(changed, checks))
    assert result.status == "failed"
    assert result.failure == failure
    assert result.verdict is None
    assert result.accepted == ()


def test_the_decision_is_deterministic_and_order_free() -> None:
    forward = handler().handle(
        command(("node_alpha_effect", "node_gamma_effect"), whole_home())
    )
    backward = handler().handle(
        command(
            ("node_gamma_effect", "node_alpha_effect"), tuple(reversed(whole_home()))
        )
    )
    assert forward == backward


def test_a_result_is_a_typed_model_that_refuses_extra_keys() -> None:
    with pytest.raises(ValidationError):
        ModelOverlayMergeOrderResult.model_validate(
            {"correlation_id": "c", "status": "completed", "surprise": 1}
        )


# --- the overlay: every setting, no default --------------------------------------------------------


def test_overlay_missing_is_refused_with_no_default(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError) as caught:
        load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert NODE_NAME in str(caught.value)


def test_overlay_with_no_roots_is_refused_with_no_default() -> None:
    with pytest.raises(NodeOverlayError):
        load_overlay({})


def test_overlay_shipped_example_loads_through_the_model(tmp_path: Path) -> None:
    root = tmp_path / NODE_NAME
    root.mkdir()
    (root / "overlay.yaml").write_text(
        EXAMPLE_OVERLAY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    loaded = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert EnumOverlayVerdict.ACCEPTED not in loaded.ahead_verdicts
    assert loaded.max_named >= 1


@pytest.mark.parametrize("missing", ["ahead_verdicts", "max_named", "injected_fault"])
def test_overlay_every_key_is_required(tmp_path: Path, missing: str) -> None:
    document = yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    del document[missing]
    root = tmp_path / NODE_NAME
    root.mkdir()
    (root / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(NodeOverlayError) as caught:
        load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert missing in str(caught.value)


def test_overlay_accepted_can_never_be_ahead() -> None:
    with pytest.raises(ValidationError):
        overlay(ahead_verdicts=["rejected", "accepted"])


def test_overlay_a_verdict_listed_twice_is_refused() -> None:
    with pytest.raises(ValidationError):
        overlay(ahead_verdicts=["rejected", "rejected"])


def test_overlay_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelOverlayMergeOrderOverlay.model_validate(
            {
                "ahead_verdicts": ["rejected"],
                "max_named": 1,
                "injected_fault": "x",
                "rung_order": 3,
            }
        )
