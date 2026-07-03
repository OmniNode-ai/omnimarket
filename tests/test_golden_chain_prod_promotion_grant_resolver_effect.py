# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prod-promotion grant resolver EFFECT tests (OMN-13439 / Phase 2b).

Proves the orchestrator fact-gathering EFFECT that resolves the prod promotion
grant from the durable trust anchor:

  * present + future-expiry + matching -> grant materialized (RESOLVED);
  * absent / expired / digest-mismatch / batch-mismatch / lane-mismatch -> None;
  * ``approved_by == requested_by`` -> SELF_GRANTED (rejected, None);
  * consumed entry -> ABSENT (no replay), None;
  * provenance fields populated on the EMITTED audit evidence (source commit SHA,
    grant file path, grant_id, file sha256, CODEOWNERS-match);
  * ``test_resolver_fetches_from_main_not_branch`` asserts the ``@main`` ref;
  * ``test_orchestrator_resolves_grant_before_gate_command`` golden-chains through
    REAL dispatch (orchestrator -> resolver EFFECT -> orchestrator -> gate
    compute), proving the resolved fact reaches the gate and is NEVER taken from
    ``start.promotion_grant``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.events.runtime_deployment import (
    GRANT_FETCH_REF,
    GRANT_FILE_PATH,
    GRANT_REPO,
    EnumGrantResolution,
    EnumOccGateState,
    EnumRuntimeLane,
    ModelProdPromotionGateCommand,
    ModelProdPromotionGateDecision,
    ModelProdPromotionGrant,
    ModelProdPromotionGrantResolveCommand,
    ModelProdPromotionGrantResolvedEvent,
    ModelReadinessProjectionFact,
)
from omnimarket.nodes.node_prod_promotion_gate_compute.handlers.handler_prod_promotion_gate import (
    HandlerProdPromotionGate,
)
from omnimarket.nodes.node_prod_promotion_grant_resolver_effect.grant_resolver import (
    ModelPruneResult,
    file_sha256,
    prune_expired,
    resolve_grant,
)
from omnimarket.nodes.node_prod_promotion_grant_resolver_effect.handlers.handler_prod_promotion_grant_resolver import (
    GitHubMainGrantFetcher,
    HandlerProdPromotionGrantResolver,
    ModelGrantFetch,
    ProtocolGrantFetcher,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_GRANT_RESOLVE,
    TOPIC_PROD_GATE_EVALUATE,
    HandlerRedeployOrchestrator,
)
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

_DIGEST = "sha256:" + "a" * 64
_DIGEST_DRIFT = "sha256:" + "b" * 64
_BATCH = "batch-2026-06-21"
_ROLLBACK = "sha256:" + "c" * 64
_REQUESTER = "node_redeploy_orchestrator"
_APPROVER = "release-captain"
_GRANT_ID = "grant-0a1b2c3d-4e5f-6789-abcd-ef0123456789"
_EVALUATED_AT = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
_SOURCE_SHA = "deadbeefcafef00d1234567890abcdef12345678"


def _grant_entry(
    *,
    grant_id: str = _GRANT_ID,
    runtime_lane: str = "prod",
    image_digest: str = _DIGEST,
    promotion_batch_id: str = _BATCH,
    approved_by: str = _APPROVER,
    created_at: datetime = _EVALUATED_AT - timedelta(minutes=5),
    expires_at: datetime = _EVALUATED_AT + timedelta(hours=2),
    consumed: bool | None = None,
    consumed_at: datetime | None = None,
    consumed_by_correlation_id: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "grant_id": grant_id,
        "runtime_lane": runtime_lane,
        "image_digest": image_digest,
        "promotion_batch_id": promotion_batch_id,
        "approved_by": approved_by,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "reason": "stability-test READY, promoting to prod",
    }
    if consumed is not None:
        entry["consumed"] = consumed
    if consumed_at is not None:
        entry["consumed_at"] = consumed_at.isoformat().replace("+00:00", "Z")
    if consumed_by_correlation_id is not None:
        entry["consumed_by_correlation_id"] = consumed_by_correlation_id
    return entry


def _grant_file(*entries: dict[str, object]) -> bytes:
    return yaml.safe_dump({"entries": list(entries)}).encode("utf-8")


class _StubFetcher:
    """Test fetcher returning fixed bytes — no network."""

    def __init__(
        self,
        raw: bytes,
        *,
        source_commit_sha: str = _SOURCE_SHA,
        codeowners_match: bool = True,
    ) -> None:
        self._fetch = ModelGrantFetch(
            raw=raw,
            source_commit_sha=source_commit_sha,
            codeowners_match=codeowners_match,
        )

    async def fetch(self) -> ModelGrantFetch:
        return self._fetch


def _resolve(
    raw: bytes,
    *,
    digest: str | None = _DIGEST,
    batch: str | None = _BATCH,
    requested_by: str = _REQUESTER,
    evaluated_at: datetime = _EVALUATED_AT,
):
    return resolve_grant(
        raw,
        requested_image_digest=digest,
        promotion_batch_id=batch,
        requested_by=requested_by,
        evaluated_at=evaluated_at,
    )


# ---------------------------------------------------------------------------
# Pure resolver logic (parse + match + lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveGrant:
    def test_present_future_expiry_matching_resolves(self) -> None:
        result = _resolve(_grant_file(_grant_entry()))
        assert result.outcome is EnumGrantResolution.RESOLVED
        assert result.grant is not None
        assert isinstance(result.grant, ModelProdPromotionGrant)
        assert result.grant.approved_lane is EnumRuntimeLane.PROD
        assert result.grant.approved_image_digest == _DIGEST
        assert result.grant.approved_promotion_batch_id == _BATCH
        assert result.grant.approved_by == _APPROVER
        assert result.grant_id == _GRANT_ID

    def test_absent_when_empty(self) -> None:
        result = _resolve(_grant_file())
        assert result.outcome is EnumGrantResolution.ABSENT
        assert result.grant is None

    def test_absent_when_digest_mismatch(self) -> None:
        result = _resolve(_grant_file(_grant_entry()), digest=_DIGEST_DRIFT)
        assert result.outcome is EnumGrantResolution.ABSENT
        assert result.grant is None

    def test_absent_when_batch_mismatch(self) -> None:
        result = _resolve(_grant_file(_grant_entry()), batch="batch-other")
        assert result.outcome is EnumGrantResolution.ABSENT
        assert result.grant is None

    def test_absent_when_lane_mismatch(self) -> None:
        result = _resolve(_grant_file(_grant_entry(runtime_lane="stability_test")))
        assert result.outcome is EnumGrantResolution.ABSENT
        assert result.grant is None

    def test_expired_when_evaluated_past_expiry(self) -> None:
        entry = _grant_entry(expires_at=_EVALUATED_AT - timedelta(seconds=1))
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.EXPIRED
        assert result.grant is None
        assert result.grant_id == _GRANT_ID

    def test_exact_expiry_boundary_is_resolved(self) -> None:
        entry = _grant_entry(expires_at=_EVALUATED_AT)
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.RESOLVED
        assert result.grant is not None

    def test_self_granted_when_approver_equals_requester(self) -> None:
        entry = _grant_entry(approved_by=_REQUESTER)
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.SELF_GRANTED
        assert result.grant is None
        assert result.grant_id == _GRANT_ID

    def test_consumed_is_absent_no_replay(self) -> None:
        entry = _grant_entry(consumed=True)
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.CONSUMED
        assert result.grant is None
        assert result.grant_id == _GRANT_ID

    def test_consumed_false_marker_still_resolves(self) -> None:
        entry = _grant_entry(consumed=False)
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.RESOLVED

    def test_corrupt_anchor_raises_not_silent_absent(self) -> None:
        with pytest.raises(ValueError, match="entries"):
            _resolve(b"not: a grant file\n")

    def test_missing_required_field_raises(self) -> None:
        entry = _grant_entry()
        del entry["approved_by"]
        with pytest.raises(ValueError, match="missing required fields"):
            _resolve(_grant_file(entry))


# ---------------------------------------------------------------------------
# Single-use lifecycle: consumed-marker carry-through + expire/prune (OMN-13424)
# ---------------------------------------------------------------------------


_CONSUMED_CORR = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


@pytest.mark.unit
class TestGrantSingleUseLifecycle:
    """OMN-13424 — single-use consume + expire + prune lifecycle on the resolver."""

    def test_consumed_at_and_correlation_carry_through_when_resolved(self) -> None:
        # A still-live grant (consumed marker absent) that nonetheless carries the
        # optional consumed_at / consumed_by_correlation_id provenance fields
        # materializes them onto the typed grant DTO.
        consumed_at = _EVALUATED_AT - timedelta(minutes=1)
        entry = _grant_entry(
            consumed_at=consumed_at,
            consumed_by_correlation_id=_CONSUMED_CORR,
        )
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.RESOLVED
        assert result.grant is not None
        assert result.grant.consumed_at == consumed_at
        assert str(result.grant.consumed_by_correlation_id) == _CONSUMED_CORR

    def test_consumed_fields_default_none_when_absent(self) -> None:
        result = _resolve(_grant_file(_grant_entry()))
        assert result.grant is not None
        assert result.grant.consumed_at is None
        assert result.grant.consumed_by_correlation_id is None

    def test_consumed_marker_takes_precedence_over_provenance(self) -> None:
        # consumed: true is single-use spent regardless of carried provenance.
        entry = _grant_entry(
            consumed=True,
            consumed_at=_EVALUATED_AT - timedelta(minutes=1),
            consumed_by_correlation_id=_CONSUMED_CORR,
        )
        result = _resolve(_grant_file(entry))
        assert result.outcome is EnumGrantResolution.CONSUMED
        assert result.grant is None

    def test_prune_drops_expired_entry(self) -> None:
        live = _grant_entry()
        expired = _grant_entry(
            grant_id="grant-1111aaaa-2222-3333-4444-555566667777",
            expires_at=_EVALUATED_AT - timedelta(seconds=1),
        )
        result = prune_expired(_grant_file(live, expired), evaluated_at=_EVALUATED_AT)
        assert isinstance(result, ModelPruneResult)
        assert result.had_expired is True
        assert result.pruned_grant_ids == (
            "grant-1111aaaa-2222-3333-4444-555566667777",
        )
        kept = yaml.safe_load(result.raw.decode("utf-8"))["entries"]
        assert [e["grant_id"] for e in kept] == [_GRANT_ID]

    def test_prune_drops_consumed_entry_not_flagged_expired(self) -> None:
        consumed = _grant_entry(consumed=True)
        result = prune_expired(_grant_file(consumed), evaluated_at=_EVALUATED_AT)
        # Consumed entries are pruned but do NOT trip the expired lint signal.
        assert result.had_expired is False
        assert result.pruned_grant_ids == (_GRANT_ID,)
        assert yaml.safe_load(result.raw.decode("utf-8"))["entries"] == []

    def test_prune_keeps_fresh_unconsumed_entry(self) -> None:
        result = prune_expired(_grant_file(_grant_entry()), evaluated_at=_EVALUATED_AT)
        assert result.had_expired is False
        assert result.pruned_grant_ids == ()
        kept = yaml.safe_load(result.raw.decode("utf-8"))["entries"]
        assert [e["grant_id"] for e in kept] == [_GRANT_ID]

    def test_prune_at_rest_empty_file_is_noop(self) -> None:
        result = prune_expired(b"entries: []\n", evaluated_at=_EVALUATED_AT)
        assert result.had_expired is False
        assert result.pruned_grant_ids == ()
        assert yaml.safe_load(result.raw.decode("utf-8"))["entries"] == []

    def test_prune_corrupt_anchor_raises_not_silent(self) -> None:
        with pytest.raises(ValueError, match="entries"):
            prune_expired(b"not: a grant file\n", evaluated_at=_EVALUATED_AT)


# ---------------------------------------------------------------------------
# Resolver EFFECT handler (parse + emit + provenance)
# ---------------------------------------------------------------------------


def _command(
    *,
    digest: str | None = _DIGEST,
    batch: str | None = _BATCH,
    requested_by: str = _REQUESTER,
    evaluated_at: datetime = _EVALUATED_AT,
) -> ModelProdPromotionGrantResolveCommand:
    return ModelProdPromotionGrantResolveCommand(
        correlation_id=uuid4(),
        runtime_lane=EnumRuntimeLane.PROD,
        requested_image_digest=digest,
        promotion_batch_id=batch,
        requested_by=requested_by,
        evaluated_at=evaluated_at,
    )


@pytest.mark.unit
class TestResolverEffectHandler:
    async def test_resolved_event_carries_grant_and_provenance(self) -> None:
        raw = _grant_file(_grant_entry())
        stub: ProtocolGrantFetcher = _StubFetcher(raw)
        handler = HandlerProdPromotionGrantResolver(fetcher=stub)
        output = await handler.handle(_command())

        assert output.node_kind == EnumNodeKind.EFFECT
        event = output.events[0]
        assert isinstance(event, ModelProdPromotionGrantResolvedEvent)
        assert event.resolution is EnumGrantResolution.RESOLVED
        assert event.grant is not None
        assert event.grant.approved_image_digest == _DIGEST
        assert event.evaluated_at == _EVALUATED_AT

        # Provenance lives on the EMITTED audit evidence — fully populated.
        prov = event.provenance
        assert prov.source_commit_sha == _SOURCE_SHA
        assert prov.grant_file_path == GRANT_FILE_PATH
        assert prov.grant_id == _GRANT_ID
        assert prov.file_sha256 == hashlib.sha256(raw).hexdigest()
        assert len(prov.file_sha256) == 64
        assert prov.codeowners_match is True

    async def test_absent_grant_still_emits_provenance(self) -> None:
        raw = _grant_file()  # empty anchor
        handler = HandlerProdPromotionGrantResolver(fetcher=_StubFetcher(raw))
        output = await handler.handle(_command())

        event = output.events[0]
        assert isinstance(event, ModelProdPromotionGrantResolvedEvent)
        assert event.resolution is EnumGrantResolution.ABSENT
        assert event.grant is None
        # No matched entry -> grant_id is None but the audit trail is still durable.
        assert event.provenance.grant_id is None
        assert event.provenance.file_sha256 == file_sha256(raw)
        assert event.provenance.source_commit_sha == _SOURCE_SHA

    async def test_expired_grant_emits_none_with_typed_resolution(self) -> None:
        raw = _grant_file(_grant_entry(expires_at=_EVALUATED_AT - timedelta(seconds=1)))
        handler = HandlerProdPromotionGrantResolver(fetcher=_StubFetcher(raw))
        output = await handler.handle(_command())
        event = output.events[0]
        assert isinstance(event, ModelProdPromotionGrantResolvedEvent)
        assert event.resolution is EnumGrantResolution.EXPIRED
        assert event.grant is None

    async def test_self_granted_emits_none(self) -> None:
        raw = _grant_file(_grant_entry(approved_by=_REQUESTER))
        handler = HandlerProdPromotionGrantResolver(fetcher=_StubFetcher(raw))
        output = await handler.handle(_command(requested_by=_REQUESTER))
        event = output.events[0]
        assert isinstance(event, ModelProdPromotionGrantResolvedEvent)
        assert event.resolution is EnumGrantResolution.SELF_GRANTED
        assert event.grant is None

    async def test_consumed_emits_none(self) -> None:
        raw = _grant_file(_grant_entry(consumed=True))
        handler = HandlerProdPromotionGrantResolver(fetcher=_StubFetcher(raw))
        output = await handler.handle(_command())
        event = output.events[0]
        assert isinstance(event, ModelProdPromotionGrantResolvedEvent)
        assert event.resolution is EnumGrantResolution.CONSUMED
        assert event.grant is None


# ---------------------------------------------------------------------------
# Fetch-from-main anti-self-approval (mirrors reject-deploy-gate-skip.yml)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFetchFromMain:
    def test_resolver_fetches_immutable_main_commit_not_branch(self) -> None:
        """The default fetcher pins all reads to the resolved main commit.

        Resolving ``main`` first prevents a request from authoring the
        authorization that approves it by editing the grant file in the same PR
        branch while keeping provenance and CODEOWNERS checks on one immutable
        ref.
        """
        captured: list[str] = []

        class _RecordingFetcher(GitHubMainGrantFetcher):
            def _request(self, url: str) -> bytes:  # type: ignore[override]
                captured.append(url)
                if "/contents/" in url and GRANT_FILE_PATH in url:
                    content = base64.b64encode(_grant_file(_grant_entry())).decode()
                    return json.dumps({"content": content}).encode()
                if "/commits/" in url:
                    return json.dumps({"sha": _SOURCE_SHA}).encode()
                # CODEOWNERS probe.
                codeowners = f"{GRANT_FILE_PATH} @OmniNode-ai/platform-leads\n"
                return json.dumps(
                    {"content": base64.b64encode(codeowners.encode()).decode()}
                ).encode()

        fetcher = _RecordingFetcher(token="t")

        import asyncio

        fetched = asyncio.run(fetcher.fetch())
        assert fetched.source_commit_sha == _SOURCE_SHA
        assert fetched.codeowners_match is True

        # The grant file and CODEOWNERS MUST be fetched at the same immutable
        # main commit, never the PR branch.
        grant_url = next(u for u in captured if GRANT_FILE_PATH in u)
        codeowners_url = next(u for u in captured if "CODEOWNERS" in u)
        commit_url = next(u for u in captured if "/commits/" in u)
        assert f"ref={_SOURCE_SHA}" in grant_url
        assert f"ref={_SOURCE_SHA}" in codeowners_url
        assert commit_url.endswith(f"/commits/{GRANT_FETCH_REF}")
        assert GRANT_REPO in grant_url

    def test_grant_fetch_ref_constant_is_main(self) -> None:
        assert GRANT_FETCH_REF == "main"
        assert GRANT_REPO == "OmniNode-ai/onex_change_control"
        assert GRANT_FILE_PATH == "grants/prod_promotion_grants.yaml"


@pytest.mark.unit
def test_grant_resolve_command_rejects_non_prod_lane() -> None:
    with pytest.raises(ValidationError):
        ModelProdPromotionGrantResolveCommand(
            correlation_id=uuid4(),
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
            requested_image_digest=_DIGEST,
            promotion_batch_id=_BATCH,
            requested_by=_REQUESTER,
            evaluated_at=_EVALUATED_AT,
        )


# ---------------------------------------------------------------------------
# Golden chain through REAL dispatch (orchestrator -> resolver -> gate)
# ---------------------------------------------------------------------------


def _start(
    *,
    runtime_lane: EnumRuntimeLane = EnumRuntimeLane.PROD,
    requested_by: str = _REQUESTER,
) -> ModelRedeployStartCommand:
    return ModelRedeployStartCommand(
        correlation_id=uuid4(),
        runtime_lane=runtime_lane,
        image_digest=_DIGEST,
        promotion_batch_id=_BATCH,
        readiness_projection=ModelReadinessProjectionFact(
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
            readiness_state="READY",
            image_digest=_DIGEST,
            promotion_batch_id=_BATCH,
        ),
        occ_gate_state=EnumOccGateState.MERGED,
        rollback_target=_ROLLBACK,
        requested_by=requested_by,
    )


@pytest.mark.unit
class TestOrchestratorResolverGoldenChain:
    async def test_prod_start_emits_grant_resolve_before_gate(self) -> None:
        """A prod redeploy-start emits the grant-RESOLVE command (not the gate)."""
        orchestrator = HandlerRedeployOrchestrator()
        start = _start()
        envelope: ModelEventEnvelope[ModelRedeployStartCommand] = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await orchestrator.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_GRANT_RESOLVE]
        resolve_command = output.events[0].payload
        assert isinstance(resolve_command, ModelProdPromotionGrantResolveCommand)
        # The resolve command carries the request key + a deterministic evaluated_at.
        assert resolve_command.requested_image_digest == _DIGEST
        assert resolve_command.promotion_batch_id == _BATCH
        assert resolve_command.evaluated_at is not None

    async def test_orchestrator_resolves_grant_before_gate_command(self) -> None:
        """Golden chain: start -> resolver EFFECT -> orchestrator -> gate compute.

        Drives the REAL dispatch path (not handler-isolation): the resolver
        materializes the grant from the durable anchor, the orchestrator threads
        the RESOLVED grant + evaluated_at into the gate command, and the gate
        compute ALLOWS prod. The grant is NEVER taken from start.promotion_grant.
        """
        orchestrator = HandlerRedeployOrchestrator()
        # The orchestrator stamps a real ``datetime.now(UTC)`` into the resolve
        # command, so the anchor's grant must have a far-future absolute expiry for
        # the golden chain to be clock-independent.
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        far_past = datetime(2020, 1, 1, tzinfo=UTC)
        resolver = HandlerProdPromotionGrantResolver(
            fetcher=_StubFetcher(
                _grant_file(_grant_entry(created_at=far_past, expires_at=far_future))
            )
        )
        gate = HandlerProdPromotionGate()
        start = _start()

        # Edge 1: redeploy-start (prod) -> grant-resolve command.
        start_env: ModelEventEnvelope[ModelRedeployStartCommand] = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        start_out = await orchestrator.handle(start_env)
        assert [e.event_type for e in start_out.events] == [TOPIC_GRANT_RESOLVE]
        resolve_command = start_out.events[0].payload
        assert isinstance(resolve_command, ModelProdPromotionGrantResolveCommand)

        # Edge 2: resolver EFFECT resolves the grant from the durable anchor.
        resolve_out = await resolver.handle(resolve_command)
        assert resolve_out.node_kind == EnumNodeKind.EFFECT
        resolved = resolve_out.events[0]
        assert isinstance(resolved, ModelProdPromotionGrantResolvedEvent)
        assert resolved.resolution is EnumGrantResolution.RESOLVED
        assert resolved.grant is not None

        # Edge 3: grant-resolved -> orchestrator threads grant into gate command.
        resolved_env: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "resolved": resolved.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=start.correlation_id,
            event_type="onex.evt.omnimarket.prod-promotion-grant-resolved.v1",
        )
        gate_routed = await orchestrator.handle(resolved_env)
        assert [e.event_type for e in gate_routed.events] == [TOPIC_PROD_GATE_EVALUATE]
        gate_command = gate_routed.events[0].payload
        assert isinstance(gate_command, ModelProdPromotionGateCommand)
        # The RESOLVED grant + evaluated_at reached the gate command.
        assert gate_command.promotion_grant is not None
        assert gate_command.promotion_grant.approved_by == _APPROVER
        assert gate_command.evaluated_at == resolved.evaluated_at

        # Edge 4: gate COMPUTE evaluates -> prod ALLOWED with the resolved grant.
        gate_env: ModelEventEnvelope[ModelProdPromotionGateCommand] = (
            ModelEventEnvelope(
                payload=gate_command,
                correlation_id=gate_command.correlation_id,
                event_type=TOPIC_PROD_GATE_EVALUATE,
            )
        )
        gate_out = await gate.handle(gate_env)
        assert gate_out.node_kind == EnumNodeKind.COMPUTE
        decision = gate_out.result
        assert isinstance(decision, ModelProdPromotionGateDecision)
        assert decision.allowed is True

    async def test_resolved_none_blocks_gate_through_real_dispatch(self) -> None:
        """An ABSENT resolution threads grant=None so the gate fails closed."""
        orchestrator = HandlerRedeployOrchestrator()
        resolver = HandlerProdPromotionGrantResolver(
            fetcher=_StubFetcher(_grant_file())  # empty anchor -> ABSENT
        )
        gate = HandlerProdPromotionGate()
        start = _start()

        resolve_command = ModelProdPromotionGrantResolveCommand(
            correlation_id=start.correlation_id,
            runtime_lane=EnumRuntimeLane.PROD,
            requested_image_digest=start.image_digest,
            promotion_batch_id=start.promotion_batch_id,
            requested_by=start.requested_by,
            evaluated_at=_EVALUATED_AT,
        )
        resolve_out = await resolver.handle(resolve_command)
        resolved = resolve_out.events[0]
        assert isinstance(resolved, ModelProdPromotionGrantResolvedEvent)
        assert resolved.grant is None

        resolved_env: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
            payload={
                "resolved": resolved.model_dump(mode="json"),
                "start": start.model_dump(mode="json"),
            },
            correlation_id=start.correlation_id,
            event_type="onex.evt.omnimarket.prod-promotion-grant-resolved.v1",
        )
        gate_routed = await orchestrator.handle(resolved_env)
        gate_command = gate_routed.events[0].payload
        assert isinstance(gate_command, ModelProdPromotionGateCommand)
        assert gate_command.promotion_grant is None

        gate_env: ModelEventEnvelope[ModelProdPromotionGateCommand] = (
            ModelEventEnvelope(
                payload=gate_command,
                correlation_id=gate_command.correlation_id,
                event_type=TOPIC_PROD_GATE_EVALUATE,
            )
        )
        gate_out = await gate.handle(gate_env)
        decision = gate_out.result
        assert isinstance(decision, ModelProdPromotionGateDecision)
        assert decision.allowed is False
        assert "missing_promotion_grant" in decision.reason

    async def test_non_prod_start_skips_resolver(self) -> None:
        """Dev/stability redeploy-start go straight to the gate (no grant needed)."""
        orchestrator = HandlerRedeployOrchestrator()
        start = _start(runtime_lane=EnumRuntimeLane.DEV)
        envelope: ModelEventEnvelope[ModelRedeployStartCommand] = ModelEventEnvelope(
            payload=start,
            correlation_id=start.correlation_id,
            event_type="onex.cmd.omnimarket.redeploy-start.v1",
        )
        output = await orchestrator.handle(envelope)
        assert [e.event_type for e in output.events] == [TOPIC_PROD_GATE_EVALUATE]
        gate_command = output.events[0].payload
        assert isinstance(gate_command, ModelProdPromotionGateCommand)
        assert gate_command.promotion_grant is None
        assert gate_command.evaluated_at is None
