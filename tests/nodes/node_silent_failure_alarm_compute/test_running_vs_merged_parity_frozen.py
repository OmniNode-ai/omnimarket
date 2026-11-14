# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running-vs-merged decision here answers as the frozen copy answered (OMN-19998).

``fixtures/running_vs_merged_frozen_parity.json`` holds each case's fleet fold, acknowledgements and
branch-install readback, and the frozen handler's own answer recorded from a run of it on a lab host
(2026-11-14; the one private repository name in a recorded text is replaced by the words this copy prints). Every case is rebuilt through this copy's ``request_from_fleet`` and
``with_branch_installs`` and decided by this copy's handler. The answers must be equal, except where
``NAMED_DIFFERENCES`` says a text differs on purpose: each entry is one correction, with its reason.
A fixture whose frozen answer was edited by hand, or a handler that drifts from the frozen decision,
fails a case by name.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from omnimarket.models.model_running_vs_merged_alarm import ModelRunningVsMergedResult
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_running_vs_merged_alarm import (
    HandlerRunningVsMergedAlarm,
    read_acknowledgements,
    request_from_fleet,
    with_branch_installs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "running_vs_merged_frozen_parity.json"

# (pattern in the frozen text, replacement here, why). Nothing else may differ.
NAMED_DIFFERENCES = (
    (
        r" --lane \S+-by-hand",
        "",
        "the adopt tool's install-tree command has no --lane flag: the frozen line made the "
        "command refuse",
    ),
    (
        r"^(on \S+: )(python3 .* install-tree )",
        r"\1OMNI_HOME=$OMNI_HOME \2",
        "the install-tree fix runs with the host's own declared OMNI_HOME",
    ),
)


def _expected_text(frozen: str) -> str:
    text = frozen
    for pattern, replacement, _why in NAMED_DIFFERENCES:
        text = re.sub(pattern, replacement, text)
    return text


def _cases() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _decide(case: dict[str, object]) -> ModelRunningVsMergedResult:
    acknowledgements = read_acknowledgements(
        json.dumps({"acknowledgements": case.get("acknowledgements", [])})
    )
    now = datetime.fromisoformat(str(case["now"]).replace("Z", "+00:00"))
    request = request_from_fleet(
        case["fleet"],  # type: ignore[arg-type]
        now,
        acknowledgements=acknowledgements,
    )
    readback = case.get("branch_installs")
    if isinstance(readback, dict):
        request = with_branch_installs(request, readback["payload"], readback["why"])
    return HandlerRunningVsMergedAlarm().handle(request)


def _normalise(frozen: dict[str, object]) -> dict[str, object]:
    out = json.loads(json.dumps(frozen))
    for finding in out["findings"]:
        finding["fix"] = _expected_text(finding["fix"])
    return out


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c["name"]))
def test_the_decision_equals_the_frozen_answer(case: dict[str, object]) -> None:
    got = json.loads(_decide(case).model_dump_json())
    assert got == _normalise(case["frozen"])  # type: ignore[arg-type]


def test_every_named_difference_is_used_by_a_recorded_case() -> None:
    texts = [f["fix"] for c in _cases() for f in c["frozen"]["findings"]]  # type: ignore[index]
    for pattern, _replacement, why in NAMED_DIFFERENCES:
        assert any(re.search(pattern, text) for text in texts), (
            f"no recorded case exercises the named difference: {why}"
        )


def test_the_table_covers_every_state_and_every_channel() -> None:
    frozen = [c["frozen"] for c in _cases()]
    assert {f["state"] for f in frozen} == {"OK", "MISSING", "UNKNOWN"}
    assert any(f["held"] for f in frozen)
    assert any(f["acknowledged"] for f in frozen)
    assert len(frozen) >= 30


def test_failing_control_a_changed_answer_is_caught() -> None:
    case = next(c for c in _cases() if c["name"] == "serve-checkout-drift")
    forged = json.loads(json.dumps(case["frozen"]))
    forged["state"] = "OK"
    forged["findings"] = []
    got = json.loads(_decide(case).model_dump_json())
    assert got != _normalise(forged)
