# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real fill effect preserves the declared model across dispatch surfaces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import (
    ModelIdleSlotFillRequest,
    ModelLabFillRequest,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers import (
    handler_lane_model_policy as policy,
)
from omnimarket.nodes.node_lane_model_policy_compute.models.model_lane_model_policy import (
    ModelLaneModelPolicy,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("claude_slots", "marks", "expected"),
    [
        (0, {}, "codex"),
        (
            2,
            {"limited_mark": {"state": "present", "until": "2026-10-29T03:00:00Z"}},
            "codex",
        ),
        (2, {}, "codex"),
    ],
)
def test_fill_routes_experiment_to_declared_available_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claude_slots: int,
    marks: dict[str, object],
    expected: str,
) -> None:
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": "experiment",
                        "kind": "experiment-design",
                        "ticket": "OMN-21409",
                    },
                    {
                        "key": "forbidden",
                        "kind": "experiment-design",
                        "ticket": "OMN-21409",
                        "required_engine": "opus",
                    },
                ]
            }
        )
    )
    calls: list[list[str]] = []

    def send(argv: list[str]) -> tuple[bool, str]:
        calls.append(argv)
        return True, "DETACHED"

    monkeypatch.setattr(fill, "_send", send)
    fill.HandlerLabFill().handle(
        ModelLabFillRequest(
            now="2026-10-27T03:27:00Z",
            request={
                "lab_headroom": {
                    "hosts": [
                        {
                            "name": "host-a",
                            **marks,
                            "admission": {
                                "claude_slots": claude_slots,
                                "codex_slots": 2,
                            },
                        }
                    ]
                }
            },
            lines=["MISSING lab-headroom:host-a | free slots 2"],
            launch=None,
            state_dir=tmp_path / "state",
            backlog=str(backlog),
            runner=tmp_path / "unused.py",
            lanes_dir=tmp_path / "receipts",
            out_of_scope={},
        )
    )
    assert len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == expected
    brief = Path(argv[argv.index("--brief") + 1]).read_text()
    assert f"ROUTE: model={expected} effort=high\n" in brief
    assert "model=opus" not in brief
    assert (
        json.loads((tmp_path / "state" / fill.SENT_FILE).read_text())["experiment"][
            "model"
        ]
        == expected
    )


@pytest.mark.unit
def test_shared_capacity_choice_comes_from_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = policy.load_policy()
    assert declared.ceiling == "sonnet"
    assert declared.order.index("codex") < declared.order.index("sonnet")
    reordered = [model for model in declared.order if model != "sonnet"]
    reordered.insert(0, "sonnet")
    monkeypatch.setattr(
        policy,
        "load_policy",
        lambda: ModelLaneModelPolicy.model_validate(
            {**declared.model_dump(), "order": reordered}
        ),
    )
    result = fill.HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now="2026-10-27T03:27:00Z",
            hosts=[
                {"name": "host-a", "admission": {"claude_slots": 1, "codex_slots": 1}}
            ],
            lines=["MISSING lab-headroom:host-a | free slots 1"],
            backlog=[
                {
                    "key": "experiment",
                    "kind": "experiment-design",
                    "ticket": "OMN-21409",
                }
            ],
        )
    )
    assert result.sends[0]["model"] == "sonnet"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mark", "allowed"),
    [
        ({"state": "present", "until": "2026-10-29T03:00:00Z"}, False),
        ({"state": "present", "until": "2026-10-26T03:00:00Z"}, True),
        ({"state": "unreadable"}, False),
        ({"state": "present", "until": "invalid"}, False),
    ],
)
def test_claude_pin_waits_for_usable_account_capacity(
    mark: dict[str, str], allowed: bool
) -> None:
    result = fill.HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now="2026-10-27T03:27:00Z",
            hosts=[
                {
                    "name": "host-a",
                    "limited_mark": mark,
                    "admission": {"claude_slots": 1, "codex_slots": 1},
                }
            ],
            lines=["MISSING lab-headroom:host-a | free slots 1"],
            backlog=[
                {
                    "key": "pinned",
                    "kind": "experiment-design",
                    "ticket": "OMN-21409",
                    "required_engine": "claude",
                }
            ],
        )
    )
    assert bool(result.sends) is allowed
    if allowed:
        assert result.sends[0]["model"] == "sonnet"
    else:
        assert result.skipped[0]["why"] == "claude-required: waiting for Claude quota"


@pytest.mark.unit
@pytest.mark.parametrize("reserved", [0, 1, 2])
def test_shared_codex_never_exceeds_the_host_budget(reserved: int) -> None:
    result = fill.HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now="2026-10-27T03:27:00Z",
            hosts=[
                {"name": "host-a", "admission": {"claude_slots": 2, "codex_slots": 2}}
            ],
            lines=["MISSING lab-headroom:host-a | free slots 2"],
            queue_reserved={"codex": reserved},
            backlog=[
                {
                    "key": f"experiment-{n}",
                    "kind": "experiment-design",
                    "ticket": "OMN-21409",
                }
                for n in range(3)
            ],
        )
    )
    assert [send["model"] for send in result.sends] == ["codex"] * (2 - reserved)
    assert result.idle_after == 0
