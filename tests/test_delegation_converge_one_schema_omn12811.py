"""OMN-12811: converge delegation producers on ONE event schema.

The canonical delegation terminal (``delegation-completed.v1`` /
``delegation-failed.v1``) is a single snake_case ``ModelDelegationResult`` schema
declared ``extra='forbid'`` — the legacy ``task-delegated.v1`` camelCase co-writer
was deleted (OMN-13629). The projection converter on this canonical terminal path
must therefore read the canonical snake_case schema ONLY; the dual-shape
camel/snake coalescing aliases are dead and are dropped.

These tests pin the converged contract: a snake_case payload maps correctly, and
a camelCase-only payload is NOT silently honored (proving the dual-shape alias
plumbing is gone).
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    _canonical_result_to_task_delegated_payload as async_convert,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    _canonical_result_to_task_delegated_payload as sync_convert,
)

_CAMEL_KEY_FRAGMENTS = (
    "Id",
    "Type",
    "Used",
    "Name",
    "Text",
    "Tokens",
    "Passed",
    "Reason",
    "Usd",
    "Ms",
    "Compliance",
    "Version",
    "To",
    "By",
    "Score",
    "Bar",
    "Source",
)


def _has_camelcase_key(keys: object) -> bool:
    return any(
        any(frag in str(k) for frag in _CAMEL_KEY_FRAGMENTS)
        and any(c.isupper() for c in str(k))
        for k in keys  # type: ignore[union-attr]
    )


def test_async_converter_maps_canonical_snake_case() -> None:
    """The canonical snake_case terminal maps onto the projection row shape."""
    payload = {
        "correlation_id": "cid-snake",
        "task_type": "code",
        "model_used": "glm-4.6",
        "quality_passed": True,
        "content": "the model answer",
        "prompt_text": "the question",
        "latency_ms": 1234,
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "tokens_to_compliance": 3,
        "compliance_attempts": 2,
        "cost_usd": 0.05,
        "cost_savings_usd": 0.02,
        "pricing_manifest_version": 7,
        "escalation_count": 1,
        "actual_score": 0.9,
        "required_bar": 0.8,
    }
    out = async_convert(payload)
    assert out["correlation_id"] == "cid-snake"
    assert out["task_type"] == "code"
    assert out["delegated_to"] == "glm-4.6"
    assert out["model_name"] == "glm-4.6"
    assert out["quality_gate_passed"] is True
    assert out["response_text"] == "the model answer"
    assert out["prompt_text"] == "the question"
    assert out["delegation_latency_ms"] == 1234
    assert out["tokens_input"] == 11
    assert out["tokens_output"] == 22
    assert out["tokens_to_compliance"] == 3
    assert out["compliance_attempts"] == 2
    assert out["cost_usd"] == 0.05
    assert out["cost_savings_usd"] == 0.02
    assert out["pricing_manifest_version"] == 7
    assert out["escalation_count"] == 1
    assert out["actual_score"] == 0.9
    assert out["required_bar"] == 0.8
    assert not _has_camelcase_key(out.keys())


def test_async_converter_ignores_legacy_camelcase_dual_shape() -> None:
    """camelCase keys are NOT honored — the dual-shape coalescing is removed.

    Before OMN-12811 the converter coalesced ``data.get("x") or data.get("X")``,
    so a camelCase payload populated the row. After convergence on the single
    snake_case canonical schema, camelCase keys are inert: required fields fall
    through to their honest empty/unknown defaults.
    """
    camel_only = {
        "correlationId": "cid-camel",
        "taskType": "code",
        "modelUsed": "glm-4.6",
        "qualityPassed": True,
        "sessionId": "sess-camel",
        "promptText": "q",
        "responseText": "a",
        "promptTokens": 11,
        "completionTokens": 22,
        "costUsd": 0.05,
        "costSavingsUsd": 0.02,
        "latencyMs": 1234,
        "escalationCount": 3,
        "actualScore": 0.9,
    }
    out = async_convert(camel_only)
    assert out["correlation_id"] is None
    assert out["session_id"] is None
    assert out["task_type"] == "unknown"
    assert out["delegated_to"] == "unknown"
    assert out["model_name"] == ""
    assert out["quality_gate_passed"] is False
    assert out["response_text"] is None
    assert out["prompt_text"] is None
    assert out["tokens_input"] == 0
    assert out["tokens_output"] == 0
    assert out["cost_usd"] == 0
    assert out["cost_savings_usd"] == 0
    assert out["delegation_latency_ms"] is None
    assert out["escalation_count"] == 0
    assert out["actual_score"] is None


def test_sync_converter_is_snake_case_only() -> None:
    """The sync projection converter consumes only the canonical snake_case schema."""
    snake = sync_convert(
        {
            "correlation_id": "cid",
            "task_type": "code",
            "model_used": "glm-4.6",
            "quality_passed": True,
            "content": "answer",
            "prompt_text": "question",
            "latency_ms": 99,
            "prompt_tokens": 4,
            "completion_tokens": 8,
        }
    )
    assert snake["model_name"] == "glm-4.6"
    assert snake["response_text"] == "answer"
    assert snake["tokens_input"] == 4
    assert not _has_camelcase_key(snake.keys())

    camel = sync_convert(
        {
            "correlationId": "cid",
            "taskType": "code",
            "modelUsed": "glm-4.6",
            "qualityPassed": True,
        }
    )
    assert camel["task_type"] == "unknown"
    assert camel["model_name"] == ""
    assert camel["correlation_id"] is None
