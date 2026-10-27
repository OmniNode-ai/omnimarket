# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's effect carries the policy decision through every dispatch surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import ModelLabFillRequest
from omnimarket.nodes.node_lane_model_policy_compute.handlers import (
    handler_lane_model_policy as policy,
)
from omnimarket.nodes.node_lane_model_policy_compute.models.model_lane_model_policy import (
    ModelLaneModelPolicy,
)


@pytest.mark.parametrize("ceiling", ["sonnet", "codex"])
def test_tick_effect_obeys_policy_before_dispatch_and_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    declared = ModelLaneModelPolicy.model_validate(
        {**policy.load_policy().model_dump(), "ceiling": ceiling}
    )
    monkeypatch.setattr(policy, "load_policy", lambda: declared)
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": "policy-probe",
                        "kind": "ticket",
                        "repo": "omnimarket",
                        "ticket": "OMN-21396",
                        "required_engine": "claude",
                    }
                ]
            }
        )
    )
    calls: list[list[str]] = []
    rows: list[tuple[str, str, str]] = []

    def send(argv: list[str]) -> tuple[bool, str]:
        calls.append(argv)
        return True, "DETACHED"

    def record(action: str, subject: str, detail: str) -> None:
        rows.append((action, subject, detail))

    monkeypatch.setattr(fill, "_send", send)
    state = tmp_path / "state"
    result = fill.HandlerLabFill().handle(
        ModelLabFillRequest(
            now="2026-10-26T19:59:00Z",
            request={
                "lab_headroom": {
                    "hosts": [{"name": "host-a", "admission": {"claude_slots": 1}}]
                }
            },
            lines=["MISSING lab-headroom:host-a | free Claude slots 1"],
            launch=None,
            state_dir=state,
            backlog=str(backlog),
            runner=tmp_path / "unused-runner.py",
            lanes_dir=tmp_path / "receipts",
            record=record,
            out_of_scope={},
        )
    )
    note = next(why for level, loop, why, _ in result.found if loop == "lab-fill")
    if ceiling == "codex":
        assert calls == rows == []
        assert not (state / fill.SENT_FILE).exists()
        assert note == (
            "lab-fill | sent=0 (claude=0 codex=0) free=1+0 sweep_planned=0 "
            "idle_after=1: nothing to send"
        )
        return

    lane = "fill-ticket-policy-probe-20261026T1959Z"
    assert len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "sonnet"
    brief = Path(argv[argv.index("--brief") + 1]).read_text()
    assert "ROUTE: model=sonnet effort=high\n" in brief
    assert rows == [("lab-fill", lane, "ticket policy-probe, sonnet")]
    assert json.loads((state / fill.SENT_FILE).read_text())["policy-probe"] == {
        "lane": lane,
        "at": "2026-10-26T19:59:00Z",
        "kind": "ticket",
        "model": "sonnet",
    }
    assert note == (
        "lab-fill | sent=1 (claude=1 codex=0) free=1+0 sweep_planned=0 "
        f"idle_after=0: {lane} (ticket policy-probe, sonnet)"
    )
