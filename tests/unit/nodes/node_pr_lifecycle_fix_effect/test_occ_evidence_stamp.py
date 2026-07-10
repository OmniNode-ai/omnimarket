# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for the deterministic OCC companion-artifact renderer (OMN-14285).

RSD seam-tests-first: these pin the exact byte-shape + determinism contract the
single OCC producer emits and the occ-preflight / receipt-gate consume. They are
authored BEFORE the effect writer so the effect writer is proven against the seam,
never the other way round.

The seam is pure: zero git/gh/network/filesystem I/O is touched here.
"""

from __future__ import annotations

import re

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    build_idempotency_key,
    classify_trivial_infra_fastpath,
    compute_contract_sha256,
    rebind_contract_sha256_in_text,
    render_companion_contract,
    render_downstream_receipt,
    render_self_bind_receipt,
)

_NAMED_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")


@pytest.mark.unit
class TestCompanionContractRender:
    def test_renders_required_gate_fields(self) -> None:
        rendered = render_companion_contract(
            ticket_id="OMN-9999",
            repo="OmniNode-ai/omnimarket",
            pr_number=123,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-123",
        )
        assert 'ticket_id: "OMN-9999"' in rendered
        assert "schema_version:" in rendered
        assert "is_seam_ticket: false" in rendered
        assert "interface_change: false" in rendered
        assert "dod_evidence:" in rendered
        assert "dod-OmniNode-ai-omnimarket-pr-123" in rendered

    def test_no_unsubstituted_named_placeholders(self) -> None:
        rendered = render_companion_contract(
            ticket_id="OMN-1234",
            repo="OmniNode-ai/test",
            pr_number=7,
            evidence_id="dod-OmniNode-ai-test-pr-7",
        )
        assert _NAMED_PLACEHOLDER_RE.findall(rendered) == []

    def test_deterministic_same_inputs_same_bytes(self) -> None:
        kwargs = {
            "ticket_id": "OMN-1",
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 1,
            "evidence_id": "dod-x",
        }
        assert render_companion_contract(**kwargs) == render_companion_contract(
            **kwargs
        )


@pytest.mark.unit
class TestDownstreamReceiptRender:
    def _render(self) -> str:
        return render_downstream_receipt(
            ticket_id="OMN-9999",
            evidence_id="dod-OmniNode-ai-omnimarket-pr-123",
            pr_number=123,
            repo="OmniNode-ai/omnimarket",
            run_timestamp="2026-07-10T00:00:00Z",
            commit_sha="abc1234",
            branch="auto/omninode-ai-omnimarket-pr-123-occ-autobind",
            probe_command="gh pr view 123 --repo OmniNode-ai/omnimarket --json number,state",
            probe_stdout='{"number":123,"state":"OPEN"}',
            exit_code=0,
        )

    def test_renders_required_receipt_fields(self) -> None:
        rendered = self._render()
        assert 'ticket_id: "OMN-9999"' in rendered
        assert "status: PASS" in rendered
        assert "pr_number: 123" in rendered
        assert 'commit_sha: "abc1234"' in rendered
        assert "runner:" in rendered
        assert "verifier:" in rendered
        # Contract binding starts as a rebindable PENDING sentinel.
        assert 'contract_sha256: "sha256:PENDING"' in rendered

    def test_no_unsubstituted_named_placeholders(self) -> None:
        # Only the intentional literal-JSON probe line may carry braces; all
        # *named* {placeholder} tokens must be gone.
        assert _NAMED_PLACEHOLDER_RE.findall(self._render()) == []

    def test_verifier_defaults_differ_from_runner(self) -> None:
        # The receipt-gate rejects verifier == runner (self-attestation).
        rendered = self._render()
        runner = re.search(r'runner:\s*"([^"]+)"', rendered)
        verifier = re.search(r'verifier:\s*"([^"]+)"', rendered)
        assert runner is not None
        assert verifier is not None
        assert runner.group(1) != verifier.group(1)


@pytest.mark.unit
class TestSelfBindReceiptRender:
    def test_renders_occ_self_bind_fields(self) -> None:
        rendered = render_self_bind_receipt(
            ticket_id="OMN-9999",
            evidence_id="occ-self-bind-pr-42",
            occ_pr_number=42,
            occ_repo="OmniNode-ai/onex_change_control",
            run_timestamp="2026-07-10T00:00:00Z",
            occ_commit_sha="def5678",
            branch="auto/omninode-ai-omnimarket-pr-123-occ-autobind",
            probe_command="gh pr view 42 --repo OmniNode-ai/onex_change_control --json number,state",
            probe_stdout='{"number":42,"state":"OPEN"}',
            exit_code=0,
        )
        assert "OCC#42" in rendered
        assert "pr_number: 42" in rendered
        assert 'commit_sha: "def5678"' in rendered
        assert _NAMED_PLACEHOLDER_RE.findall(rendered) == []


@pytest.mark.unit
class TestContractSha256:
    def test_bare_hex_digest_stable(self) -> None:
        content = "some contract bytes"
        assert compute_contract_sha256(content) == compute_contract_sha256(content)
        assert len(compute_contract_sha256(content)) == 64

    def test_str_and_bytes_agree(self) -> None:
        assert compute_contract_sha256("abc") == compute_contract_sha256(b"abc")

    def test_distinct_content_distinct_digest(self) -> None:
        assert compute_contract_sha256("aaa") != compute_contract_sha256("bbb")

    def test_rebind_sets_prefixed_line(self) -> None:
        receipt = 'x: 1\ncontract_sha256: "sha256:PENDING"\ny: 2\n'
        digest = "a" * 64
        rebound = rebind_contract_sha256_in_text(receipt, digest)
        assert f'contract_sha256: "sha256:{digest}"' in rebound
        assert "PENDING" not in rebound
        # Only the contract_sha256 line changed.
        assert "x: 1" in rebound
        assert "y: 2" in rebound

    def test_rebind_is_idempotent_fixpoint(self) -> None:
        digest = "b" * 64
        once = rebind_contract_sha256_in_text(
            'contract_sha256: "sha256:PENDING"\n', digest
        )
        twice = rebind_contract_sha256_in_text(once, digest)
        assert once == twice


@pytest.mark.unit
class TestIdempotencyKey:
    def _base(self) -> dict[str, str]:
        return {
            "ticket_id": "OMN-9999",
            "evidence_item_id": "dod-x",
            "repo": "OmniNode-ai/omnimarket",
            "pr_head_sha": "abc123",
            "contract_sha256": "deadbeef",
        }

    def test_deterministic(self) -> None:
        assert build_idempotency_key(**self._base()) == build_idempotency_key(
            **self._base()
        )

    def test_head_sha_changes_key(self) -> None:
        assert build_idempotency_key(**self._base()) != build_idempotency_key(
            **{**self._base(), "pr_head_sha": "def456"}
        )

    def test_contract_sha_changes_key(self) -> None:
        assert build_idempotency_key(**self._base()) != build_idempotency_key(
            **{**self._base(), "contract_sha256": "cafebabe"}
        )


@pytest.mark.unit
class TestTrivialInfraFastpath:
    def test_empty_file_list_never_qualifies(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath([], total_diff_lines=0)
        assert eligible is False
        assert "cannot prove triviality" in reason

    def test_runtime_python_never_qualifies(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["src/omnimarket/nodes/node_x/handlers/handler_x.py"], total_diff_lines=1
        )
        assert eligible is False
        assert "runtime-touching" in reason

    def test_migration_never_qualifies(self) -> None:
        eligible, _ = classify_trivial_infra_fastpath(
            ["migrations/0007_add_table.sql"], total_diff_lines=1
        )
        assert eligible is False

    def test_trivial_dockerfile_bump_qualifies(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["docker/Dockerfile"], total_diff_lines=1
        )
        assert eligible is True
        assert "fast-path" in reason

    def test_too_many_lines_disqualifies(self) -> None:
        eligible, reason = classify_trivial_infra_fastpath(
            ["docker/Dockerfile"], total_diff_lines=99
        )
        assert eligible is False
        assert "exceeds trivial threshold" in reason
