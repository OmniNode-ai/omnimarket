# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each routing entry's handler resolves the runtime's typed arm with its event model."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
    ModelPrLandingGithubFailed,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_alarm import (
    HandlerMergeOverrideAlarm,
)
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_entries import (
    HandlerMergeOverrideLandingCompleted,
    HandlerMergeOverrideLandingFailed,
    HandlerMergeOverridePrMerged,
    HandlerMergeOverrideRepoMerged,
    HandlerMergeOverrideSweep,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideOutcome,
    ModelMergeOverrideResult,
    ModelMergeOverrideSweepTrigger,
)
from omnimarket.validators.routing_input_model_fit import _signature_model_name
from tests.unit.nodes.node_merge_override_alarm_effect.support import (
    READ_SUFFIX,
    VERDICT_SUFFIX,
    Publisher,
    make_rig,
    pr_merged_event,
    read_answer,
)

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_merge_override_alarm_effect"
)


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_every_routed_handler_resolves_its_event_model() -> None:
    for entry in _contract()["handler_routing"]["handlers"]:
        resolved = _signature_model_name(
            entry["handler"]["name"], entry["handler"]["module"]
        )
        assert resolved == entry["event_model"]["name"], (
            f"{entry['handler']['name']} resolved to {resolved!r}, "
            f"expected {entry['event_model']['name']!r}"
        )


def test_the_top_level_union_handler_still_resolves_no_model() -> None:
    assert (
        _signature_model_name(
            "HandlerMergeOverrideAlarm",
            "omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_alarm",
        )
        is None
    )


@pytest.mark.parametrize(
    ("entry_cls", "model_cls"),
    [
        (HandlerMergeOverridePrMerged, ModelPrMergedEvent),
        (HandlerMergeOverrideRepoMerged, ModelRepoMerged),
        (HandlerMergeOverrideLandingCompleted, ModelPrLandingGithubCompleted),
        (HandlerMergeOverrideLandingFailed, ModelPrLandingGithubFailed),
        (HandlerMergeOverrideSweep, ModelMergeOverrideSweepTrigger),
    ],
)
def test_each_entry_hands_its_event_to_the_alarm_logic(
    entry_cls: Callable[[], Any], model_cls: type[BaseModel]
) -> None:
    entry: Any = entry_cls()
    sentinel = ModelMergeOverrideResult(outcome=EnumMergeOverrideOutcome.IGNORED)
    recorded: list[Any] = []

    class _Stub:
        def handle(self, request: Any) -> ModelMergeOverrideResult:
            recorded.append(request)
            return sentinel

    entry._core = cast(HandlerMergeOverrideAlarm, _Stub())
    request = model_cls.model_construct()
    result = entry.handle(request)
    assert result is sentinel
    assert recorded == [request]


def test_golden_chain_through_the_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    entry_pr = HandlerMergeOverridePrMerged(
        event_publisher=rig.publisher,
        database=rig.database,
        reader=rig.reader,
        clock=rig.clock,
    )
    entry_landing = HandlerMergeOverrideLandingCompleted(
        event_publisher=rig.publisher,
        database=rig.database,
        reader=rig.reader,
        clock=rig.clock,
    )
    requested = entry_pr.handle(pr_merged_event())
    assert requested.outcome.value == "read_requested"
    assert len(rig.publisher.on(READ_SUFFIX)) == 1
    result = entry_landing.handle(read_answer(rig))

    rig2 = make_rig(monkeypatch, tmp_path)
    requested2 = rig2.handler.handle(pr_merged_event())
    assert requested2.outcome.value == "read_requested"
    result2 = rig2.handler.handle(read_answer(rig2))
    assert result.outcome == result2.outcome
    assert result.reason == result2.reason
    assert len(rig.publisher.on(VERDICT_SUFFIX)) == 1


def test_error_chain_through_the_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    faulty = Publisher(error="broker down")
    rig = make_rig(monkeypatch, tmp_path, publisher=faulty)
    entry_pr = HandlerMergeOverridePrMerged(
        event_publisher=rig.publisher,
        database=rig.database,
        reader=rig.reader,
        clock=rig.clock,
    )
    result = entry_pr.handle(pr_merged_event())
    assert result.outcome.value != "read_requested"

    rig2 = make_rig(monkeypatch, tmp_path, publisher=Publisher(error="broker down"))
    result2 = rig2.handler.handle(pr_merged_event())
    assert result.outcome.value == result2.outcome.value
