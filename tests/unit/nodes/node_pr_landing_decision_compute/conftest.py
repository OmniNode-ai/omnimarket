# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden capture of every landing decision, for the lease-reducer lift (OMN-24484).

The lift moves the lease arithmetic out of the decision handler and into
``node_lease_receipt_reducer``. The parity gate is that every decision the
existing tests of this directory make is byte-equal before and after. Byte
equality is read as the sha-256 of the decision's canonical JSON (sorted keys,
no whitespace), kept per test and per call in
``tests/fixtures/pr_landing_decision/lease_lift_golden.json``.

The gate runs by default: ``compare`` unless ``LANDING_LEASE_PARITY`` says
otherwise, so every CI split of this directory compares against the capture.

* ``LANDING_LEASE_PARITY=capture`` writes the file at the end of the session
  (run once on ``origin/dev``, before the lift).
* ``LANDING_LEASE_PARITY=off`` disables the gate (a developer's local choice;
  no CI configuration sets it).
* anything else, including unset: ``compare``. A test id the capture does not
  know fails too when its file is captured (a test added after the lift has
  no pre-lift baseline to compare against; capture it on the pre-lift base).
  A whole new file -- one the capture never saw -- is not a parity subject.

The gate's rules live in ``lease_parity.py`` next to this conftest, so they
can be unit-tested directly (``test_lease_parity_gate.py``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_node,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from tests.unit.nodes.node_pr_landing_decision_compute.lease_parity import (
    GOLDEN_PATH,
    PARITY_ENV,
    entry,
    parity_mode,
    parity_verdict,
)

_captured: dict[str, list[dict[str, Any]]] = {}


@pytest.fixture(autouse=True)
def lease_lift_parity(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    mode = parity_mode()
    if mode == "off":
        yield
        return
    real = decision_node._decision
    calls: list[dict[str, Any]] = []

    def wrapped(*args: Any, **kwargs: Any) -> ModelLandingDecision:
        decision = real(*args, **kwargs)
        calls.append(entry(decision))
        return decision

    monkeypatch.setattr(decision_node, "_decision", wrapped)
    yield
    nodeid = request.node.nodeid
    if mode == "capture":
        _captured[nodeid] = calls
        return
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    verdict = parity_verdict(nodeid, calls, golden)
    if verdict is not None:
        pytest.fail(verdict)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.environ.get(PARITY_ENV) != "capture" or not _captured:
        return
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(_captured, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
