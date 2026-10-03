# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolver failures must complete the real redeploy chain as audited refusals."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.events.runtime_deployment import (
    GRANT_FILE_PATH,
    EnumGrantResolution,
    EnumOccGateState,
    EnumProdGateOutcome,
    EnumPromotionClass,
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelGrantProvenance,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelProdPromotionGrantResolveCommand,
    ModelProdPromotionGrantResolvedEvent,
    ModelReadinessProjectionFact,
    ModelRedeployCompletedEvent,
    render_grant_refusal,
)
from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
)
from omnimarket.nodes.node_prod_promotion_grant_resolver_effect.handlers import (
    handler_prod_promotion_grant_resolver as resolver_module,
)
from omnimarket.nodes.node_prod_promotion_grant_resolver_effect.handlers.handler_prod_promotion_grant_resolver import (
    GitHubMainGrantFetcher,
    HandlerProdPromotionGrantResolver,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_GRANT_RESOLVE,
    TOPIC_PROD_GATE_EVALUATE,
    TOPIC_REDEPLOY_COMPLETED,
    HandlerRedeployOrchestrator,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_redeploy_orchestrator/contract.yaml"
)
_SUBSCRIBE = contract_subscribe_topics(_CONTRACT)
_TOPIC_START = next(t for t in _SUBSCRIBE if t.endswith("redeploy-start.v1"))
_TOPIC_RESOLVED = next(
    t for t in _SUBSCRIBE if t.endswith("prod-promotion-grant-resolved.v1")
)
_TOPIC_EVALUATED = next(
    t for t in _SUBSCRIBE if t.endswith("prod-promotion-gate-evaluated.v1")
)
_SOURCE_SHA = "a" * 40
_DIGEST = "sha256:" + "b" * 64
_BATCH = "refusal-batch"
_EVALUATED_AT = datetime(2026, 10, 3, tzinfo=UTC)


def _start(*, ready: bool = True) -> ModelRedeployStartCommand:
    return ModelRedeployStartCommand(
        correlation_id=uuid4(),
        runtime_lane=EnumRuntimeLane.PROD,
        image_digest=_DIGEST,
        promotion_batch_id=_BATCH,
        readiness_projection=(
            ModelReadinessProjectionFact(
                readiness_state="READY",
                image_digest=_DIGEST,
                promotion_batch_id=_BATCH,
            )
            if ready
            else None
        ),
        occ_gate_state=EnumOccGateState.MERGED,
        rollback_target="sha256:" + "c" * 64,
    )


def _content(raw: bytes) -> bytes:
    return json.dumps({"content": base64.b64encode(raw).decode()}).encode()


async def _drive(
    handler: HandlerProdPromotionGrantResolver,
    start: ModelRedeployStartCommand | None = None,
) -> tuple[
    ModelProdPromotionGrantResolvedEvent,
    ModelProdPromotionGateDecision,
    ModelRedeployCompletedEvent,
]:
    """Drive real start -> resolver -> gate -> BLOCKED completion handlers."""
    start = start if start is not None else _start()
    orchestrator = HandlerRedeployOrchestrator()
    started = await orchestrator.handle(
        ModelEventEnvelope(
            payload=start, correlation_id=start.correlation_id, event_type=_TOPIC_START
        )
    )
    assert [e.event_type for e in started.events] == [TOPIC_GRANT_RESOLVE]
    command = started.events[0].payload
    assert isinstance(command, ModelProdPromotionGrantResolveCommand)
    resolved = await handler.handle(command)
    assert resolved.grant is None
    assert resolved.correlation_id == start.correlation_id
    assert resolved.evaluated_at == command.evaluated_at
    gated = await orchestrator.handle(
        ModelEventEnvelope(
            payload={
                "resolved": resolved.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=start.correlation_id,
            event_type=_TOPIC_RESOLVED,
        )
    )
    assert [e.event_type for e in gated.events] == [TOPIC_PROD_GATE_EVALUATE]
    gate_command = gated.events[0].payload
    assert isinstance(gate_command, ModelProdPromotionGateCommand)
    assert gate_command.grant_refusal == resolved.resolution
    assert gate_command.promotion_grant is None
    decision = await HandlerProdPromotionGate().handle(gate_command)
    assert decision.allowed is False
    routed = await orchestrator.handle(
        ModelEventEnvelope(
            payload={
                "decision": decision.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=start.correlation_id,
            event_type=_TOPIC_EVALUATED,
        )
    )
    assert [e.event_type for e in routed.events] == [TOPIC_REDEPLOY_COMPLETED]
    completed = routed.events[0].payload
    assert isinstance(completed, ModelRedeployCompletedEvent)
    assert completed.final_phase is EnumRedeployPhase.BLOCKED
    assert completed.correlation_id == start.correlation_id
    assert completed.error_message == decision.reason
    assert render_grant_refusal(resolved.provenance) in completed.error_message
    assert (
        "anchor=OmniNode-ai/omninode_infra:grants/prod_promotion_grants.yaml"
        in completed.error_message
    )
    assert "ref=main" in completed.error_message
    return resolved, decision, completed


@pytest.mark.unit
class TestGrantResolveRefusalChain:
    @pytest.mark.parametrize("file_only", [False, True])
    async def test_private_anchor_404(
        self, monkeypatch: pytest.MonkeyPatch, file_only: bool
    ) -> None:
        def request(url: str) -> bytes:
            if file_only and "/commits/" in url:
                return json.dumps({"sha": _SOURCE_SHA}).encode()
            raise HTTPError(url, 404, "Not Found", None, None)

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        resolved, decision, completed = await _drive(
            HandlerProdPromotionGrantResolver(fetcher)
        )
        assert resolved.resolution is EnumGrantResolution.UNREADABLE
        assert resolved.provenance.http_status == 404
        assert (
            "unreadable (missing file or token without access)"
            in resolved.provenance.refusal_reason
        )
        assert decision.outcome is EnumProdGateOutcome.GRANT_ANCHOR_UNREADABLE
        assert completed.error_message.startswith("grant_anchor_unreadable:")
        assert "http_status=404" in completed.error_message
        assert resolved.provenance.source_commit_sha == (
            _SOURCE_SHA if file_only else None
        )
        assert resolved.provenance.file_sha256 is None
        if file_only:
            assert f"source_commit={_SOURCE_SHA}" in completed.error_message

    @pytest.mark.parametrize(
        ("status", "reason"),
        [(401, "token rejected"), (403, "token forbidden"), (500, "HTTP 500")],
    )
    async def test_http_error(
        self, monkeypatch: pytest.MonkeyPatch, status: int, reason: str
    ) -> None:
        def request(url: str) -> bytes:
            raise HTTPError(url, status, "HTTP error", None, None)

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        resolved, decision, completed = await _drive(
            HandlerProdPromotionGrantResolver(fetcher)
        )
        assert resolved.resolution is EnumGrantResolution.UNREADABLE
        assert resolved.provenance.http_status == status
        assert decision.outcome is EnumProdGateOutcome.GRANT_ANCHOR_UNREADABLE
        assert reason in completed.error_message

    @pytest.mark.parametrize("raises", [True, False])
    async def test_token_not_provisioned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        raises: bool,
    ) -> None:
        async def missing_secret(ref: str) -> None:
            if raises:
                raise RuntimeError("FAKE_SECRET_MUST_NEVER_APPEAR")

        monkeypatch.setattr(resolver_module, "resolve_api_key_async", missing_secret)
        with caplog.at_level(logging.WARNING, logger=resolver_module.__name__):
            resolved, decision, completed = await _drive(
                HandlerProdPromotionGrantResolver()
            )
        assert resolved.resolution is EnumGrantResolution.UNREADABLE
        assert resolved.provenance.http_status is None
        assert decision.outcome is EnumProdGateOutcome.GRANT_ANCHOR_UNREADABLE
        assert "'GITHUB_TOKEN'" in completed.error_message
        assert (
            "did not resolve" if raises else "resolved to no value"
        ) in completed.error_message
        assert "http_status=none" in completed.error_message
        assert (
            "FAKE_SECRET_MUST_NEVER_APPEAR" not in completed.error_message + caplog.text
        )
        assert "correlation_id=" in caplog.text
        assert "resolution=unreadable" in caplog.text

    @pytest.mark.parametrize(
        "raw", [b"entries: [unterminated", b"- just\n- a list\n", b"\xff"]
    )
    async def test_unparseable_registry(
        self, monkeypatch: pytest.MonkeyPatch, raw: bytes
    ) -> None:
        def request(url: str) -> bytes:
            if "/commits/" in url:
                return json.dumps({"sha": _SOURCE_SHA}).encode()
            if GRANT_FILE_PATH in url:
                return _content(raw)
            return _content(f"{GRANT_FILE_PATH} @owners".encode())

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        resolved, decision, completed = await _drive(
            HandlerProdPromotionGrantResolver(fetcher)
        )
        assert resolved.resolution is EnumGrantResolution.UNPARSEABLE
        assert resolved.provenance.file_sha256 == hashlib.sha256(raw).hexdigest()
        assert resolved.provenance.source_commit_sha == _SOURCE_SHA
        assert resolved.provenance.codeowners_match is True
        assert resolved.provenance.http_status is None
        assert len(resolved.provenance.refusal_reason) <= 300
        assert decision.outcome is EnumProdGateOutcome.GRANT_ANCHOR_UNPARSEABLE
        assert completed.error_message.startswith("grant_anchor_unparseable:")
        assert "\n" not in completed.error_message

    @pytest.mark.parametrize("candidate", [False, True])
    async def test_refusal_outranks_other_facts(
        self, monkeypatch: pytest.MonkeyPatch, candidate: bool
    ) -> None:
        def request(url: str) -> bytes:
            raise HTTPError(url, 404, "Not Found", None, None)

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        start = _start(ready=False)
        orchestrator = HandlerRedeployOrchestrator()
        command = ModelProdPromotionGrantResolveCommand(
            correlation_id=start.correlation_id, evaluated_at=_EVALUATED_AT
        )
        resolved = await HandlerProdPromotionGrantResolver(fetcher).handle(command)
        gated = await orchestrator.handle(
            ModelEventEnvelope(
                payload={
                    "resolved": resolved.model_dump(mode="json"),
                    "start": start.model_dump(mode="json"),
                },
                correlation_id=start.correlation_id,
                event_type=_TOPIC_RESOLVED,
            )
        )
        gate_command = gated.events[0].payload
        assert isinstance(gate_command, ModelProdPromotionGateCommand)
        # Candidate facts arrive on the gate command, independently of the anchor.
        gate_command = gate_command.model_copy(
            update={
                "promotion_class": EnumPromotionClass.STABILITY_CANDIDATE
                if candidate
                else EnumPromotionClass.CLEAN_MAIN,
                "non_main_lineage": candidate,
            }
        )
        decision = await HandlerProdPromotionGate().handle(gate_command)
        assert decision.outcome is EnumProdGateOutcome.GRANT_ANCHOR_UNREADABLE
        routed = await orchestrator.handle(
            ModelEventEnvelope(
                payload={
                    "decision": decision.model_dump(mode="json"),
                    "start": start.model_dump(mode="json"),
                },
                correlation_id=start.correlation_id,
                event_type=_TOPIC_EVALUATED,
            )
        )
        assert [e.event_type for e in routed.events] == [TOPIC_REDEPLOY_COMPLETED]
        completed = routed.events[0].payload
        assert isinstance(completed, ModelRedeployCompletedEvent)
        assert completed.final_phase is EnumRedeployPhase.BLOCKED
        assert completed.error_message == decision.reason
        assert render_grant_refusal(resolved.provenance) in completed.error_message

    @pytest.mark.parametrize(
        ("error", "detail"),
        [
            (URLError("secret"), "transport error (URLError)"),
            (TimeoutError("secret"), "transport error (TimeoutError)"),
            (OSError("secret"), "transport error (OSError)"),
            (RuntimeError("secret"), "resolver error (RuntimeError)"),
        ],
    )
    async def test_transport_and_safety_net(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception, detail: str
    ) -> None:
        def request(url: str) -> bytes:
            raise error

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        resolved, decision, completed = await _drive(
            HandlerProdPromotionGrantResolver(fetcher)
        )
        assert resolved.resolution is EnumGrantResolution.UNREADABLE
        assert resolved.provenance.http_status is None
        assert decision.outcome is EnumProdGateOutcome.GRANT_ANCHOR_UNREADABLE
        assert detail in completed.error_message
        assert "secret" not in completed.error_message

    @pytest.mark.parametrize("body", [b"not json", b"{}", b'{"sha": null}'])
    async def test_malformed_api_response(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", lambda _url: body)
        resolved, _, completed = await _drive(
            HandlerProdPromotionGrantResolver(fetcher)
        )
        assert resolved.resolution is EnumGrantResolution.UNREADABLE
        assert resolved.provenance.http_status is None
        assert "unexpected GitHub API response" in completed.error_message

    async def test_codeowners_error_preserves_read_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = b"entries: []"

        def request(url: str) -> bytes:
            if "/commits/" in url:
                return json.dumps({"sha": _SOURCE_SHA}).encode()
            if GRANT_FILE_PATH in url:
                return _content(raw)
            raise HTTPError(url, 403, "Forbidden", None, None)

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        resolved, _, _ = await _drive(HandlerProdPromotionGrantResolver(fetcher))
        assert resolved.provenance.http_status == 403
        assert resolved.provenance.source_commit_sha == _SOURCE_SHA
        assert resolved.provenance.file_sha256 == hashlib.sha256(raw).hexdigest()
        assert resolved.provenance.codeowners_match is False

    @pytest.mark.parametrize(
        "body", [b"{}", b'{"content": null}', b'{"content": "!!!!"}']
    )
    async def test_malformed_grant_content(
        self, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        def request(url: str) -> bytes:
            if "/commits/" in url:
                return json.dumps({"sha": _SOURCE_SHA}).encode()
            return body

        fetcher = GitHubMainGrantFetcher(token="t")
        monkeypatch.setattr(fetcher, "_request", request)
        resolved, _, completed = await _drive(
            HandlerProdPromotionGrantResolver(fetcher)
        )
        assert resolved.resolution is EnumGrantResolution.UNREADABLE
        assert resolved.provenance.source_commit_sha == _SOURCE_SHA
        assert resolved.provenance.file_sha256 is None
        assert "unexpected GitHub API response" in completed.error_message


@pytest.mark.unit
class TestGrantRefusalValidation:
    def test_refusal_forbids_materialized_grant(self) -> None:
        grant = ModelProdPromotionGrant(
            grant_id="refused-grant",
            approved_lane=EnumRuntimeLane.PROD,
            approved_image_digest=_DIGEST,
            approved_promotion_batch_id=_BATCH,
            approved_by="owner",
            created_at=_EVALUATED_AT,
            expires_at=_EVALUATED_AT,
        )
        with pytest.raises(ValidationError, match="must have grant=None"):
            ModelProdPromotionGrantResolvedEvent(
                correlation_id=uuid4(),
                resolution=EnumGrantResolution.UNREADABLE,
                grant=grant,
                evaluated_at=_EVALUATED_AT,
                provenance=ModelGrantProvenance(
                    codeowners_match=False, refusal_reason="unreadable"
                ),
            )

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_refusal_requires_reason(self, reason: str | None) -> None:
        with pytest.raises(ValidationError, match="non-empty refusal_reason"):
            ModelProdPromotionGrantResolvedEvent(
                correlation_id=uuid4(),
                resolution=EnumGrantResolution.UNREADABLE,
                evaluated_at=_EVALUATED_AT,
                provenance=ModelGrantProvenance(
                    codeowners_match=False, refusal_reason=reason
                ),
            )

    @pytest.mark.parametrize("missing", ["source_commit_sha", "file_sha256"])
    def test_non_refusal_requires_read_provenance(self, missing: str) -> None:
        provenance = ModelGrantProvenance(
            codeowners_match=True,
            source_commit_sha=None if missing == "source_commit_sha" else _SOURCE_SHA,
            file_sha256=None if missing == "file_sha256" else "a" * 64,
        )
        with pytest.raises(
            ValidationError, match="requires source_commit_sha and file_sha256"
        ):
            ModelProdPromotionGrantResolvedEvent(
                correlation_id=uuid4(),
                resolution=EnumGrantResolution.RESOLVED,
                evaluated_at=_EVALUATED_AT,
                provenance=provenance,
            )

    def test_non_refusal_forbids_refusal_reason(self) -> None:
        with pytest.raises(ValidationError, match="refusal_reason=None"):
            ModelProdPromotionGrantResolvedEvent(
                correlation_id=uuid4(),
                resolution=EnumGrantResolution.ABSENT,
                evaluated_at=_EVALUATED_AT,
                provenance=ModelGrantProvenance(
                    source_commit_sha=_SOURCE_SHA,
                    file_sha256="a" * 64,
                    codeowners_match=True,
                    refusal_reason="cannot read",
                ),
            )
