# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_local_alert_transition_compute, driven only by what its contract declares.

The compute is called in process, so the chain is the contract itself: its
declared handler, input model and output model are resolved by name and one
request runs through the handler. Nothing is imported directly, so this fails
when the contract and the code drift apart.

Failure modes:

* the contract names a handler module, class or model that does not exist;
* the handler returns something other than the declared output model;
* a stored alert going away is not cleared, or a new one is not raised.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_local_alert_transition_compute"
    / "contract.yaml"
)
TENANT = "11111111-1111-4111-8111-111111111111"


def _resolve(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)


def _row(service: str, verdict: str, code: str | None, at: str) -> dict[str, object]:
    return {
        "tenant_id": TENANT,
        "mode": "compose",
        "service": service,
        "verdict": verdict,
        "verdict_reason": f"{service} {verdict}",
        "last_seen": at,
        "alert_code": code,
        "alert_severity": None if code is None else "critical",
        "alert_since": None if code is None else at,
        "report_id": None,
    }


def test_the_declared_chain_raises_and_clears() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    handler_cls = getattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["class"],
    )
    request_cls = _resolve(contract["handler"]["input_model"])
    result_cls = _resolve(contract["handler"]["output_model"])

    t0, t1 = "2026-10-31T09:00:00+00:00", "2026-10-31T09:01:00+00:00"
    before = (
        _row("postgres", "down", "SERVICE_DOWN", t0),
        _row("valkey", "running", None, t0),
    )
    after = (
        _row("postgres", "running", None, t1),
        _row("valkey", "down", "SERVICE_DOWN", t1),
    )
    result = handler_cls().handle(request_cls(before=before, after=after))

    assert isinstance(result, result_cls)
    assert [(t.subject, t.transition.value) for t in result.transitions] == [
        ("compose/postgres", "cleared"),
        ("compose/valkey", "raised"),
    ]
