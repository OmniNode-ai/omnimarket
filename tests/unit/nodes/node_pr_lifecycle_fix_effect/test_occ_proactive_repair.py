# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the OCC proactive-repair surface on the converged producer.

OMN-12425 (proactive delegated repair) originally shipped as ``OccContractAdapter``.
OMN-14285 converged that + ``OccAutobindAdapter`` into the single
:class:`OccCompanionEmitter`; the deterministic render/hash/classify helpers moved
to the pure :mod:`occ_evidence_stamp` seam (covered by ``test_occ_evidence_stamp``).

This file keeps the still-meaningful proactive-repair behaviors on the converged
producer: gap detection, construction-time ``verifier != runner`` enforcement,
the trivial-infra OCC fast-path (seam function + handler routing), and the
no-side-effect dry-run mode.

All tests run without any real git, network, or GitHub API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    classify_trivial_infra_fastpath,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

# ---------------------------------------------------------------------------
# verifier != runner enforcement (now construction-time, OMN-12791 / OMN-14285)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerifierRunnerEnforcement:
    """The producer must REJECT verifier == runner (self-attestation)."""

    def test_self_attestation_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="self-attestation"):
            OccCompanionEmitter(runner="same", verifier="same")

    def test_different_verifier_and_runner_constructs(self) -> None:
        emitter = OccCompanionEmitter(
            runner="node_pr_lifecycle_fix_effect",
            verifier="occ-evidence-source-autobind",
        )
        # Guard helper is idempotent and callable post-construction.
        emitter._validate_verifier_not_runner(runner="a", verifier="b")

    def test_default_construction_has_distinct_identities(self) -> None:
        emitter = OccCompanionEmitter()
        assert emitter._runner != emitter._verifier


# ---------------------------------------------------------------------------
# Gap detection (pure)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectOccGap:
    def test_detects_missing_contract_and_receipt(self) -> None:
        result = OccCompanionEmitter().detect_occ_gap(
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
            ticket_id="OMN-12425",
            contract_exists=False,
            receipt_exists=False,
        )
        assert result["has_gap"] is True
        assert "contract" in result["gap_reason"].lower()

    def test_detects_missing_receipt(self) -> None:
        result = OccCompanionEmitter().detect_occ_gap(
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
            ticket_id="OMN-12425",
            contract_exists=True,
            receipt_exists=False,
        )
        assert result["has_gap"] is True
        assert "receipt" in result["gap_reason"].lower()

    def test_no_gap_when_both_exist(self) -> None:
        result = OccCompanionEmitter().detect_occ_gap(
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
            ticket_id="OMN-12425",
            contract_exists=True,
            receipt_exists=True,
        )
        assert result["has_gap"] is False


# ---------------------------------------------------------------------------
# Dry-run mode: describe intent, ZERO side effects (no token, no git, no network)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryRunMode:
    async def test_create_occ_contract_dry_run_no_io(self) -> None:
        emitter = OccCompanionEmitter(mode="dry_run")
        # No I/O is mocked — a dry-run that touched git/network would raise here.
        result = await emitter.create_occ_contract(
            "OmniNode-ai/omnimarket", 42, "OMN-12425"
        )
        assert "[dry-run]" in result
        assert "OMN-12425" in result

    async def test_autobind_dry_run_no_io(self) -> None:
        emitter = OccCompanionEmitter(mode="dry_run")
        result = await emitter.autobind_evidence_source(
            "OmniNode-ai/omnimarket", 42, "OMN-12425"
        )
        assert "[dry-run]" in result


# ---------------------------------------------------------------------------
# Handler integration: deploy-gate dry-run command uses the noop adapter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerGapDetectionIntegration:
    async def test_deploy_gate_contract_not_found_dry_run_uses_noop(self) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        handler = HandlerPrLifecycleFix()  # all noop adapters
        command = ModelPrLifecycleFixCommand(
            correlation_id=uuid4(),
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            block_reason=EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
            ticket_id="OMN-12425",
            dry_run=True,
            requested_at=datetime.now(tz=UTC),
        )
        result = await handler.handle(command)
        assert result.fix_applied is True
        assert "[noop]" in result.fix_action
        assert result.error is None


# ---------------------------------------------------------------------------
# Trivial-infra OCC fast-path (OMN-13776) — seam function
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClassifyTrivialInfraFastpath:
    """Size-and-path-scoped exemption for one-line non-runtime infra edits."""

    def test_oneline_dockerfile_bump_is_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["deploy/Dockerfile"], total_diff_lines=2
        )
        assert eligible is True
        assert "fast-path" in reason

    def test_musl_version_bump_is_eligible(self) -> None:
        eligible, _ = classify_trivial_infra_fastpath(
            ["deploy/musl-toolchain.txt"], total_diff_lines=2
        )
        assert eligible is True

    def test_requirements_txt_bump_is_eligible(self) -> None:
        eligible, _ = classify_trivial_infra_fastpath(
            ["requirements-lock.txt"], total_diff_lines=1
        )
        assert eligible is True

    def test_empty_changed_files_never_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath([], total_diff_lines=0)
        assert eligible is False
        assert "no changed_files" in reason

    def test_runtime_handler_change_not_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            [
                "src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/"
                "occ_companion_emitter.py"
            ],
            total_diff_lines=1,
        )
        assert eligible is False
        assert "runtime-touching" in reason

    def test_migration_change_not_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["src/omnimarket/nodes/node_x/migrations/0001_init.sql"],
            total_diff_lines=1,
        )
        assert eligible is False
        assert "runtime-touching" in reason

    def test_large_diff_on_infra_file_not_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["Dockerfile"], total_diff_lines=50
        )
        assert eligible is False
        assert "exceeds trivial threshold" in reason

    def test_too_many_files_not_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["Dockerfile", "requirements.txt", ".python-version"],
            total_diff_lines=3,
        )
        assert eligible is False
        assert "exceeds trivial threshold" in reason

    def test_mixed_infra_and_source_file_not_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["Dockerfile", "src/omnimarket/nodes/node_x/handlers/handler_x.py"],
            total_diff_lines=2,
        )
        assert eligible is False
        assert "runtime-touching" in reason

    def test_non_allowlisted_infra_path_not_eligible(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["README.md"], total_diff_lines=1
        )
        assert eligible is False
        assert "allowlist" in reason


@pytest.mark.unit
class TestHandlerTrivialInfraFastpathRouting:
    """HandlerPrLifecycleFix skips the OCC producer entirely on a fast-path hit."""

    async def test_trivial_infra_edit_skips_occ_adapter(self) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        occ = MagicMock()
        occ.create_occ_contract = MagicMock(
            side_effect=AssertionError("must not be called on fast-path hit")
        )
        handler = HandlerPrLifecycleFix(occ_contract_adapter=occ)
        command = ModelPrLifecycleFixCommand(
            correlation_id=uuid4(),
            pr_number=99,
            repo="OmniNode-ai/omnimarket",
            block_reason=EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
            ticket_id="OMN-13765",
            requested_at=datetime.now(tz=UTC),
            changed_files=["deploy/Dockerfile"],
            diff_total_lines=2,
        )
        result = await handler.handle(command)
        assert result.fix_applied is True
        assert result.error is None
        assert "OCC fast-path" in result.fix_action
        occ.create_occ_contract.assert_not_called()

    async def test_runtime_touching_change_still_calls_occ_adapter(self) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        occ = MagicMock()

        async def _create_occ_contract(
            repo: str, pr_number: int, ticket_id: str
        ) -> str:
            return f"created OCC contract for {ticket_id} on {repo}#{pr_number}"

        occ.create_occ_contract = _create_occ_contract
        handler = HandlerPrLifecycleFix(occ_contract_adapter=occ)
        command = ModelPrLifecycleFixCommand(
            correlation_id=uuid4(),
            pr_number=100,
            repo="OmniNode-ai/omnimarket",
            block_reason=EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
            ticket_id="OMN-13765",
            requested_at=datetime.now(tz=UTC),
            changed_files=[
                "src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/"
                "handler_pr_lifecycle_fix.py"
            ],
            diff_total_lines=1,
        )
        result = await handler.handle(command)
        assert result.fix_applied is True
        assert "created OCC contract" in result.fix_action

    async def test_no_changed_files_falls_back_to_occ_adapter(self) -> None:
        """Backward compat: omitting changed_files (existing callers) never
        takes the fast-path — still routes to the OCC producer."""
        from datetime import UTC, datetime
        from uuid import uuid4

        occ = MagicMock()

        async def _create_occ_contract(
            repo: str, pr_number: int, ticket_id: str
        ) -> str:
            return f"created OCC contract for {ticket_id} on {repo}#{pr_number}"

        occ.create_occ_contract = _create_occ_contract
        handler = HandlerPrLifecycleFix(occ_contract_adapter=occ)
        command = ModelPrLifecycleFixCommand(
            correlation_id=uuid4(),
            pr_number=101,
            repo="OmniNode-ai/omnimarket",
            block_reason=EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND,
            ticket_id="OMN-12425",
            requested_at=datetime.now(tz=UTC),
        )
        result = await handler.handle(command)
        assert result.fix_applied is True
        assert "created OCC contract" in result.fix_action
