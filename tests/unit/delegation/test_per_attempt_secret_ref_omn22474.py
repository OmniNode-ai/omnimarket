# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22474 AC1: each customer-key attempt names the key it used.

OMN-19996 PR 1 made ``secret_ref`` a forthcoming attempt key, decoded and
dropped. With omniclaude's omnimarket pin past PR 1, the attempt record declares
it: a reference, never a value, present on a customer-key attempt only and
absent from the dump otherwise, so a run with no customer key serialises as
before.

Failure modes, each with a test below:
A1 a customer-key attempt's ref is dropped from its record.
A2 an attempt with no ref grows a ``secret_ref`` key (null) on the wire.
A3 the delegate-skill handler drops the ref when it builds the ladder, on
   either the port-owned ``attempts`` path or the escalation-history path.
A4 a house-key ref (not the minted tenant-key shape) reaches the record.
A5 the local port's rung entry for a call it made does not name the key.
"""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _local_rung_attempt,
)

pytestmark = pytest.mark.unit

X = "cred_tenant-a_openrouter_" + "a" * 32
Y = "cred_tenant-a_openrouter_" + "b" * 32
HOUSE = "OPENROUTER_API_KEY"

_BASE = {
    "tier": "tenant_byok",
    "backend_id": "byok-openrouter",
    "model_id": "m-1",
    "quality_gate_passed": True,
}


def test_per_attempt_secret_ref_is_declared_and_dumped() -> None:
    record = ModelDelegateSkillAttemptRecord(**_BASE, secret_ref=X)
    assert record.secret_ref == X
    assert record.model_dump(mode="json")["secret_ref"] == X


def test_per_attempt_secret_ref_absent_leaves_no_key() -> None:
    record = ModelDelegateSkillAttemptRecord(**_BASE)
    assert "secret_ref" not in record.model_dump(mode="json")
    assert "secret_ref" not in record.model_dump_json()


def test_per_attempt_secret_ref_from_port_attempts() -> None:
    records = _attempt_records(
        {
            "correlation_id": str(uuid4()),
            "attempts": [
                _BASE | {"secret_ref": X},
                _BASE | {"secret_ref": HOUSE},
                _BASE | {"secret_ref": Y},
                dict(_BASE),
            ],
        }
    )
    assert [record.secret_ref for record in records] == [X, None, Y, None]


def test_per_attempt_secret_ref_from_escalation_history() -> None:
    def row(ref: str | None) -> dict[str, object]:
        base: dict[str, object] = {
            "tier_name": "tenant_byok",
            "model_used": "m-1",
            "backend_ref": "byok-openrouter",
            "acceptance_decision": "climb",
        }
        return base | ({"secret_ref": ref} if ref is not None else {})

    records = _attempt_records(
        {
            "correlation_id": str(uuid4()),
            "escalation_history": [row(X), row(HOUSE), row(None), row(Y)],
        }
    )
    assert [record.secret_ref for record in records] == [X, None, None, Y]


def test_per_attempt_secret_ref_on_the_local_rung_entry() -> None:
    backend = SimpleNamespace(
        backend_id="byok-openrouter",
        model_id="m-1",
        substituted_from_backend_id=None,
        endpoint_ref="https://openrouter.invalid/api/v1",
        tier=None,
        routing_tier=None,
    )
    result = SimpleNamespace(
        actual_cost_usd=0.0,
        usage_source=SimpleNamespace(value="measured"),
        latency_ms=5,
        http_status=500,
        provider_code=None,
        secret_ref=X,
    )
    entry = _local_rung_attempt(
        cast("Any", backend),
        cast("Any", result),
        None,
        "provider failed",
        configuration=None,
        size_band=None,
    )
    assert entry["secret_ref"] == X
