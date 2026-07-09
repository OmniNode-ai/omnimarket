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
            probe_command=(
                "gh pr view 2043 --repo OmniNode-ai/omnibase_infra "
                "--json number,state,headRefName"
            ),
            probe_stdout='{"headRefName":"feat","number":2043,"state":"open"}',
            exit_code=0,
        )
        # check_receipt_hardening: pr_number >= 1, commit_sha 7-40 hex.
        assert "pr_number: 2043" in rendered
        assert "040eb235aaaaaaaa" in rendered
        assert "status: PASS" in rendered
        assert "exit_code: 0" in rendered
        # Genuine probe output, not a fabricated template (OMN-13990 item 4).
        assert '{"headRefName":"feat","number":2043,"state":"open"}' in rendered
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
            probe_command=(
                "gh pr view 2801 --repo OmniNode-ai/onex_change_control "
                "--json number,state"
            ),
            probe_stdout='{"number":2801,"state":"open"}',
            exit_code=0,
        )
        assert "pr_number: 2801" in rendered
        assert "d97d5db9bbbbbbbb" in rendered
        assert "exit_code: 0" in rendered
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

        def fake_rest(method, path, *, body=None, token=None):  # type: ignore[no-untyped-def]
            if method == "GET":  # default-branch resolution
                return {"default_branch": "dev"}
            return {"number": 2900}

        with (
            patch.object(adapter, "_first_open_pr_number", return_value=None),
            patch(f"{_MODULE}.rest_json", side_effect=fake_rest) as mock_rest,
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
        ):
            result = adapter._open_or_sync_occ_pr(
                branch="auto/omn-9999-occ-autobind",
                ticket="OMN-9999",
                repo="OmniNode-ai/omnibase_infra",
                pr_number=2043,
            )

        assert result == 2900
        # Last call is the POST create, based on OCC's DEFAULT branch (dev),
        # NOT a hardcoded "main" (OMN-13990 base-branch fix).
        assert mock_rest.call_args[0][0] == "POST"
        assert mock_rest.call_args.kwargs["body"]["base"] == "dev"

    def test_occ_default_branch_resolves_from_repo(self) -> None:
        from unittest.mock import patch

        with patch(
            f"{_MODULE}.rest_json", return_value={"default_branch": "dev"}
        ) as mock_rest:
            base = OccAutobindAdapter._occ_default_branch(
                "OmniNode-ai", "onex_change_control", "tok"
            )

        assert base == "dev"
        assert mock_rest.call_args[0][0] == "GET"


# ---------------------------------------------------------------------------
# Re-fire (synchronize) safety: pushes must be --force (OMN-13990 / CodeRabbit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutobindForcePush:
    def test_pushes_use_force_for_refire_safety(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        git_calls: list[list[str]] = []

        def fake_run_git(argv, *, cwd):  # type: ignore[no-untyped-def]
            git_calls.append(argv)
            return ""

        def fake_rest(method, path, *, body=None, token=None):  # type: ignore[no-untyped-def]
            if "onex_change_control" in path:  # OCC PR probe fetch
                return {"state": "open", "number": 3658}
            return {  # product PR snapshot: born without an OCC Evidence-Source
                "body": "PR body, no evidence yet",
                "title": "fix(OMN-1): thing",
                "head": {"sha": "a" * 40, "ref": "feat"},
                "state": "open",
            }

        with (
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
            patch(f"{_MODULE}.rest_json", side_effect=fake_rest),
            patch.object(adapter, "_clone_and_branch"),
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_rebind_contract_sha256"),
            patch.object(adapter, "_open_or_sync_occ_pr", return_value=3658),
            patch.object(adapter, "_head_sha", return_value="occhead0"),
            patch.object(adapter, "_observe_pr_probe", return_value=("{}", 0)),
            patch.object(adapter, "_patch_evidence_source"),
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            result = adapter._autobind_sync("OmniNode-ai/omnibase_infra", 2043, None)

        push_calls = [c for c in git_calls if "push" in c]
        assert push_calls, "expected at least one git push"
        for call in push_calls:
            assert "--force" in call, (
                f"OCC branch push must be --force for synchronize re-fire "
                f"safety (non-fast-forward otherwise): {call}"
            )
        assert "OCC#3658" in result


# ---------------------------------------------------------------------------
# Create-if-absent: a genuinely FRESH ticket (no pre-existing OCC contract)
# MINTS a net-new companion (OMN-14173 — the merge_sweep --fix-only residual).
#
# The prior end-to-end coverage (TestAutobindForcePush) forced is_file=True (the
# SYNC path), so nothing locked the CREATE path. That left the "OccAutobindAdapter
# only syncs an existing contract and no-ops for a fresh ticket" hypothesis
# unfalsified. These tests exercise both branches and assert the EFFECT — a
# net-new contract WRITTEN, the auto/* companion branch PUSHED (force), and
# Evidence-Source PATCHed — never merely that the adapter call returned. Reverting
# the `if not contract_path.is_file()` create guard makes the fresh-ticket test
# fail (no contract written), which is the regression lock the residual needed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutobindCreatesContractForFreshTicket:
    @staticmethod
    def _run_autobind(
        *, contract_exists: bool
    ) -> tuple[dict[str, str], list[list[str]], dict, str]:
        """Drive ``_autobind_sync`` with every I/O seam mocked.

        Returns ``(writes_by_path, git_calls, patch_evidence_kwargs, result)``.
        ``contract_exists`` toggles ``Path.is_file``: ``False`` = fresh ticket
        (create path), ``True`` = existing contract (sync path).
        """
        from pathlib import Path
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        git_calls: list[list[str]] = []
        writes: dict[str, str] = {}
        patch_kwargs: dict = {}

        def fake_run_git(argv, *, cwd):  # type: ignore[no-untyped-def]
            git_calls.append(argv)
            return ""

        def fake_rest(method, path, *, body=None, token=None):  # type: ignore[no-untyped-def]
            if "onex_change_control" in path:  # OCC PR probe fetch
                return {"state": "open", "number": 3658}
            return {  # product PR: born WITHOUT an OCC Evidence-Source
                "body": "PR body, no evidence yet",
                "title": "feat(OMN-99999): brand new ticket, no OCC contract",
                "head": {"sha": "a" * 40, "ref": "feat"},
                "state": "open",
            }

        def capture_write(self, data, *args, **kwargs):  # type: ignore[no-untyped-def]
            writes[str(self)] = data
            return

        def fake_patch_evidence(**kwargs):  # type: ignore[no-untyped-def]
            patch_kwargs.update(kwargs)

        with (
            patch(f"{_MODULE}._resolve_github_token", return_value="tok"),
            patch(f"{_MODULE}.rest_json", side_effect=fake_rest),
            patch.object(adapter, "_clone_and_branch"),
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_rebind_contract_sha256"),
            patch.object(adapter, "_open_or_sync_occ_pr", return_value=3658),
            patch.object(adapter, "_head_sha", return_value="occhead0"),
            patch.object(adapter, "_observe_pr_probe", return_value=("{}", 0)),
            patch.object(
                adapter, "_patch_evidence_source", side_effect=fake_patch_evidence
            ),
            patch.object(Path, "is_file", return_value=contract_exists),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text", new=capture_write),
        ):
            result = adapter._autobind_sync("OmniNode-ai/omnimarket", 1653, None)

        return writes, git_calls, patch_kwargs, result

    def test_fresh_ticket_mints_net_new_contract_and_pushes(self) -> None:
        """FRESH ticket (no contract) → CREATE a net-new companion and push it.

        This is the exact scenario the residual named: ``--fix-only`` must MINT
        for a first-time ticket, not no-op. Verifies the mint EFFECT.
        """
        writes, git_calls, patch_kwargs, result = self._run_autobind(
            contract_exists=False
        )

        # EFFECT 1 — a net-new contracts/<ticket>.yaml is CREATED (not synced).
        contract_writes = {
            path: content
            for path, content in writes.items()
            if path.endswith("contracts/OMN-99999.yaml")
        }
        assert contract_writes, (
            "a fresh ticket with NO pre-existing OCC contract MUST create "
            f"contracts/OMN-99999.yaml; paths written: {sorted(writes)}"
        )
        (contract_content,) = contract_writes.values()
        assert 'ticket_id: "OMN-99999"' in contract_content
        assert "dod_evidence:" in contract_content

        # EFFECT 2 — the auto/* companion branch is force-pushed.
        push_calls = [call for call in git_calls if "push" in call]
        assert push_calls, "the companion branch must be pushed for a fresh ticket"
        for call in push_calls:
            assert "--force" in call, f"companion push must be --force: {call}"

        # EFFECT 3 — Evidence-Source: OCC#<n> is patched back onto the product PR.
        assert patch_kwargs.get("occ_pr_number") == 3658
        assert "OCC#3658" in result

    def test_existing_contract_syncs_without_recreating_contract(self) -> None:
        """EXISTING contract → SYNC (bind receipts) WITHOUT rewriting the contract.

        OMN-13888 whole-file-hash defect: an existing OCC contract must never be
        modified. The sync path still binds the companion (receipts + branch push
        + Evidence-Source patch), it just does not touch the contract file.
        """
        writes, git_calls, patch_kwargs, result = self._run_autobind(
            contract_exists=True
        )

        contract_writes = [
            path for path in writes if path.endswith("contracts/OMN-99999.yaml")
        ]
        assert not contract_writes, (
            "an existing OCC contract must NOT be recreated/modified on the sync "
            f"path (OMN-13888 whole-file-hash); contract writes seen: {contract_writes}"
        )
        # The companion is still bound: receipts written + branch pushed + patched.
        assert any("dod_receipts" in path for path in writes), (
            "downstream/self-bind receipts must still be written on the sync path"
        )
        push_calls = [call for call in git_calls if "push" in call]
        assert push_calls, "the companion branch must still be pushed on the sync path"
        for call in push_calls:
            assert "--force" in call, f"companion push must be --force: {call}"
        assert patch_kwargs.get("occ_pr_number") == 3658
        assert "OCC#3658" in result


# ---------------------------------------------------------------------------
# Validator-parity ticket extraction (OMN-13990 D3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractTickets:
    def test_single_title_token(self) -> None:
        assert OccAutobindAdapter._extract_tickets("fix(OMN-1234): x", "body") == [
            "OMN-1234"
        ]

    def test_all_title_tokens_when_no_closing_keyword(self) -> None:
        assert OccAutobindAdapter._extract_tickets(
            "fix(OMN-1)(OMN-2): x", "no closing keyword here"
        ) == ["OMN-1", "OMN-2"]

    def test_body_closing_keyword_is_exclusive(self) -> None:
        # Gate parity: a body closing-keyword wins over ALL title tokens.
        assert OccAutobindAdapter._extract_tickets("fix(OMN-9): x", "Closes OMN-5") == [
            "OMN-5"
        ]

    def test_returns_empty_when_none(self) -> None:
        assert OccAutobindAdapter._extract_tickets("no ticket", "still none") == []


# ---------------------------------------------------------------------------
# Genuine probe execution (OMN-13990 item 4 / OMN-14055)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestObservePrProbe:
    def test_uses_real_gh_stdout_when_available(self) -> None:
        from unittest.mock import MagicMock, patch

        adapter = OccAutobindAdapter()
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = '{"state": "open", "number": 5}'

        with patch(f"{_MODULE}.subprocess.run", return_value=completed):
            stdout, exit_code = adapter._observe_pr_probe(
                probe_command="gh pr view 5 --repo o/r --json number,state",
                token="tok",
                fallback={"number": 5, "state": "unknown"},
            )

        # Real gh output, re-serialised compact + sorted (single YAML-safe line).
        assert stdout == '{"number":5,"state":"open"}'
        assert exit_code == 0

    def test_falls_back_to_rest_data_when_gh_missing(self) -> None:
        from unittest.mock import patch

        adapter = OccAutobindAdapter()
        with patch(f"{_MODULE}.subprocess.run", side_effect=FileNotFoundError):
            stdout, exit_code = adapter._observe_pr_probe(
                probe_command="gh pr view 5 --repo o/r --json number,state",
                token="tok",
                fallback={"number": 5, "state": "open"},
            )

        # Genuine REST-observed facts, never a fabricated template.
        assert stdout == '{"number":5,"state":"open"}'
        assert exit_code == 0

    def test_falls_back_on_nonzero_exit(self) -> None:
        from unittest.mock import MagicMock, patch

        adapter = OccAutobindAdapter()
        failed = MagicMock()
        failed.returncode = 1
        failed.stdout = ""

        with patch(f"{_MODULE}.subprocess.run", return_value=failed):
            stdout, exit_code = adapter._observe_pr_probe(
                probe_command="gh pr view 5 --repo o/r --json number,state",
                token="tok",
                fallback={"number": 5, "state": "closed"},
            )

        assert stdout == '{"number":5,"state":"closed"}'
        assert exit_code == 0
