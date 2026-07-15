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
    ci_check_evidence_id,
    classify_trivial_infra_fastpath,
    compute_contract_sha256,
    extract_evidence_item_id,
    rebind_contract_entry_sha256_in_text,
    rebind_contract_sha256_in_text,
    render_ci_check_receipt,
    render_companion_contract,
    render_downstream_receipt,
    render_self_bind_dod_evidence_item,
    render_self_bind_receipt,
)

# Mirrors the classifier `derive_proof_tier` ships in onex_change_control#3990
# (scripts/validation/check_contract_substance_floor.py, OMN-14409): a `gh pr
# view --json <metadata-only-fields>` command is an existence probe (tier L0);
# anything else — including `gh pr checks` — is left to derive a higher tier.
# Re-derived here (not imported) because #3990 lives in a different repo; the
# RED->GREEN proof against the REAL deriver is run manually and cited in the
# PR body per OMN-14425.
_GH_PR_VIEW_RE = re.compile(r"\bgh\s+pr\s+view\b")


def _is_existence_probe_like_omn_14409(command: str) -> bool:
    return bool(_GH_PR_VIEW_RE.search(command))


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
class TestContractSubstanceFloor:
    """OMN-14425: the autobind must emit a check that derives above tier L0.

    OMN-14409's contract substance floor (OCC#3990) rejects a contract whose
    ENTIRE dod_evidence is existence probes. Proven live: contracts/OMN-14400.yaml
    and contracts/OMN-14411.yaml were minted with 100% `gh pr view --json
    number,state` checks and would fail that gate. This is the RED->GREEN proof
    at the render layer: the OLD single-item shape is all-existence (would FAIL
    #3990's gate); the NEW two-item shape adds a check that is not an existence
    probe (would PASS).
    """

    def _render(self) -> str:
        return render_companion_contract(
            ticket_id="OMN-14425",
            repo="OmniNode-ai/omnimarket",
            pr_number=1721,
            evidence_id="dod-OmniNode-ai-omnimarket-pr-1721",
        )

    def test_declares_two_dod_evidence_items(self) -> None:
        rendered = self._render()
        assert rendered.count("- id: ") == 2

    def test_existence_probe_item_is_preserved_verbatim(self) -> None:
        # OMN-14425 ADDS a claim; it must not remove or alter the binding probe
        # the Evidence-Source autobind stamp path depends on.
        rendered = self._render()
        assert (
            "gh pr view 1721 --repo OmniNode-ai/omnimarket --json number,state"
            in rendered
        )

    def test_second_item_check_value_is_not_an_existence_probe(self) -> None:
        # OMN-14650: the second item is a product-diff-scope assertion (`gh pr
        # diff ... --name-only | grep -q .`), NOT a source-CI-green probe. It
        # still clears the OMN-14409 substance floor (static-assert family) and
        # is not an existence probe.
        rendered = self._render()
        ci_check_value = (
            "gh pr diff 1721 --repo OmniNode-ai/omnimarket --name-only | grep -q ."
        )
        assert ci_check_value in rendered
        assert not _is_existence_probe_like_omn_14409(ci_check_value)
        # Regression: the former `gh pr checks <source>` CI-green gate is gone.
        assert "gh pr checks" not in rendered

    def test_second_item_id_derived_from_base_evidence_id(self) -> None:
        rendered = self._render()
        expected_id = ci_check_evidence_id("dod-OmniNode-ai-omnimarket-pr-1721")
        assert f'id: "{expected_id}"' in rendered

    def test_old_single_item_shape_was_all_existence_red_case(self) -> None:
        """Pin the defective shape OMN-14425 fixes, so this test regresses loudly
        if the fix is ever reverted. This is exactly contracts/OMN-14400.yaml /
        OMN-14411.yaml as minted before this fix — every declared check matches
        the existence-probe pattern."""
        old_shape_only_check = (
            "gh pr view 1721 --repo OmniNode-ai/omnimarket --json number,state"
        )
        assert _is_existence_probe_like_omn_14409(old_shape_only_check)


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

    def test_renders_contract_entry_sha256_pending_sentinel(self) -> None:
        # OMN-14418 residual 3: the downstream receipt corresponds to a
        # declared dod_evidence item, so it MUST carry a rebindable
        # contract_entry_sha256 sentinel alongside the legacy whole-file one.
        rendered = self._render()
        assert 'contract_entry_sha256: "sha256:PENDING"' in rendered

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

    def test_renders_contract_entry_sha256_pending_sentinel(self) -> None:
        # OMN-14650: the self-bind receipt's evidence_item_id
        # ("occ-self-bind-pr-<n>") is now APPENDED to the companion contract's
        # dod_evidence as a declared item, so it MUST carry a rebindable
        # contract_entry_sha256 sentinel (the per-entry scheme the proven merged
        # path uses) alongside the whole-file contract_sha256. Before OMN-14650
        # this field was deliberately omitted, which is exactly why every auto/*
        # companion failed eligibility with pr_ticket_mismatch.
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
        assert 'contract_entry_sha256: "sha256:PENDING"' in rendered
        assert 'contract_sha256: "sha256:PENDING"' in rendered


@pytest.mark.unit
class TestSelfBindDodEvidenceItemRender:
    def test_renders_declared_self_bind_contract_item(self) -> None:
        rendered = render_self_bind_dod_evidence_item(
            evidence_id="occ-self-bind-pr-42",
            occ_pr_number=42,
            occ_repo="OmniNode-ai/onex_change_control",
            ticket_id="OMN-9999",
        )

        assert rendered.startswith('  - id: "occ-self-bind-pr-42"\n')
        assert "OCC companion PR #42" in rendered
        assert (
            'check_value: "gh pr view 42 --repo OmniNode-ai/onex_change_control '
            '--json number,state"' in rendered
        )
        assert _NAMED_PLACEHOLDER_RE.findall(rendered) == []


@pytest.mark.unit
class TestCiCheckReceiptRender:
    """OMN-14425: the receipt backing the new substantive dod_evidence item."""

    def _render(self) -> str:
        return render_ci_check_receipt(
            ticket_id="OMN-9999",
            evidence_id=ci_check_evidence_id("dod-OmniNode-ai-omnimarket-pr-123"),
            pr_number=123,
            repo="OmniNode-ai/omnimarket",
            run_timestamp="2026-07-10T00:00:00Z",
            commit_sha="abc1234",
            branch="auto/omninode-ai-omnimarket-pr-123-occ-autobind",
            probe_command="gh pr diff 123 --repo OmniNode-ai/omnimarket --name-only",
            probe_stdout='{"number":123,"note":"diff not observed"}',
            exit_code=0,
        )

    def test_renders_required_receipt_fields(self) -> None:
        rendered = self._render()
        assert 'ticket_id: "OMN-9999"' in rendered
        assert 'evidence_item_id: "dod-OmniNode-ai-omnimarket-pr-123-ci"' in rendered
        assert "status: PASS" in rendered
        assert "pr_number: 123" in rendered
        assert 'commit_sha: "abc1234"' in rendered
        # OMN-14650: the declared check is a product-diff-scope assertion, not the
        # former source-CI-green `gh pr checks` probe.
        assert (
            'check_value: "gh pr diff 123 --repo OmniNode-ai/omnimarket '
            '--name-only | grep -q ."' in rendered
        )
        assert "gh pr checks" not in rendered
        assert 'contract_sha256: "sha256:PENDING"' in rendered

    def test_no_unsubstituted_named_placeholders(self) -> None:
        assert _NAMED_PLACEHOLDER_RE.findall(self._render()) == []

    def test_verifier_defaults_differ_from_runner(self) -> None:
        rendered = self._render()
        runner = re.search(r'runner:\s*"([^"]+)"', rendered)
        verifier = re.search(r'verifier:\s*"([^"]+)"', rendered)
        assert runner is not None
        assert verifier is not None
        assert runner.group(1) != verifier.group(1)


@pytest.mark.unit
class TestCiCheckEvidenceId:
    def test_derives_from_base_id_with_ci_suffix(self) -> None:
        assert ci_check_evidence_id("dod-x-pr-1") == "dod-x-pr-1-ci"

    def test_deterministic(self) -> None:
        assert ci_check_evidence_id("dod-a") == ci_check_evidence_id("dod-a")

    def test_distinct_ids_stay_distinct(self) -> None:
        assert ci_check_evidence_id("dod-a") != ci_check_evidence_id("dod-b")


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
class TestContractEntrySha256Rebind:
    """OMN-13888 / OMN-14418 residual 3: per-entry rebind helpers."""

    def test_rebind_sets_prefixed_line_verbatim(self) -> None:
        receipt = 'x: 1\ncontract_entry_sha256: "sha256:PENDING"\ny: 2\n'
        prefixed = f"sha256:{'a' * 64}"
        rebound = rebind_contract_entry_sha256_in_text(receipt, prefixed)
        assert f'contract_entry_sha256: "{prefixed}"' in rebound
        assert "PENDING" not in rebound
        # Only the contract_entry_sha256 line changed.
        assert "x: 1" in rebound
        assert "y: 2" in rebound

    def test_rebind_does_not_touch_sibling_contract_sha256_line(self) -> None:
        # The two fields must rebind independently — a receipt carries both.
        receipt = (
            'contract_sha256: "sha256:PENDING"\n'
            'contract_entry_sha256: "sha256:PENDING"\n'
        )
        prefixed = f"sha256:{'c' * 64}"
        rebound = rebind_contract_entry_sha256_in_text(receipt, prefixed)
        assert 'contract_sha256: "sha256:PENDING"' in rebound  # untouched
        assert f'contract_entry_sha256: "{prefixed}"' in rebound

    def test_rebind_is_idempotent_fixpoint(self) -> None:
        prefixed = f"sha256:{'d' * 64}"
        once = rebind_contract_entry_sha256_in_text(
            'contract_entry_sha256: "sha256:PENDING"\n', prefixed
        )
        twice = rebind_contract_entry_sha256_in_text(once, prefixed)
        assert once == twice

    def test_rebind_is_noop_when_field_absent(self) -> None:
        # Legacy receipts may still lack the per-entry field — rebinding must
        # leave that text byte-for-byte unchanged, never fabricate the line.
        receipt = 'ticket_id: "OMN-9999"\ncontract_sha256: "sha256:PENDING"\n'
        rebound = rebind_contract_entry_sha256_in_text(receipt, f"sha256:{'e' * 64}")
        assert rebound == receipt
        assert "contract_entry_sha256" not in rebound

    def test_extract_evidence_item_id_finds_declared_id(self) -> None:
        text = 'ticket_id: "OMN-9999"\nevidence_item_id: "dod-x-pr-1"\ncheck_type: "command"\n'
        assert extract_evidence_item_id(text) == "dod-x-pr-1"

    def test_extract_evidence_item_id_none_when_absent(self) -> None:
        assert extract_evidence_item_id('ticket_id: "OMN-9999"\n') is None


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
