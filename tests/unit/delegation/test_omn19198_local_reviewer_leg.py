# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19198: the reviewer leg binds with only what the machine has.

The shipped judge (``cloud-glm-judge``) is a metered provider behind a
credential reference. On a customer machine that holds no such credential the
reviewer leg used to resolve to that endpoint anyway: a customer with no key had
a reviewer that could never authenticate, and C14 row 4 ("the reviewer leg is
unmetered") was red on the published release.

Now the declared judge is used where its credential resolves (the lab, or a
customer who supplied that key), the customer's own local model is used where
it does not, and a machine with neither gets a typed ``JUDGE_NO_REVIEWER_BOUND``
verdict with no call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.delegation_judge_verdict import EnumDelegationJudgeVerdict
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge import (
    adapter_routing_resolved_judge as judge_mod,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.adapter_routing_resolved_judge import (
    JudgeReviewerUnboundError,
    RoutingResolvedJudgeInferenceAdapter,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)

pytestmark = pytest.mark.unit

_LOCAL_ENDPOINT = "http://127.0.0.1:18741/v1/chat/completions"  # url-authority-ok: test loopback customer model
_CUSTOMER_MODEL = "customer-local-model"


def _bind_customer_overlay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The customer's overlay: their model on the two shipped local rungs."""
    from omnimarket.routing import delegation_backend_resolution as resolution_mod

    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        "backends:\n"
        + "".join(
            f"  - backend_id: {backend_id}\n"
            f'    endpoint_url: "{_LOCAL_ENDPOINT}"\n'
            f'    model_name: "{_CUSTOMER_MODEL}"\n'
            for backend_id in ("local-coder", "local-heavy-reasoning")
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    # The package conftest pins both the module default and the resolver's
    # keyword default at an absent file; this test's customer file replaces both.
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", overlay)
    for fn_name in ("resolve_delegation_backend", "load_bifrost_backends"):
        kwdefaults = getattr(resolution_mod, fn_name).__kwdefaults__
        monkeypatch.setitem(kwdefaults, "overlay_path", overlay)


def test_a_customer_without_a_key_is_reviewed_by_their_own_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1 + AC2: private endpoint, no credential reference."""
    _bind_customer_overlay(monkeypatch, tmp_path)

    backend = RoutingResolvedJudgeInferenceAdapter()._resolve_backend()

    assert backend.backend_id == "local-heavy-reasoning"
    assert backend.endpoint_ref == _LOCAL_ENDPOINT
    assert backend.model_id == _CUSTOMER_MODEL
    assert backend.secret_ref is None


def test_the_declared_judge_is_kept_where_its_credential_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: a machine that resolves the judge's credential (the lab, through its
    configured store; or a customer who stored that provider's key) keeps it."""
    _bind_customer_overlay(monkeypatch, tmp_path)
    monkeypatch.setattr(
        judge_mod,
        "api_key_ref_available",
        lambda ref, **_: ref == "llm.gemini.api_key",
    )

    backend = RoutingResolvedJudgeInferenceAdapter()._resolve_backend()

    assert backend.backend_id == "cloud-glm-judge"
    assert backend.secret_ref == "llm.gemini.api_key"


def test_a_machine_with_neither_has_no_reviewer_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conftest overlay is absent, so no local rung has an endpoint."""
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    adapter = RoutingResolvedJudgeInferenceAdapter()

    with pytest.raises(JudgeReviewerUnboundError, match="no reviewer is bound"):
        adapter._resolve_backend()
    assert adapter.reviewer_unbound_reason() is not None


@pytest.mark.asyncio
async def test_an_unbound_reviewer_is_a_typed_verdict_with_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    posted: list[str] = []

    def _never(**kwargs: Any) -> Any:
        posted.append(str(kwargs.get("endpoint_url")))
        raise AssertionError("no reviewer is bound; nothing may be called")

    monkeypatch.setattr(judge_mod.transport, "post_chat_completion", _never)

    event = await HandlerJudgeAdequacy().score(
        correlation_id=uuid4(),
        task_type="code_generation",
        prompt="write add(a, b)",
        candidate_output="def add(a, b):\n    return a + b\n",
    )

    assert event.verdict == EnumDelegationJudgeVerdict.JUDGE_FAILED
    assert event.failure_kind == "JUDGE_NO_REVIEWER_BOUND"
    assert posted == []


@pytest.mark.asyncio
async def test_the_local_reviewer_is_called_without_an_authorization_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through the real adapter: the one call goes to the customer's
    model, unauthenticated, and its verdict is recorded."""
    _bind_customer_overlay(monkeypatch, tmp_path)
    calls: list[dict[str, Any]] = []

    class _Response:
        json_body = {
            "choices": [
                {
                    "message": {
                        "content": '{"adequacy_score": 0.9, "reasoning": "correct"}'
                    }
                }
            ]
        }

    def _post(**kwargs: Any) -> _Response:
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr(judge_mod.transport, "post_chat_completion", _post)

    event = await HandlerJudgeAdequacy().score(
        correlation_id=uuid4(),
        task_type="code_generation",
        prompt="write add(a, b)",
        candidate_output="def add(a, b):\n    return a + b\n",
    )

    assert len(calls) == 1
    assert calls[0]["endpoint_url"] == _LOCAL_ENDPOINT
    assert "Authorization" not in calls[0]["extra_headers"]
    assert calls[0]["payload"]["model"] == _CUSTOMER_MODEL
    assert event.verdict == EnumDelegationJudgeVerdict.PASS
    assert event.judge_model == _CUSTOMER_MODEL
