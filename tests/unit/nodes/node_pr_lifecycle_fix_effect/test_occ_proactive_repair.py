# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for OCC proactive delegated-repair path (OMN-12425).

Tests gap-detection + receipt generation in dry-run mode, idempotent
re-run no-op, verifier != runner enforcement, and all required receipt
fields (contract_sha256, pr_head_sha, source_repo, etc.).

All tests run without any real git, network, or GitHub API calls.
"""

from __future__ import annotations

import hashlib
import re
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract import (
    _RECEIPT_TEMPLATE,
    OccContractAdapter,
    _build_idempotency_key,
    _compute_contract_sha256,
    classify_trivial_infra_fastpath,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)


@pytest.fixture(autouse=True)
def _fake_github_token() -> object:
    """Stub the token resolver and product-PR fetch for every test.

    OMN-13990 switched the OccContractAdapter clone to HTTPS x-access-token, so
    ``_create_occ_contract_sync`` now resolves ``GITHUB_TOKEN`` and GETs the
    product PR body (early Evidence-Source guard) before cloning. These unit
    tests mock all git/network I/O; the resolver + rest_json must be stubbed so
    no test reaches the real secret store or GitHub. rest_json returns a body
    with NO Evidence-Source so the mutate flow proceeds past the early guard.
    """
    with (
        patch(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
            "adapter_occ_contract._resolve_github_token",
            return_value="fake-token",
        ) as mock_token,
        patch(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
            "adapter_occ_contract.rest_json",
            return_value={"body": ""},
        ),
    ):
        yield mock_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_contract_yaml(ticket_id: str = "OMN-12425") -> str:
    return f"""---
schema_version: "1.0.0"
ticket_id: "{ticket_id}"
title: "Test contract"
summary: "Test"
is_seam_ticket: false
interface_change: false
interfaces_touched: []
evidence_requirements: []
emergency_bypass:
  enabled: false
  justification: ""
  follow_up_ticket_id: ""
dod_evidence:
  - id: "dod-test"
    description: "test"
    source: "generated"
    checks: []
"""


# ---------------------------------------------------------------------------
# contract_sha256 computation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeContractSha256:
    def test_returns_hex_string(self) -> None:
        content = _sample_contract_yaml()
        result = _compute_contract_sha256(content)
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 hex

    def test_deterministic(self) -> None:
        content = _sample_contract_yaml()
        assert _compute_contract_sha256(content) == _compute_contract_sha256(content)

    def test_different_content_different_hash(self) -> None:
        a = _compute_contract_sha256("aaa")
        b = _compute_contract_sha256("bbb")
        assert a != b

    def test_matches_stdlib_sha256(self) -> None:
        content = _sample_contract_yaml()
        expected = hashlib.sha256(content.encode()).hexdigest()
        assert _compute_contract_sha256(content) == expected


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildIdempotencyKey:
    def test_key_is_deterministic(self) -> None:
        key1 = _build_idempotency_key(
            ticket_id="OMN-12425",
            evidence_item_id="dod-test",
            repo="OmniNode-ai/omnimarket",
            pr_head_sha="abc123",
            contract_sha256="deadbeef",
        )
        key2 = _build_idempotency_key(
            ticket_id="OMN-12425",
            evidence_item_id="dod-test",
            repo="OmniNode-ai/omnimarket",
            pr_head_sha="abc123",
            contract_sha256="deadbeef",
        )
        assert key1 == key2

    def test_key_changes_on_different_pr_head_sha(self) -> None:
        base = {
            "ticket_id": "OMN-12425",
            "evidence_item_id": "dod-test",
            "repo": "OmniNode-ai/omnimarket",
            "pr_head_sha": "abc123",
            "contract_sha256": "deadbeef",
        }
        key1 = _build_idempotency_key(**base)
        key2 = _build_idempotency_key(**{**base, "pr_head_sha": "def456"})
        assert key1 != key2

    def test_key_changes_on_different_contract_sha256(self) -> None:
        base = {
            "ticket_id": "OMN-12425",
            "evidence_item_id": "dod-test",
            "repo": "OmniNode-ai/omnimarket",
            "pr_head_sha": "abc123",
            "contract_sha256": "deadbeef",
        }
        key1 = _build_idempotency_key(**base)
        key2 = _build_idempotency_key(**{**base, "contract_sha256": "cafebabe"})
        assert key1 != key2

    def test_key_all_five_components_matter(self) -> None:
        base = {
            "ticket_id": "OMN-12425",
            "evidence_item_id": "dod-test",
            "repo": "OmniNode-ai/omnimarket",
            "pr_head_sha": "abc123",
            "contract_sha256": "deadbeef",
        }
        keys = set()
        for field in base:
            modified = {**base, field: base[field] + "-X"}
            keys.add(_build_idempotency_key(**modified))
        # All 5 modifications produce different keys
        assert len(keys) == 5


# ---------------------------------------------------------------------------
# Receipt template: required fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReceiptTemplateRequiredFields:
    """Receipt template must include all mandatory fields per DoD."""

    def test_contract_sha256_field_present(self) -> None:
        assert "contract_sha256" in _RECEIPT_TEMPLATE

    def test_pr_head_sha_field_present(self) -> None:
        assert "pr_head_sha" in _RECEIPT_TEMPLATE

    def test_source_repo_field_present(self) -> None:
        assert "source_repo" in _RECEIPT_TEMPLATE

    def test_run_timestamp_field_present(self) -> None:
        assert "run_timestamp" in _RECEIPT_TEMPLATE

    def test_commit_sha_field_present(self) -> None:
        assert "commit_sha" in _RECEIPT_TEMPLATE

    def test_probe_command_field_present(self) -> None:
        assert "probe_command" in _RECEIPT_TEMPLATE

    def test_probe_stdout_field_present(self) -> None:
        assert "probe_stdout" in _RECEIPT_TEMPLATE

    def test_runner_field_present(self) -> None:
        assert "runner" in _RECEIPT_TEMPLATE

    def test_verifier_field_present(self) -> None:
        assert "verifier" in _RECEIPT_TEMPLATE

    def test_no_unsubstituted_placeholders_after_format(self) -> None:
        rendered = _RECEIPT_TEMPLATE.format(
            ticket_id="OMN-12425",
            evidence_id="dod-test",
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            run_timestamp="2026-06-29T00:00:00Z",
            commit_sha="abc123",
            branch="auto/omn-12425-occ-contract",
            repo_slug="OmniNode-ai-omnimarket",
            contract_sha256="deadbeef" * 8,
            pr_head_sha="headsha456",
            source_repo="OmniNode-ai/omnimarket",
            runner="node_pr_lifecycle_fix_effect",
            verifier="occ-auto-contract-verifier",
        )
        unresolved = re.findall(r"\{[a-z_]+\}", rendered)
        assert unresolved == [], f"unresolved placeholders: {unresolved}"


# ---------------------------------------------------------------------------
# verifier != runner enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerifierRunnerEnforcement:
    """Receipts must REJECT verifier == runner (self-attestation)."""

    def test_self_attestation_raises_value_error(self) -> None:
        adapter = OccContractAdapter()
        with pytest.raises(ValueError, match="self-attestation"):
            adapter._validate_verifier_not_runner(
                runner="node_pr_lifecycle_fix_effect",
                verifier="node_pr_lifecycle_fix_effect",
            )

    def test_different_verifier_and_runner_ok(self) -> None:
        adapter = OccContractAdapter()
        # Must not raise
        adapter._validate_verifier_not_runner(
            runner="node_pr_lifecycle_fix_effect",
            verifier="occ-auto-contract-verifier",
        )

    def test_verifier_runner_mismatch_required_in_create_sync(
        self, tmp_path: object
    ) -> None:
        """_create_occ_contract_sync must enforce verifier != runner."""
        adapter = OccContractAdapter(
            runner="test-runner",
            verifier="test-runner",  # same as runner → must fail
        )

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            return "abc123" if "rev-parse" in argv else ""

        with (
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch("tempfile.TemporaryDirectory") as mock_tmpdir,
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake-occ")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx

            with pytest.raises(ValueError, match="self-attestation"):
                adapter._create_occ_contract_sync(
                    "OmniNode-ai/omnimarket", 42, "OMN-12425"
                )


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryRunMode:
    """dry_run mode: gap detected, description returned, NO mutations."""

    def test_dry_run_detects_missing_contract_gap(self) -> None:
        adapter = OccContractAdapter()
        result = adapter.detect_occ_gap(
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
            ticket_id="OMN-12425",
            contract_exists=False,
            receipt_exists=False,
        )
        assert result["has_gap"] is True
        assert "contract" in result["gap_reason"].lower()

    def test_dry_run_detects_missing_receipt_gap(self) -> None:
        adapter = OccContractAdapter()
        result = adapter.detect_occ_gap(
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
            ticket_id="OMN-12425",
            contract_exists=True,
            receipt_exists=False,
        )
        assert result["has_gap"] is True
        assert "receipt" in result["gap_reason"].lower()

    def test_no_gap_when_both_exist(self) -> None:
        adapter = OccContractAdapter()
        result = adapter.detect_occ_gap(
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
            ticket_id="OMN-12425",
            contract_exists=True,
            receipt_exists=True,
        )
        assert result["has_gap"] is False

    def test_create_occ_contract_dry_run_no_git_calls(self) -> None:
        """In dry-run, _run_git must NOT be called."""
        adapter = OccContractAdapter(mode="dry_run")
        git_calls: list[list[str]] = []

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            return ""

        with patch.object(adapter, "_run_git", side_effect=fake_run_git):
            result = adapter._create_occ_contract_sync(
                "OmniNode-ai/omnimarket", 42, "OMN-12425"
            )

        assert git_calls == [], "dry_run must not call _run_git"
        assert "[dry-run]" in result

    def test_create_occ_contract_dry_run_no_occ_pr(self) -> None:
        """In dry-run, _open_occ_pr must NOT be called."""
        adapter = OccContractAdapter(mode="dry_run")
        open_pr_calls: list[dict] = []

        def fake_open_pr(**kw: object) -> int:
            open_pr_calls.append(dict(kw))
            return 99

        with patch.object(adapter, "_open_occ_pr", side_effect=fake_open_pr):
            adapter._create_occ_contract_sync("OmniNode-ai/omnimarket", 42, "OMN-12425")

        assert open_pr_calls == [], "dry_run must not open OCC PR"

    def test_create_occ_contract_dry_run_no_evidence_append(self) -> None:
        """In dry-run, _append_evidence_to_pr must NOT be called."""
        adapter = OccContractAdapter(mode="dry_run")
        append_calls: list[dict] = []

        def fake_append(**kw: object) -> None:
            append_calls.append(dict(kw))

        with patch.object(adapter, "_append_evidence_to_pr", side_effect=fake_append):
            adapter._create_occ_contract_sync("OmniNode-ai/omnimarket", 42, "OMN-12425")

        assert append_calls == [], "dry_run must not append evidence"

    def test_create_occ_contract_mutate_mode_calls_all_steps(self) -> None:
        """mutate mode: all side-effect steps are called."""
        adapter = OccContractAdapter(mode="mutate")
        git_calls: list[list[str]] = []

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            return "abc123" if "rev-parse" in argv else ""

        append_calls: list[dict] = []

        with (
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_open_occ_pr", return_value=55),
            patch.object(
                adapter,
                "_append_evidence_to_pr",
                side_effect=lambda **kw: append_calls.append(kw),
            ),
            patch("tempfile.TemporaryDirectory") as mock_tmpdir,
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake-occ")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx

            action = adapter._create_occ_contract_sync(
                "OmniNode-ai/omnimarket", 42, "OMN-12425"
            )

        clone_call = next((c for c in git_calls if "clone" in c), None)
        assert clone_call is not None, "mutate must git clone"
        assert "OMN-12425" in action


# ---------------------------------------------------------------------------
# Idempotency: re-run is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdempotentRerun:
    """Identical re-run (same idempotency key) is a no-op."""

    def test_idempotent_rerun_skips_git(self) -> None:
        """Second call with same key must return no-op without _run_git."""
        adapter = OccContractAdapter(mode="mutate")
        git_calls: list[list[str]] = []

        pr_head_sha = "headsha-abc123"

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            return "contractsha" if "rev-parse" in argv else ""

        with (
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_open_occ_pr", return_value=55),
            patch.object(adapter, "_append_evidence_to_pr"),
            patch("tempfile.TemporaryDirectory") as mock_tmpdir,
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake-occ")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx

            # First call: must do work
            adapter._create_occ_contract_sync(
                "OmniNode-ai/omnimarket",
                42,
                "OMN-12425",
                pr_head_sha=pr_head_sha,
            )

        git_calls_after_first = list(git_calls)
        git_calls.clear()

        # Second call with same pr_head_sha: must short-circuit
        result2 = adapter._create_occ_contract_sync(
            "OmniNode-ai/omnimarket",
            42,
            "OMN-12425",
            pr_head_sha=pr_head_sha,
        )

        assert git_calls == [], "idempotent rerun must not call _run_git"
        assert "[no-op]" in result2 or "no-op" in result2.lower()
        assert git_calls_after_first  # first call did work

    def test_different_pr_head_sha_not_idempotent(self) -> None:
        """Different pr_head_sha → not idempotent, must do work."""
        adapter = OccContractAdapter(mode="mutate")
        git_calls: list[list[str]] = []

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            return "abc" if "rev-parse" in argv else ""

        with (
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_open_occ_pr", return_value=55),
            patch.object(adapter, "_append_evidence_to_pr"),
            patch("tempfile.TemporaryDirectory") as mock_tmpdir,
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake-occ")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx

            adapter._create_occ_contract_sync(
                "OmniNode-ai/omnimarket",
                42,
                "OMN-12425",
                pr_head_sha="sha-v1",
            )

        _ = list(git_calls)  # first run produced calls (validated by clone_calls below)
        git_calls.clear()

        with (
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_open_occ_pr", return_value=56),
            patch.object(adapter, "_append_evidence_to_pr"),
            patch("tempfile.TemporaryDirectory") as mock_tmpdir2,
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            mock_ctx2 = MagicMock()
            mock_ctx2.__enter__ = MagicMock(return_value="/tmp/fake-occ2")
            mock_ctx2.__exit__ = MagicMock(return_value=False)
            mock_tmpdir2.return_value = mock_ctx2

            adapter._create_occ_contract_sync(
                "OmniNode-ai/omnimarket",
                42,
                "OMN-12425",
                pr_head_sha="sha-v2",  # different SHA
            )

        # Second call DID work
        clone_calls = [c for c in git_calls if "clone" in c]
        assert clone_calls, "different SHA must trigger full create"


# ---------------------------------------------------------------------------
# Handler integration: gap_detection block reason
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerGapDetectionIntegration:
    """Handler routes occ_gap_detected to OCC contract adapter in dry-run."""

    async def test_deploy_gate_contract_not_found_dry_run_uses_noop(self) -> None:
        """dry_run=True on command uses noop adapter → [noop] in action."""
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
# Trivial-infra OCC fast-path (OMN-13776)
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
        """A runtime-touching change (node handler .py file) never takes the
        fast-path, even at 1 changed line."""
        eligible, reason = classify_trivial_infra_fastpath(
            [
                "src/omnimarket/nodes/node_pr_lifecycle_fix_effect/handlers/"
                "adapter_occ_contract.py"
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
        """Path-scoped infra file but diff too large -> not trivial."""
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
    """HandlerPrLifecycleFix skips the OCC adapter entirely on a fast-path hit."""

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
        takes the fast-path — still routes to the OCC adapter."""
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
