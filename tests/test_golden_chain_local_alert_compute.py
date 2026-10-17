# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_local_alert_compute, driven only by what its contract declares.

The compute is called in process, so the chain is the contract itself: its
declared handler, input model and output model are resolved by name, the rules
are read from its config, and one request runs through the handler. Nothing is
imported directly, so this fails when the contract and the code drift apart.

Failure modes:

* the contract names a handler module, class or model that does not exist;
* the handler returns something other than the declared output model;
* the declared rules cannot be read into the rules model;
* the declared chain does not reach a verdict for every service, or opens an
  alert for a running one.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_local_alert_compute"
    / "contract.yaml"
)
_T0 = datetime(2026, 10, 17, 4, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return loaded


def _resolve(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


def test_the_declared_chain_runs_from_the_contract_alone() -> None:
    contract = _contract()
    handler_cls = _resolve(contract["handler"]["module"], contract["handler"]["class"])
    request_cls = _resolve(
        contract["input_model"]["module"], contract["input_model"]["name"]
    )
    result_cls = _resolve(
        contract["output_model"]["module"], contract["output_model"]["name"]
    )
    rules = load_local_alert_rules(_CONTRACT_PATH)

    def row(service: str, state: str, seen: datetime) -> dict[str, object]:
        return {
            "tenant_id": "t",
            "mode": "compose",
            "service": service,
            "observed_state": state,
            "reason": f"{service} {state}",
            "last_seen": seen,
            "last_change": seen,
        }

    request = request_cls.model_validate(
        {
            "services": [
                row("postgres", "running", _T0),
                row("redpanda", "down", _T0),
                row("valkey", "running", _T0 - timedelta(seconds=90)),
            ],
            "observed_at": _T0,
            "rules": rules,
        }
    )
    result = handler_cls().handle(request)

    assert isinstance(result, result_cls)
    assert [(v.service, v.verdict.value) for v in result.verdicts] == [
        ("postgres", "running"),
        ("redpanda", "down"),
        ("valkey", "stale"),
    ]
    assert sorted((a.service, a.code) for a in result.alerts) == [
        ("redpanda", "SERVICE_DOWN"),
        ("valkey", "HEALTH_UNKNOWN"),
    ]


def test_the_contract_declares_an_in_process_pure_compute() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    # No topic of its own: the projection calls it in process, which is why it
    # carries no onex.nodes entry point and is marked experimental.
    assert "event_bus" not in contract
    assert contract["lifecycle"] == "experimental"
