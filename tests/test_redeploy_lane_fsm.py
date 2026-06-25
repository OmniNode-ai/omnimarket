# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-policy, digest-gate, and FSM-phase tests (OMN-13211 / B3).

Covers the deploy domain primitives re-homed to
``omnimarket.events.runtime_deployment`` when the bespoke ``node_redeploy``
WorkflowPackage was decomposed into canonical nodes:

  - lane policy maps dev / stability-test / prod to compose project, overlays,
    and health targets;
  - the post-deploy verification segment (VERIFY_HEALTH -> PROBING -> ... ->
    READINESS_SCORING -> READY | BLOCKED) is additive — the base IDLE..DONE
    segment is unchanged;
  - the production same-digest gate blocks a direct prod deploy without a matching
    stability-test READY digest, and blocks a digest mismatch.
"""

from __future__ import annotations

import pytest

from omnimarket.events.runtime_deployment import (
    _VERIFICATION_SEQUENCE,  # test asserts segment shape
    TERMINAL_PHASES,
    EnumRedeployPhase,
    EnumRuntimeLane,
    ModelStabilityReadiness,
    evaluate_prod_digest_gate,
    lane_target,
    next_phase,
    next_verification_phase,
)

_DIGEST_STABILITY = "sha256:aaaa1111"
_DIGEST_PROD_DRIFT = "sha256:bbbb2222"

# Representative per-lane health URLs injected by tests via monkeypatch.  These
# match the compose service DNS names used in real lane deployments; the
# contract reference resolves them from the overlay env var at call time.
_DEV_HEALTH_URLS = (
    "http://omninode-runtime:8085/health;http://runtime-effects:8086/health"
)
_STABILITY_HEALTH_URLS = (
    "http://omninode-runtime:18085/health;http://runtime-effects:18086/health"
)
_PROD_HEALTH_URLS = (
    "http://omninode-runtime:28085/health;http://runtime-effects:28086/health"
)


# ---------------------------------------------------------------------------
# Lane policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLanePolicy:
    def test_dev_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNTIME_DEV_HEALTH_URLS", _DEV_HEALTH_URLS)
        target = lane_target(EnumRuntimeLane.DEV)
        assert target.compose_project == "omnibase-infra"
        assert target.rebuilds_from_source is True
        assert any("8085" in t for t in target.health_targets)

    def test_stability_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNTIME_STABILITY_HEALTH_URLS", _STABILITY_HEALTH_URLS)
        target = lane_target(EnumRuntimeLane.STABILITY_TEST)
        assert target.compose_project == "omnibase-infra-stability-test"
        assert "docker-compose.stability-test.yml" in target.compose_files
        assert any("18085" in t for t in target.health_targets)

    def test_prod_target_never_rebuilds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUNTIME_PROD_HEALTH_URLS", _PROD_HEALTH_URLS)
        target = lane_target(EnumRuntimeLane.PROD)
        assert target.compose_project == "omnibase-infra-prod"
        assert "docker-compose.prod.yml" in target.compose_files
        assert any("28085" in t for t in target.health_targets)
        # Production must never rebuild — it deploys a stability-proven digest.
        assert target.rebuilds_from_source is False

    def test_all_lanes_have_distinct_projects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RUNTIME_DEV_HEALTH_URLS", _DEV_HEALTH_URLS)
        monkeypatch.setenv("RUNTIME_STABILITY_HEALTH_URLS", _STABILITY_HEALTH_URLS)
        monkeypatch.setenv("RUNTIME_PROD_HEALTH_URLS", _PROD_HEALTH_URLS)
        projects = {lane_target(lane).compose_project for lane in EnumRuntimeLane}
        assert len(projects) == len(list(EnumRuntimeLane))

    def test_health_targets_overlay_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lane_target resolves health URLs from the overlay env var — not hardcoded."""
        custom_url = "http://myhost:9999/health"
        monkeypatch.setenv("RUNTIME_DEV_HEALTH_URLS", custom_url)
        target = lane_target(EnumRuntimeLane.DEV)
        assert target.health_targets == (custom_url,)

    def test_lane_target_fails_closed_when_health_url_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lane_target raises KeyError when the overlay health URL var is unset."""
        monkeypatch.delenv("RUNTIME_DEV_HEALTH_URLS", raising=False)
        with pytest.raises(KeyError, match="RUNTIME_DEV_HEALTH_URLS"):
            lane_target(EnumRuntimeLane.DEV)


# ---------------------------------------------------------------------------
# Production same-digest gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProdDigestGate:
    def test_prod_without_digest_blocked(self) -> None:
        decision = evaluate_prod_digest_gate(
            requested_digest=None, stability_readiness=None
        )
        assert decision.allowed is False
        assert "image_digest" in decision.reason

    def test_prod_without_stability_readiness_blocked(self) -> None:
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_STABILITY, stability_readiness=None
        )
        assert decision.allowed is False
        assert "stability" in decision.reason.lower()

    def test_prod_with_failed_stability_blocked(self) -> None:
        readiness = ModelStabilityReadiness(image_digest=_DIGEST_STABILITY, ready=False)
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_STABILITY, stability_readiness=readiness
        )
        assert decision.allowed is False
        assert "readiness failed" in decision.reason.lower()

    def test_prod_digest_mismatch_blocked(self) -> None:
        readiness = ModelStabilityReadiness(image_digest=_DIGEST_STABILITY, ready=True)
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_PROD_DRIFT, stability_readiness=readiness
        )
        assert decision.allowed is False
        assert "does not match" in decision.reason

    def test_prod_matching_stability_allowed_reuses_digest(self) -> None:
        readiness = ModelStabilityReadiness(image_digest=_DIGEST_STABILITY, ready=True)
        decision = evaluate_prod_digest_gate(
            requested_digest=_DIGEST_STABILITY, stability_readiness=readiness
        )
        assert decision.allowed is True
        # prod reuses the exact stability digest — no rebuild.
        assert decision.image_digest == _DIGEST_STABILITY


# ---------------------------------------------------------------------------
# Extended FSM transitions (pure phase helpers)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtendedFsm:
    def test_base_segment_unchanged(self) -> None:
        chain = [EnumRedeployPhase.IDLE]
        cur = EnumRedeployPhase.IDLE
        while cur != EnumRedeployPhase.DONE:
            cur = next_phase(cur)
            chain.append(cur)
        assert chain == [
            EnumRedeployPhase.IDLE,
            EnumRedeployPhase.SYNC_CLONES,
            EnumRedeployPhase.UPDATE_PINS,
            EnumRedeployPhase.REBUILD,
            EnumRedeployPhase.SEED_INFISICAL,
            EnumRedeployPhase.VERIFY_HEALTH,
            EnumRedeployPhase.DONE,
        ]

    def test_verification_segment_legal_order(self) -> None:
        chain = [EnumRedeployPhase.VERIFY_HEALTH]
        cur = EnumRedeployPhase.VERIFY_HEALTH
        while cur != EnumRedeployPhase.READINESS_SCORING:
            cur = next_verification_phase(cur)
            chain.append(cur)
        assert chain == [
            EnumRedeployPhase.VERIFY_HEALTH,
            EnumRedeployPhase.PROBING,
            EnumRedeployPhase.SWEEPING,
            EnumRedeployPhase.EVIDENCE_REDUCING,
            EnumRedeployPhase.OCC_DRAFTING,
            EnumRedeployPhase.OCC_VALIDATING,
            EnumRedeployPhase.READINESS_SCORING,
        ]

    def test_readiness_scoring_is_gate_decided(self) -> None:
        with pytest.raises(ValueError, match="READY or BLOCKED"):
            next_verification_phase(EnumRedeployPhase.READINESS_SCORING)

    def test_ready_and_blocked_and_rolled_back_are_terminal(self) -> None:
        assert EnumRedeployPhase.READY in TERMINAL_PHASES
        assert EnumRedeployPhase.BLOCKED in TERMINAL_PHASES
        assert EnumRedeployPhase.ROLLED_BACK in TERMINAL_PHASES

    def test_verification_phases_not_in_base_sequence(self) -> None:
        cur = EnumRedeployPhase.IDLE
        base_walk: set[EnumRedeployPhase] = {cur}
        while cur != EnumRedeployPhase.DONE:
            cur = next_phase(cur)
            base_walk.add(cur)
        assert base_walk.isdisjoint(set(_VERIFICATION_SEQUENCE))

    def test_cannot_advance_from_terminal(self) -> None:
        for terminal in TERMINAL_PHASES:
            with pytest.raises(ValueError, match="terminal phase"):
                next_phase(terminal)
