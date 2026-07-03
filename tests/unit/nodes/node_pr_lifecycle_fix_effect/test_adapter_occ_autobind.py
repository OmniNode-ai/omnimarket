# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for OccAutobindAdapter (OMN-13317 F1).

All tests run without any real git, network, or GitHub API calls. The adapter's
internal helpers are exercised through seams and the YAML-builder templates are
tested directly.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_autobind import (
    _CONTRACT_TEMPLATE,
    _DOWNSTREAM_RECEIPT_TEMPLATE,
    _SELF_BIND_RECEIPT_TEMPLATE,
    OccAutobindAdapter,
)

_MODULE = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_autobind"


# ---------------------------------------------------------------------------
# YAML template tests — pure string construction, zero I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutobindYamlTemplates:
    def test_contract_template_renders_required_fields(self) -> None:
        rendered = _CONTRACT_TEMPLATE.format(
            ticket_id="OMN-9999",
            repo="OmniNode-ai/omnibase_infra",
            pr_number=2043,
            evidence_id="dod-OmniNode-ai-omnibase_infra-pr-2043",
        )
        assert 'ticket_id: "OMN-9999"' in rendered
        assert "is_seam_ticket: false" in rendered
        assert "dod_evidence:" in rendered
        unresolved = re.findall(r"\{[a-z_]+\}", rendered)
        assert unresolved == [], f"unresolved placeholders: {unresolved}"

    def test_downstream_receipt_stamps_real_product_head(self) -> None:
        rendered = _DOWNSTREAM_RECEIPT_TEMPLATE.format(
            ticket_id="OMN-9999",
            evidence_id="dod-OmniNode-ai-omnibase_infra-pr-2043",
            pr_number=2043,
            repo="OmniNode-ai/omnibase_infra",
            run_timestamp="2026-06-19T12:00:00Z",
            commit_sha="040eb235aaaaaaaa",
            branch="auto/omn-9999-occ-autobind",
        )
        # check_receipt_hardening: pr_number >= 1, commit_sha 7-40 hex.
        assert "pr_number: 2043" in rendered
        assert "040eb235aaaaaaaa" in rendered
        assert "status: PASS" in rendered
        # verifier is identifiable, not a denylisted session-local alias.
        assert "occ-evidence-source-autobind" in rendered
        assert 'contract_sha256: "sha256:PENDING"' in rendered
        unresolved = re.findall(r"\{[a-z_]+\}", rendered)
        assert unresolved == [], f"unresolved placeholders: {unresolved}"

    def test_self_bind_receipt_uses_real_occ_pr_number(self) -> None:
        rendered = _SELF_BIND_RECEIPT_TEMPLATE.format(
            ticket_id="OMN-9999",
            evidence_id="occ-self-bind-pr-2801",
            occ_pr_number=2801,
            occ_repo="OmniNode-ai/onex_change_control",
            run_timestamp="2026-06-19T12:00:00Z",
            occ_commit_sha="d97d5db9bbbbbbbb",
            branch="auto/omn-9999-occ-autobind",
        )
        assert "pr_number: 2801" in rendered
        assert "d97d5db9bbbbbbbb" in rendered
        unresolved = re.findall(r"\{[a-z_]+\}", rendered)
        assert unresolved == [], f"unresolved placeholders: {unresolved}"


# ---------------------------------------------------------------------------
# Ticket detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectTicket:
    def test_detects_from_title(self) -> None:
        assert (
            OccAutobindAdapter._detect_ticket(
                "fix(OMN-1234): something", "no ticket here"
            )
            == "OMN-1234"
        )

    def test_falls_back_to_body(self) -> None:
        assert (
            OccAutobindAdapter._detect_ticket("no ticket", "cites OMN-5678 in body")
            == "OMN-5678"
        )

    def test_returns_none_when_absent(self) -> None:
        assert OccAutobindAdapter._detect_ticket("no ticket", "still none") is None


# ---------------------------------------------------------------------------
# contract_sha256 rebinding across ALL matching receipts (friction #9)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRebindContractSha256:
    def test_rebinds_every_matching_receipt(self, tmp_path: object) -> None:
        from pathlib import Path

        clone = Path(str(tmp_path))  # type: ignore[arg-type]
        contract = clone / "contracts" / "OMN-9999.yaml"
        contract.parent.mkdir(parents=True)
        contract.write_bytes(b"contract body bytes\n")
        expected = hashlib.sha256(contract.read_bytes()).hexdigest()

        receipt_root = clone / "drift" / "dod_receipts" / "OMN-9999"
        a = receipt_root / "dod-a" / "command.yaml"
        b = receipt_root / "occ-self-bind-pr-1" / "command.yaml"
        for r in (a, b):
            r.parent.mkdir(parents=True, exist_ok=True)
            r.write_text(
                'schema_version: "1.0.0"\ncontract_sha256: "sha256:PENDING"\n',
                encoding="utf-8",
            )

        adapter = OccAutobindAdapter()
        adapter._rebind_contract_sha256(clone, "OMN-9999", contract)

        for r in (a, b):
            assert f'contract_sha256: "sha256:{expected}"' in r.read_text()

    def test_rebinds_stale_hash_to_current(self, tmp_path: object) -> None:
        from pathlib import Path

        clone = Path(str(tmp_path))  # type: ignore[arg-type]
        contract = clone / "contracts" / "OMN-1.yaml"
        contract.parent.mkdir(parents=True)
        contract.write_bytes(b"v2\n")
        expected = hashlib.sha256(contract.read_bytes()).hexdigest()
        stale = "a" * 64
        receipt = clone / "drift" / "dod_receipts" / "OMN-1" / "x" / "command.yaml"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(f'contract_sha256: "sha256:{stale}"\n', encoding="utf-8")

        OccAutobindAdapter()._rebind_contract_sha256(clone, "OMN-1", contract)
        assert f'contract_sha256: "sha256:{expected}"' in receipt.read_text()


# ---------------------------------------------------------------------------
# Evidence-Source PATCH semantics (friction #7 — REST PATCH, not gh pr edit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatchEvidenceSource:
    def _capture(self) -> tuple[list, object]:
        calls: list = []

        def fake_rest(method, path, *, body=None, token=None):  # type: ignore[no-untyped-def]
            calls.append({"method": method, "path": path, "body": body})
            return {}

        return calls, fake_rest

    def test_rewrites_product_sha_source_to_occ(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        calls, fake_rest = self._capture()
        body = "title\n\nEvidence-Source: 040eb235abcdef\nEvidence-Ticket: OMN-9999\n"

        with (
            patch(f"{_MODULE}.rest_json", side_effect=fake_rest),
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            adapter._patch_evidence_source(
                repo="OmniNode-ai/omnibase_infra",
                pr_number=2043,
                occ_pr_number=2801,
                ticket="OMN-9999",
                existing_body=body,
            )

        assert len(calls) == 1
        assert calls[0]["method"] == "PATCH"
        new_body = calls[0]["body"]["body"]
        assert "Evidence-Source: OCC#2801" in new_body
        assert "040eb235abcdef" not in new_body

    def test_appends_when_no_evidence_source_line(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        calls, fake_rest = self._capture()

        with (
            patch(f"{_MODULE}.rest_json", side_effect=fake_rest),
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            adapter._patch_evidence_source(
                repo="OmniNode-ai/omnibase_infra",
                pr_number=2043,
                occ_pr_number=2801,
                ticket="OMN-9999",
                existing_body="just a body",
            )

        assert len(calls) == 1
        assert "Evidence-Source: OCC#2801" in calls[0]["body"]["body"]

    def test_noop_when_already_correct(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        calls, fake_rest = self._capture()

        with (
            patch(f"{_MODULE}.rest_json", side_effect=fake_rest),
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            adapter._patch_evidence_source(
                repo="OmniNode-ai/omnibase_infra",
                pr_number=2043,
                occ_pr_number=2801,
                ticket="OMN-9999",
                existing_body="body\nEvidence-Source: OCC#2801\n",
            )

        assert calls == []


# ---------------------------------------------------------------------------
# Idempotency: already-bound product PR is a no-op end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutobindIdempotency:
    def test_already_occ_bound_returns_noop(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        pr_data = {
            "body": "body\nEvidence-Source: OCC#2801\n",
            "title": "fix(OMN-9999): x",
            "head": {"sha": "040eb235abcdef0123"},
        }

        with (
            patch(f"{_MODULE}.rest_json", return_value=pr_data) as mock_rest,
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            result = adapter._autobind_sync("OmniNode-ai/omnibase_infra", 2043, None)

        assert "no-op" in result
        # Only the initial GET happened — no clone, no PR, no PATCH.
        assert mock_rest.call_count == 1

    def test_raises_when_head_sha_unresolvable(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        pr_data = {"body": "x", "title": "fix(OMN-1): y", "head": {"sha": "nothex!!"}}

        with (
            patch(f"{_MODULE}.rest_json", return_value=pr_data),
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
            pytest.raises(RuntimeError, match="head SHA"),
        ):
            adapter._autobind_sync("OmniNode-ai/omnibase_infra", 2043, None)


# ---------------------------------------------------------------------------
# Open-or-sync OCC PR — reuses existing open PR for the branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenOrSyncOccPr:
    def test_returns_existing_pr_number_without_creating(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        with (
            patch.object(
                adapter, "_first_open_pr_number", return_value=2801
            ) as mock_find,
            patch(f"{_MODULE}.rest_json") as mock_rest,
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            result = adapter._open_or_sync_occ_pr(
                branch="auto/omn-9999-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnibase_infra",
                pr_number=2043,
            )

        assert result == 2801
        mock_find.assert_called_once()
        # No POST create call when an open PR already exists.
        mock_rest.assert_not_called()

    def test_creates_pr_when_none_exists(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        with (
            patch.object(adapter, "_first_open_pr_number", return_value=None),
            patch(f"{_MODULE}.rest_json", return_value={"number": 2900}) as mock_rest,
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            result = adapter._open_or_sync_occ_pr(
                branch="auto/omn-9999-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnibase_infra",
                pr_number=2043,
            )

        assert result == 2900
        assert mock_rest.call_args[0][0] == "POST"
