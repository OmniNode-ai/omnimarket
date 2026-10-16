# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the pure rescue-patch reviewer."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.rescue_patch_review import (
    HandlerRescuePatchReview,
)
from omnimarket.models.rescue_patch_review import (
    EnumRescuePatchVerdict,
    ModelRescuePatchReviewRequest,
    ModelRescuePatchReviewResult,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_rescue_patch_review_compute"
)
PATCH = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"


def test_golden_chain_review_then_superseded() -> None:
    handler = HandlerRescuePatchReview()
    request = ModelRescuePatchReviewRequest(patch_text=PATCH, applies_to_base=True)
    result = handler.handle(
        ModelRescuePatchReviewRequest.model_validate_json(request.model_dump_json())
    )
    assert result.verdict is EnumRescuePatchVerdict.NEEDS_READ
    assert result.files == ("src/app.py",)
    superseded = handler.handle(request.model_copy(update={"already_on_base": True}))
    assert superseded.verdict is EnumRescuePatchVerdict.DISCARD_SUPERSEDED
    assert superseded.verdict.is_discard
    assert (
        ModelRescuePatchReviewResult.model_validate_json(superseded.model_dump_json())
        == superseded
    )


def test_error_chain_request_without_git_fact_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRescuePatchReviewRequest.model_validate({"patch_text": PATCH})


def test_contract_declares_its_topics_and_resolvable_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.rescue-patch-review-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.rescue-patch-review-decided.v1"
    ]
    assert contract["runtime_dispatch"]["command_topic"] == bus["subscribe_topics"][0]
    assert contract["terminal_event"] == bus["publish_topics"][0]
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["handler"]["class"] == HandlerRescuePatchReview.__name__
    assert contract["handler"]["module"] == HandlerRescuePatchReview.__module__
    assert (
        getattr(
            import_module(contract["handler"]["module"]), contract["handler"]["class"]
        )
        is HandlerRescuePatchReview
    )
    assert contract["input_model"] == contract["handler"]["input_model"]
    route = contract["handler_routing"]["handlers"][0]
    assert route["operation"] == "review_rescue_patch"
    assert route["input_model"] == contract["input_model"]
    assert route["output_model"] == contract["output_model"]
