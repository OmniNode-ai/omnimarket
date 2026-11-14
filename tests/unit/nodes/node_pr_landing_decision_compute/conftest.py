# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden capture of every landing decision, for the lease-reducer lift (OMN-24484).

The lift moves the lease arithmetic out of the decision handler and into
``node_lease_receipt_reducer``. The parity gate is that every decision the
existing tests of this directory make is byte-equal before and after. Byte
equality is read as the sha-256 of the decision's canonical JSON (sorted keys,
no whitespace), kept per test and per call in
``tests/fixtures/pr_landing_decision/lease_lift_golden.json``.

* ``LANDING_LEASE_PARITY=capture`` writes the file at the end of the session
  (run once on ``origin/dev``, before the lift).
* ``LANDING_LEASE_PARITY=compare`` fails each test whose decisions differ from
  the capture.
* unset: this file does nothing, so an ordinary run is untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_node,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)

PARITY_ENV: Final = "LANDING_LEASE_PARITY"
GOLDEN_PATH: Final = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "pr_landing_decision"
    / "lease_lift_golden.json"
)

_captured: dict[str, list[dict[str, Any]]] = {}


def canonical(decision: ModelLandingDecision) -> str:
    return json.dumps(
        decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _entry(decision: ModelLandingDecision) -> dict[str, Any]:
    dispatched = [
        (
            action.cause_brief.brief_class.value
            if action.cause_brief is not None
            else action.brief.brief_class.value
            if action.brief is not None
            else ""
        )
        for action in decision.actions
        if action.kind.value == "dispatch_worker"
    ]
    return {
        "sha256": hashlib.sha256(canonical(decision).encode()).hexdigest(),
        "dispatched": dispatched,
        "recorded": len(decision.recorded_outcomes),
        "discarded": sum(
            1 for action in decision.actions if action.kind.value == "discard_result"
        ),
    }


@pytest.fixture(autouse=True)
def lease_lift_parity(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    mode = os.environ.get(PARITY_ENV, "")
    if not mode:
        yield
        return
    real = decision_node._decision
    calls: list[dict[str, Any]] = []

    def wrapped(*args: Any, **kwargs: Any) -> ModelLandingDecision:
        decision = real(*args, **kwargs)
        calls.append(_entry(decision))
        return decision

    monkeypatch.setattr(decision_node, "_decision", wrapped)
    yield
    nodeid = request.node.nodeid
    if mode == "capture":
        _captured[nodeid] = calls
        return
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if nodeid not in golden:
        return
    want = [entry["sha256"] for entry in golden[nodeid]]
    got = [entry["sha256"] for entry in calls]
    assert got == want, f"{nodeid}: decisions differ from the pre-lift capture"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.environ.get(PARITY_ENV) != "capture" or not _captured:
        return
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(_captured, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
