# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13853: wire the OCC_RECEIPT close-evidence kind into the close gate.

OMN-13817 defined ``EnumCloseEvidenceKind.OCC_RECEIPT`` but left it with zero
constructing call sites — the gate could never accept a tracked node_dod_verify
receipt as durable close evidence. These tests prove the wiring:

* the pure receipt validator accepts only a schema-valid PASS receipt bound to
  the ticket, and rejects missing / non-PASS / mismatched receipts (fail-closed);
* the git-backed probe positively verifies a tracked PASS receipt end-to-end
  against a real temp repo, and returns ``None`` for every failure mode;
* the handler constructs ``ModelCloseEvidence(kind=OCC_RECEIPT, ...)`` and closes
  the ticket through the ``_mark_done`` chokepoint; and
* the wf_1628d9a5 no-evidence batch (Done, startedAt=null, zero attachments,
  no tracked receipt) is REJECTED by the gate in a dry-run of ``handle()``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
    OccReceiptProbe,
    OccReceiptSubprocessProbe,
    _occ_receipt_dir,
    _parse_receipt_payload,
    _receipt_is_pass_for_ticket,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTicket,
    ModelLinearTriageStartCommand,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _pass_receipt(ticket_id: str = "OMN-9999") -> dict[str, object]:
    """A schema-valid PASS ModelDodReceipt-shaped payload."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "evidence_item_id": "dod-run",
        "check_type": "command",
        "status": "PASS",
        "run_timestamp": datetime.now(tz=UTC).isoformat(),
        "commit_sha": "a" * 40,
        "runner": "node-dod-verify",
        "verifier": "node-dod-verify-ci",
    }


def _ticket(
    identifier: str = "OMN-9999",
    *,
    state: str = "Backlog",
    branch_name: str = "",
    parent_id: str = "",
) -> ModelLinearTicket:
    return ModelLinearTicket(
        id=f"id-{identifier}",
        identifier=identifier,
        title=f"{identifier} work",
        state=state,
        updated_at="2026-07-01T18:35:39.782Z",
        branch_name=branch_name,
        parent_id=parent_id,
    )


def _make_issue(
    *,
    identifier: str,
    state: str = "Backlog",
    days_ago: int = 5,
) -> dict[str, Any]:
    updated_at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"{identifier} work",
        "state": {"name": state},
        "updatedAt": updated_at,
        "branchName": "",
        "parent": None,
        "labels": {"nodes": []},
    }


def _stub_linear_client(issues: list[dict[str, Any]]) -> LinearClientProtocol:
    client = MagicMock(spec=LinearClientProtocol)
    client.list_issues.return_value = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": issues,
            }
        }
    }
    # No children / history for the flat ticket sets these tests use.
    client.list_children.return_value = {
        "data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
    }
    client.list_issue_history.return_value = {
        "data": {"issue": {"history": {"nodes": []}}}
    }
    return client  # type: ignore[return-value]


def _stub_github_no_prs() -> GitHubClientProtocol:
    gh = MagicMock(spec=GitHubClientProtocol)
    gh.search_prs.return_value = []
    gh.search_prs_in_repo.return_value = []
    gh.list_prs_by_head.return_value = []
    gh.pr_closing_ticket_refs.return_value = []
    return gh  # type: ignore[return-value]


class _StubOccProbe:
    """Injectable :class:`OccReceiptProbe` returning a fixed per-ticket map."""

    def __init__(self, details: dict[str, str | None]) -> None:
        self._details = details

    def occ_receipt_detail(self, *, ticket_id: str) -> str | None:
        return self._details.get(ticket_id)


# ---------------------------------------------------------------------------
# Pure validator: _receipt_is_pass_for_ticket
# ---------------------------------------------------------------------------


class TestReceiptValidator:
    def test_pass_receipt_bound_to_ticket_is_accepted(self) -> None:
        assert _receipt_is_pass_for_ticket(_pass_receipt("OMN-9999"), "OMN-9999")

    def test_status_case_insensitive(self) -> None:
        payload = _pass_receipt()
        payload["status"] = "pass"
        assert _receipt_is_pass_for_ticket(payload, "OMN-9999")

    def test_ticket_id_case_insensitive(self) -> None:
        assert _receipt_is_pass_for_ticket(_pass_receipt("omn-9999"), "OMN-9999")

    def test_fail_status_rejected(self) -> None:
        payload = _pass_receipt()
        payload["status"] = "FAIL"
        assert not _receipt_is_pass_for_ticket(payload, "OMN-9999")

    def test_advisory_status_rejected(self) -> None:
        payload = _pass_receipt()
        payload["status"] = "ADVISORY"
        assert not _receipt_is_pass_for_ticket(payload, "OMN-9999")

    def test_missing_status_rejected(self) -> None:
        payload = _pass_receipt()
        del payload["status"]
        assert not _receipt_is_pass_for_ticket(payload, "OMN-9999")

    def test_missing_run_timestamp_rejected(self) -> None:
        payload = _pass_receipt()
        del payload["run_timestamp"]
        assert not _receipt_is_pass_for_ticket(payload, "OMN-9999")

    def test_blank_run_timestamp_rejected(self) -> None:
        payload = _pass_receipt()
        payload["run_timestamp"] = "   "
        assert not _receipt_is_pass_for_ticket(payload, "OMN-9999")

    def test_mismatched_ticket_rejected(self) -> None:
        assert not _receipt_is_pass_for_ticket(_pass_receipt("OMN-1111"), "OMN-9999")

    def test_missing_ticket_id_rejected(self) -> None:
        payload = _pass_receipt()
        del payload["ticket_id"]
        assert not _receipt_is_pass_for_ticket(payload, "OMN-9999")


# ---------------------------------------------------------------------------
# Pure parser: _parse_receipt_payload
# ---------------------------------------------------------------------------


class TestReceiptParser:
    def test_parses_yaml_mapping(self) -> None:
        raw = yaml.safe_dump(_pass_receipt())
        parsed = _parse_receipt_payload(raw)
        assert parsed is not None
        assert parsed["status"] == "PASS"

    def test_parses_json_mapping(self) -> None:
        raw = json.dumps(_pass_receipt())
        parsed = _parse_receipt_payload(raw)
        assert parsed is not None
        assert parsed["ticket_id"] == "OMN-9999"

    def test_non_mapping_rejected(self) -> None:
        assert _parse_receipt_payload("- a\n- b\n") is None

    def test_unparseable_rejected(self) -> None:
        assert _parse_receipt_payload("::: not : valid : yaml :::") is None

    def test_empty_rejected(self) -> None:
        assert _parse_receipt_payload("") is None


# ---------------------------------------------------------------------------
# git-backed probe: OccReceiptSubprocessProbe against a real temp repo
# ---------------------------------------------------------------------------


def _init_occ_repo(tmp_path: Path, receipts: dict[str, dict[str, object]]) -> Path:
    """Create a git repo with receipts committed under drift/dod_receipts/.

    ``receipts`` maps an OCC-root-relative path (e.g.
    ``drift/dod_receipts/OMN-9999/dod-run/command.yaml``) to a receipt payload.
    Returns the repo root path.
    """
    repo = tmp_path / "onex_change_control"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    for rel_path, payload in receipts.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(payload), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "receipts"],
        cwd=repo,
        check=True,
    )
    return repo


def _probe(repo: Path) -> OccReceiptSubprocessProbe:
    # Use HEAD as the governance ref for the temp repo (no remote to track).
    return OccReceiptSubprocessProbe(occ_repo_path=repo, governance_ref="HEAD")


class TestOccReceiptSubprocessProbe:
    def test_tracked_pass_receipt_returns_receipt_dir(self, tmp_path: Path) -> None:
        repo = _init_occ_repo(
            tmp_path,
            {"drift/dod_receipts/OMN-9999/dod-run/command.yaml": _pass_receipt()},
        )
        detail = _probe(repo).occ_receipt_detail(ticket_id="OMN-9999")
        assert detail == _occ_receipt_dir("OMN-9999")

    def test_non_pass_receipt_returns_none(self, tmp_path: Path) -> None:
        payload = _pass_receipt()
        payload["status"] = "FAIL"
        repo = _init_occ_repo(
            tmp_path,
            {"drift/dod_receipts/OMN-9999/dod-run/command.yaml": payload},
        )
        assert _probe(repo).occ_receipt_detail(ticket_id="OMN-9999") is None

    def test_no_tracked_receipt_returns_none(self, tmp_path: Path) -> None:
        # Receipt exists for a different ticket only.
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-1111/dod-run/command.yaml": _pass_receipt(
                    "OMN-1111"
                )
            },
        )
        assert _probe(repo).occ_receipt_detail(ticket_id="OMN-9999") is None

    def test_receipt_for_other_ticket_id_returns_none(self, tmp_path: Path) -> None:
        # Directory name matches but the receipt body binds a different ticket.
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-9999/dod-run/command.yaml": _pass_receipt(
                    "OMN-1111"
                )
            },
        )
        assert _probe(repo).occ_receipt_detail(ticket_id="OMN-9999") is None

    def test_multiple_receipts_one_pass_returns_dir(self, tmp_path: Path) -> None:
        fail = _pass_receipt()
        fail["status"] = "FAIL"
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-9999/item-a/command.yaml": fail,
                "drift/dod_receipts/OMN-9999/item-b/command.yaml": _pass_receipt(),
            },
        )
        assert _probe(repo).occ_receipt_detail(
            ticket_id="OMN-9999"
        ) == _occ_receipt_dir("OMN-9999")

    def test_missing_repo_dir_returns_none(self, tmp_path: Path) -> None:
        probe = OccReceiptSubprocessProbe(
            occ_repo_path=tmp_path / "does-not-exist", governance_ref="HEAD"
        )
        assert probe.occ_receipt_detail(ticket_id="OMN-9999") is None

    def test_unset_omni_home_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNI_HOME", raising=False)
        probe = OccReceiptSubprocessProbe()
        assert probe.occ_receipt_detail(ticket_id="OMN-9999") is None


# ---------------------------------------------------------------------------
# Handler: _apply_occ_receipt constructs OCC_RECEIPT evidence and closes
# ---------------------------------------------------------------------------


class TestApplyOccReceipt:
    def test_close_constructs_occ_receipt_evidence_and_writes_done(self) -> None:
        handler = HandlerLinearTriage()
        handler._mark_done = MagicMock()  # type: ignore[method-assign]
        ticket = _ticket("OMN-9999")

        actions, count, suppressed = handler._apply_occ_receipt(
            ticket,
            "drift/dod_receipts/OMN-9999",
            MagicMock(),
            dry_run=False,
            flag_only=False,
        )

        assert count == 1
        assert suppressed is None
        assert actions[0].action == EnumTriageAction.MARK_DONE
        # The decisive assertion: OCC_RECEIPT evidence was constructed and passed
        # to the _mark_done chokepoint.
        _, kwargs = handler._mark_done.call_args
        evidence = kwargs["evidence"]
        assert isinstance(evidence, ModelCloseEvidence)
        assert evidence.kind == EnumCloseEvidenceKind.OCC_RECEIPT
        assert "drift/dod_receipts/OMN-9999" in evidence.detail

    def test_close_passes_the_real_gate_and_writes_done(self) -> None:
        # No _mark_done stub — exercise the real fail-closed chokepoint end-to-end.
        handler = HandlerLinearTriage()
        client = MagicMock()
        ticket = _ticket("OMN-9999")

        _actions, count, _ = handler._apply_occ_receipt(
            ticket,
            "drift/dod_receipts/OMN-9999",
            client,
            dry_run=False,
            flag_only=False,
        )

        assert count == 1
        client.save_issue.assert_called_once_with(issue_id=ticket.id, state="Done")
        client.save_comment.assert_called_once()

    def test_flag_only_suppresses_without_write(self) -> None:
        handler = HandlerLinearTriage()
        client = MagicMock()
        actions, count, suppressed = handler._apply_occ_receipt(
            _ticket("OMN-9999"),
            "drift/dod_receipts/OMN-9999",
            client,
            dry_run=False,
            flag_only=True,
        )
        assert count == 0
        assert suppressed is not None
        assert actions[0].action == EnumTriageAction.WOULD_MARK_DONE
        client.save_issue.assert_not_called()

    def test_dry_run_does_not_write(self) -> None:
        handler = HandlerLinearTriage()
        client = MagicMock()
        actions, count, _ = handler._apply_occ_receipt(
            _ticket("OMN-9999"),
            "drift/dod_receipts/OMN-9999",
            client,
            dry_run=True,
            flag_only=False,
        )
        assert count == 0
        assert actions[0].action == EnumTriageAction.WOULD_MARK_DONE
        client.save_issue.assert_not_called()

    def test_open_children_suppresses_close(self) -> None:
        handler = HandlerLinearTriage()
        client = MagicMock()
        actions, count, suppressed = handler._apply_occ_receipt(
            _ticket("OMN-9999"),
            "drift/dod_receipts/OMN-9999",
            client,
            dry_run=False,
            flag_only=False,
            has_open_children=True,
        )
        assert (actions, count, suppressed) == ([], 0, None)
        client.save_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: full handle() drives the OCC_RECEIPT close path
# ---------------------------------------------------------------------------


class TestHandleOccReceiptPath:
    async def test_receipt_backed_ticket_is_closed_via_occ_receipt(self) -> None:
        client = _stub_linear_client([_make_issue(identifier="OMN-9999")])
        gh = _stub_github_no_prs()
        probe: OccReceiptProbe = _StubOccProbe(
            {"OMN-9999": "drift/dod_receipts/OMN-9999"}
        )
        handler = HandlerLinearTriage(
            client=client, github_client=gh, occ_receipt_probe=probe
        )

        result = await handler.handle(
            ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
        )

        assert result.marked_done == 1
        client.save_issue.assert_called_once_with(issue_id="id-OMN-9999", state="Done")
        occ_actions = [
            a
            for a in result.actions
            if a.action == EnumTriageAction.MARK_DONE and "OCC receipt" in a.evidence
        ]
        assert len(occ_actions) == 1

    async def test_no_receipt_ticket_is_not_closed(self) -> None:
        client = _stub_linear_client([_make_issue(identifier="OMN-9999")])
        gh = _stub_github_no_prs()
        probe: OccReceiptProbe = _StubOccProbe({"OMN-9999": None})
        handler = HandlerLinearTriage(
            client=client, github_client=gh, occ_receipt_probe=probe
        )

        result = await handler.handle(
            ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()

    async def test_wf_1628d9a5_batch_rejected_in_dry_run(self) -> None:
        """The wf_1628d9a5 false-Done batch shape: Backlog tickets with no PR and
        no tracked receipt (startedAt=null, zero attachments). The probe returns
        None for every one (fail-closed), so no OCC_RECEIPT evidence is
        constructed and zero closes occur — even with flag_only=False."""
        batch_ids = [
            "OMN-13797",
            "OMN-13798",
            "OMN-13800",
            "OMN-13802",
            "OMN-13803",
            "OMN-13805",
            "OMN-13788",
        ]
        client = _stub_linear_client([_make_issue(identifier=i) for i in batch_ids])
        gh = _stub_github_no_prs()
        # No tracked receipt for any batch ticket -> probe fails closed.
        probe: OccReceiptProbe = _StubOccProbe(dict.fromkeys(batch_ids))
        handler = HandlerLinearTriage(
            client=client, github_client=gh, occ_receipt_probe=probe
        )

        result = await handler.handle(
            ModelLinearTriageStartCommand(flag_only=False, dry_run=True)
        )

        assert result.marked_done == 0
        assert result.marked_done_superseded == 0
        close_actions = [
            a
            for a in result.actions
            if a.action
            in (EnumTriageAction.MARK_DONE, EnumTriageAction.WOULD_MARK_DONE)
        ]
        assert close_actions == []
        client.save_issue.assert_not_called()

    async def test_non_pass_receipt_batch_rejected(self) -> None:
        """Even when receipt directories exist, a non-PASS receipt makes the probe
        return None, so the gate constructs no OCC_RECEIPT evidence."""
        client = _stub_linear_client([_make_issue(identifier="OMN-13797")])
        gh = _stub_github_no_prs()
        probe: OccReceiptProbe = _StubOccProbe({"OMN-13797": None})
        handler = HandlerLinearTriage(
            client=client, github_client=gh, occ_receipt_probe=probe
        )

        result = await handler.handle(
            ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
