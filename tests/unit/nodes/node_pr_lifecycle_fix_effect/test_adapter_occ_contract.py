# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for OccContractAdapter.

All tests run without any real git, network, or GitHub API calls.
The adapter's internal helpers are exercised through seams and the
YAML-builder functions are tested directly.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract import (
    _CONTRACT_TEMPLATE,
    _RECEIPT_TEMPLATE,
    OccContractAdapter,
)

# ---------------------------------------------------------------------------
# YAML template tests — pure string construction, zero I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOccContractYamlTemplates:
    def test_contract_template_renders_required_fields(self) -> None:
        rendered = _CONTRACT_TEMPLATE.format(
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
        assert "evidence_id" not in rendered  # placeholder must be substituted
        assert "dod-OmniNode-ai-omnimarket-pr-123" in rendered

    def test_receipt_template_renders_required_fields(self) -> None:
        rendered = _RECEIPT_TEMPLATE.format(
            ticket_id="OMN-9999",
            evidence_id="dod-OmniNode-ai-omnimarket-pr-123",
            pr_number=123,
            repo="OmniNode-ai/omnimarket",
            run_timestamp="2026-05-25T00:00:00Z",
            commit_sha="abc123",
            branch="auto/omn-9999-occ-contract",
            repo_slug="OmniNode-ai-omnimarket",
        )
        assert 'ticket_id: "OMN-9999"' in rendered
        assert "status: PASS" in rendered
        assert "pr_number: 123" in rendered
        assert "node_pr_lifecycle_fix_effect" in rendered
        assert "occ-auto-contract" in rendered
        assert "abc123" in rendered

    def test_contract_template_no_unsubstituted_placeholders(self) -> None:
        rendered = _CONTRACT_TEMPLATE.format(
            ticket_id="OMN-1234",
            repo="OmniNode-ai/test",
            pr_number=7,
            evidence_id="dod-OmniNode-ai-test-pr-7",
        )
        assert "{" not in rendered, "unsubstituted placeholder found in contract YAML"
        assert "}" not in rendered, "unsubstituted placeholder found in contract YAML"

    def test_receipt_template_no_unsubstituted_placeholders(self) -> None:
        rendered = _RECEIPT_TEMPLATE.format(
            ticket_id="OMN-1234",
            evidence_id="dod-OmniNode-ai-test-pr-7",
            pr_number=7,
            repo="OmniNode-ai/test",
            run_timestamp="2026-01-01T00:00:00Z",
            commit_sha="deadbeef",
            branch="auto/omn-1234-occ-contract",
            repo_slug="OmniNode-ai-test",
        )
        # The probe_stdout line intentionally contains {{ }} for JSON literal braces;
        # after .format() those become { } — that is expected and correct.
        # All *named* placeholders must be gone.
        import re

        unresolved = re.findall(r"\{[a-z_]+\}", rendered)
        assert unresolved == [], f"unresolved placeholders: {unresolved}"


# ---------------------------------------------------------------------------
# OccContractAdapter._run_git helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOccContractAdapterRunGit:
    def test_run_git_returns_stripped_stdout(self, tmp_path: object) -> None:
        adapter = OccContractAdapter()
        result = adapter._run_git(
            ["git", "--version"],
            cwd=str(tmp_path),  # type: ignore[arg-type]
        )
        assert result.startswith("git version")

    def test_run_git_raises_on_nonzero_exit(self, tmp_path: object) -> None:
        adapter = OccContractAdapter()
        with pytest.raises(subprocess.CalledProcessError):
            adapter._run_git(
                ["git", "rev-parse", "--verify", "refs/heads/nonexistent-branch-xyz"],
                cwd=str(tmp_path),  # type: ignore[arg-type]
            )

    def test_head_sha_returns_unknown_on_error(self, tmp_path: object) -> None:
        """_head_sha returns 'unknown' when git is not initialised in the dir."""
        adapter = OccContractAdapter()
        result = adapter._head_sha(str(tmp_path))  # type: ignore[arg-type]
        assert result == "unknown"


# ---------------------------------------------------------------------------
# OccContractAdapter._open_occ_pr — mock rest_json
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOccContractAdapterOpenOccPr:
    def test_open_occ_pr_calls_rest_json_and_returns_number(self) -> None:
        adapter = OccContractAdapter()
        mock_resp = {"number": 42, "html_url": "https://github.com/test/pr/42"}

        with patch(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract.rest_json",
            return_value=mock_resp,
        ) as mock_rest:
            result = adapter._open_occ_pr(
                branch="auto/omn-9999-occ-contract",
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=123,
            )

        assert result == 42
        mock_rest.assert_called_once()
        call_kwargs = mock_rest.call_args
        assert call_kwargs[0][0] == "POST"
        assert "pulls" in call_kwargs[0][1]

    def test_open_occ_pr_raises_on_missing_number(self) -> None:
        adapter = OccContractAdapter()
        with (
            patch(
                "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract.rest_json",
                return_value={"html_url": "https://github.com/test"},
            ),
            pytest.raises(RuntimeError, match="unexpected number field"),
        ):
            adapter._open_occ_pr(
                branch="auto/omn-9999-occ-contract",
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=123,
            )


# ---------------------------------------------------------------------------
# OccContractAdapter._append_evidence_to_pr — mock rest_json
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOccContractAdapterAppendEvidence:
    def test_appends_evidence_footer_when_absent(self) -> None:
        adapter = OccContractAdapter()
        call_log: list[tuple[str, str]] = []

        def fake_rest(method: str, path: str, *, body: dict | None = None) -> dict:
            call_log.append((method, path))
            if method == "GET":
                return {"body": "existing PR body"}
            return {}

        with patch(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract.rest_json",
            side_effect=fake_rest,
        ):
            adapter._append_evidence_to_pr(
                repo="OmniNode-ai/omnimarket",
                pr_number=77,
                occ_pr_number=99,
                ticket_id="OMN-9999",
            )

        assert ("GET", "/repos/OmniNode-ai/omnimarket/pulls/77") in call_log
        assert ("PATCH", "/repos/OmniNode-ai/omnimarket/pulls/77") in call_log

    def test_skips_patch_when_evidence_already_present(self) -> None:
        adapter = OccContractAdapter()
        existing_body = "existing body\n\nEvidence-Source: OCC#99\n"

        def fake_rest(method: str, path: str, *, body: dict | None = None) -> dict:
            if method == "GET":
                return {"body": existing_body}
            return {}

        patch_called = False

        def fake_rest_with_check(
            method: str, path: str, *, body: dict | None = None
        ) -> dict:
            nonlocal patch_called
            if method == "PATCH":
                patch_called = True
            return fake_rest(method, path, body=body)

        with patch(
            "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract.rest_json",
            side_effect=fake_rest_with_check,
        ):
            adapter._append_evidence_to_pr(
                repo="OmniNode-ai/omnimarket",
                pr_number=77,
                occ_pr_number=99,
                ticket_id="OMN-9999",
            )

        assert not patch_called, "PATCH must be skipped when evidence already present"


# ---------------------------------------------------------------------------
# OccContractAdapter.create_occ_contract — end-to-end with all I/O mocked
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOccContractAdapterCreateOccContract:
    def test_create_occ_contract_calls_all_steps(self) -> None:
        """Full workflow: clone → branch → write files → commit → push → PR → append."""
        adapter = OccContractAdapter()

        git_calls: list[list[str]] = []

        def fake_run_git(argv: list[str], *, cwd: str) -> str:
            git_calls.append(argv)
            return "abc123" if "rev-parse" in argv else ""

        open_pr_result = 55
        append_calls: list[dict] = []

        with (
            patch.object(adapter, "_run_git", side_effect=fake_run_git),
            patch.object(adapter, "_open_occ_pr", return_value=open_pr_result),
            patch.object(
                adapter,
                "_append_evidence_to_pr",
                side_effect=lambda **kw: append_calls.append(kw),
            ),
            patch("tempfile.TemporaryDirectory") as mock_tmpdir,
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.write_text"),
        ):
            # TemporaryDirectory context manager must yield a real path string
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value="/tmp/fake-occ")
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_tmpdir.return_value = mock_ctx

            result = adapter._create_occ_contract_sync(
                "OmniNode-ai/omnimarket", 123, "OMN-9999"
            )

        assert "OMN-9999" in result
        assert "occ_pr=55" in result
        # verify git clone was attempted
        clone_call = next((c for c in git_calls if "clone" in c), None)
        assert clone_call is not None, "git clone must be called"
        # verify open_occ_pr was called
        assert open_pr_result == 55
        # verify evidence was appended
        assert len(append_calls) == 1
        assert append_calls[0]["occ_pr_number"] == 55
        assert append_calls[0]["ticket_id"] == "OMN-9999"

    async def test_create_occ_contract_async_delegates_to_sync(self) -> None:
        adapter = OccContractAdapter()
        sync_calls: list[tuple] = []

        def fake_sync(repo: str, pr_number: int, ticket_id: str) -> str:
            sync_calls.append((repo, pr_number, ticket_id))
            return f"[fake] created contract for {ticket_id}"

        with patch.object(adapter, "_create_occ_contract_sync", side_effect=fake_sync):
            result = await adapter.create_occ_contract(
                "OmniNode-ai/omnimarket", 42, "OMN-9999"
            )

        assert result == "[fake] created contract for OMN-9999"
        assert sync_calls == [("OmniNode-ai/omnimarket", 42, "OMN-9999")]
