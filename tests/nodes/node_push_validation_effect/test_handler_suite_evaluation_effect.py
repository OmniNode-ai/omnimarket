# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16524 ACCEPTANCE SUITE — HandlerSuiteEvaluationEffect (def-B, rung R1).

These tests drive the R1 acceptance bar through seeded
``ProtocolSuiteEvaluationClient`` stubs — no live git/docker:

* A green suite produces verdict=pass with no failure_detail.
* A red suite produces verdict=fail with a non-empty failure_detail — and is
  a SUCCESSFUL node execution (the handler returns a receipt, never raises).
* ``evaluated_tree_digest`` is exactly what the CLIENT computed (never the
  caller's `commit_sha` claim) — proves the handler treats it as
  content-addressed, independently-derived data.
* A malformed/absent `tenant_principal_id` refuses BEFORE any client call
  (raises, routed to the failure terminal topic).
* `suite_scope` is echoed onto the receipt from the REQUEST, not invented by
  the handler.
* An empty `suite_log_digest` from the client is refused (RuntimeError) —
  a run suite always has a complete log digest.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from omnimarket.nodes.node_push_validation_effect.handlers.handler_suite_evaluation_effect import (
    HandlerSuiteEvaluationEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_suite_evaluation_receipt import (
    EnumSuiteEvaluationVerdict,
)
from omnimarket.nodes.node_push_validation_effect.models.model_suite_evaluation_request import (
    ModelSuiteEvaluationRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ModelSuiteRun,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_suite_evaluation_client import (
    ModelSuiteEvaluationResult,
)

COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
TREE_DIGEST = "fedcba9876543210fedcba9876543210fedcba98"
POLICY_DIGEST = hashlib.sha256(b"tests/unit\x00").hexdigest()
PRINCIPAL = "t-000000000000400080000000000000aa"
CORRELATION = "00000000-0000-4000-8000-000000000003"

GREEN_LOG = "312 passed in 41.02s\n"
RED_LOG = "FAILED tests/unit/test_seam.py::test_field_match - AssertionError\n1 failed, 311 passed\n"


def make_request(**overrides: Any) -> ModelSuiteEvaluationRequest:
    kwargs: dict[str, Any] = {
        "repo": "OmniNode-ai/omnibase_compat",
        "commit_sha": COMMIT_SHA,
        "requester": "session:omn16524-r1-build",
        "correlation_id": CORRELATION,
        "emitted_at": "2026-08-25T00:00:00Z",
        "tenant_principal_id": PRINCIPAL,
    }
    kwargs.update(overrides)
    return ModelSuiteEvaluationRequest(**kwargs)


def result_from_seeded_log(
    log_text: str,
    *,
    evaluated_tree_digest: str = TREE_DIGEST,
    selector_policy_digest: str = POLICY_DIGEST,
) -> ModelSuiteEvaluationResult:
    """Derive the suite result FROM the seeded log — red because the log is
    red, not because a mock was told to say so."""
    passed = "FAILED" not in log_text
    return ModelSuiteEvaluationResult(
        evaluated_tree_digest=evaluated_tree_digest,
        selector_policy_digest=selector_policy_digest,
        suite=ModelSuiteRun(
            passed=passed,
            log_digest=hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
            detail="" if passed else log_text.strip()[-500:],
        ),
    )


class StubSuiteEvaluationClient:
    """Seeded ProtocolSuiteEvaluationClient recording every call in order."""

    def __init__(
        self,
        *,
        result: ModelSuiteEvaluationResult | None = None,
        host: str = "gate-runner-201",
    ) -> None:
        self.calls: list[str] = []
        self._result = result or result_from_seeded_log(GREEN_LOG)
        self._host = host

    def evaluate_commit(
        self, repo: str, commit_sha: str, suite_scope: str
    ) -> ModelSuiteEvaluationResult:
        self.calls.append(f"evaluate_commit:{repo}:{commit_sha}:{suite_scope}")
        return self._result

    def read_host_identity(self) -> str:
        self.calls.append("read_host_identity")
        return self._host


@pytest.mark.asyncio
async def test_green_suite_produces_pass_verdict_no_failure_detail() -> None:
    client = StubSuiteEvaluationClient(result=result_from_seeded_log(GREEN_LOG))
    handler = HandlerSuiteEvaluationEffect(client=client)

    receipt = await handler.handle(make_request())

    assert receipt.verdict is EnumSuiteEvaluationVerdict.PASS
    assert receipt.failure_detail is None
    assert receipt.suite_log_digest == hashlib.sha256(GREEN_LOG.encode()).hexdigest()
    assert client.calls == [
        "read_host_identity",
        f"evaluate_commit:OmniNode-ai/omnibase_compat:{COMMIT_SHA}:tests/unit",
    ]


@pytest.mark.asyncio
async def test_red_suite_produces_fail_verdict_is_successful_execution() -> None:
    client = StubSuiteEvaluationClient(result=result_from_seeded_log(RED_LOG))
    handler = HandlerSuiteEvaluationEffect(client=client)

    # A red suite is a SUCCESSFUL node execution: the handler returns a
    # receipt, it does not raise.
    receipt = await handler.handle(make_request())

    assert receipt.verdict is EnumSuiteEvaluationVerdict.FAIL
    assert receipt.failure_detail
    assert "FAILED" in receipt.failure_detail


@pytest.mark.asyncio
async def test_evaluated_tree_digest_is_the_clients_value_not_the_commit_claim() -> (
    None
):
    """R1's content-addressing AC: evaluated_tree_digest is what the executor
    independently computed, never a caller-trusted label — this test proves
    the handler passes it through unmodified from the client's own return,
    never deriving or overwriting it from request.commit_sha."""
    distinct_tree = "1" * 39 + "e"
    client = StubSuiteEvaluationClient(
        result=result_from_seeded_log(GREEN_LOG, evaluated_tree_digest=distinct_tree)
    )
    handler = HandlerSuiteEvaluationEffect(client=client)

    receipt = await handler.handle(make_request())

    assert receipt.evaluated_tree_digest == distinct_tree
    assert receipt.evaluated_tree_digest != receipt.commit_sha


@pytest.mark.asyncio
async def test_suite_scope_echoed_from_request_not_invented() -> None:
    client = StubSuiteEvaluationClient(result=result_from_seeded_log(GREEN_LOG))
    handler = HandlerSuiteEvaluationEffect(client=client)

    receipt = await handler.handle(make_request(suite_scope="tests/unit/nodes"))

    assert receipt.suite_scope == "tests/unit/nodes"
    assert "tests/unit/nodes" in client.calls[1]


@pytest.mark.asyncio
async def test_malformed_tenant_principal_refuses_before_any_client_call() -> None:
    client = StubSuiteEvaluationClient()
    handler = HandlerSuiteEvaluationEffect(client=client)
    request = make_request().model_construct(
        **{**make_request().model_dump(), "tenant_principal_id": "not-a-principal"}
    )

    with pytest.raises(ValueError, match="tenant_principal_id"):
        await handler.handle(request)

    assert client.calls == []


@pytest.mark.asyncio
async def test_empty_suite_log_digest_is_refused() -> None:
    bad_result = ModelSuiteEvaluationResult(
        evaluated_tree_digest=TREE_DIGEST,
        selector_policy_digest=POLICY_DIGEST,
        suite=ModelSuiteRun(passed=True, log_digest="0" * 64, detail=""),
    ).model_copy(
        update={
            "suite": ModelSuiteRun.model_construct(
                passed=True, log_digest="", detail=""
            )
        }
    )
    client = StubSuiteEvaluationClient(result=bad_result)
    handler = HandlerSuiteEvaluationEffect(client=client)

    with pytest.raises(RuntimeError, match="empty log digest"):
        await handler.handle(make_request())


@pytest.mark.asyncio
async def test_correlation_and_tenant_binding_echoed() -> None:
    client = StubSuiteEvaluationClient(result=result_from_seeded_log(GREEN_LOG))
    handler = HandlerSuiteEvaluationEffect(client=client)

    receipt = await handler.handle(make_request())

    assert receipt.correlation_id == CORRELATION
    assert receipt.tenant_principal_id == PRINCIPAL
    assert receipt.host_identity == "gate-runner-201"
    assert receipt.repo == "OmniNode-ai/omnibase_compat"
    assert receipt.commit_sha == COMMIT_SHA
