# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20368: the triage node stops closing a ticket on a merged PR alone.

Operator ruling (2026-10-02): a ticket reaches Done only on a PASS dod_verify
whose checks bind every acceptance criterion. Before this change a merged
implementing PR was enough for ``_mark_done`` to write Done. These tests fail
there and pass with the gate: a merged PR with no bound receipt leaves the
ticket open and records why; a bound PASS receipt closes it.

This module is about the real gate, so it opts out of the suite-wide stand-down.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
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
from omnimarket.nodes.node_linear_triage.services.done_write_receipt_gate import (
    DodVerifySubprocessProbe,
    DoneWriteReceiptRefusedError,
    dod_verify_argv,
)

USES_REAL_DONE_WRITE_GATE = True

_PROBE_RUN = "omnimarket.nodes.node_linear_triage.services.done_write_receipt_gate.subprocess.run"

_DESCRIPTION = (
    "## Acceptance Criteria\n"
    "- **AC1**: the triage node leaves a ticket open without a bound receipt\n"
    "- **AC2**: the triage node closes a ticket with a bound PASS receipt\n"
)
_STATE_MODEL = (
    "omnimarket.nodes.node_dod_verify.models.model_dod_verify_state.ModelDodVerifyState"
)
_SUMMARY_MODEL = (
    "omnibase_infra.cli.model_receipt_runtime_summary.ModelReceiptRuntimeSummary"
)


def _verdict(
    *, binds: dict[str, list[str]], status: str = "verified"
) -> dict[str, Any]:
    checks = [
        {
            "evidence_id": check_id,
            "status": "verified",
            "proof_class": "behavior",
            "binds_ac": acs,
        }
        for check_id, acs in binds.items()
    ]
    return {
        "status": status,
        "total_checks": len(checks),
        "verified_count": len(checks) if status == "verified" else 0,
        "failed_count": 0,
        "non_probative_count": 0,
        "checks": checks,
    }


_BOUND = _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]})
_UNBOUND = _verdict(binds={"t1": ["AC1"], "t2": []})


class _Probe:
    """A verdict probe that records how often it was asked."""

    def __init__(self, verdict: dict[str, Any] | None, reason: str = "") -> None:
        self.calls: list[str] = []
        self._verdict = verdict
        self._reason = reason

    def verdict_for(self, *, ticket_id: str) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(ticket_id)
        return self._verdict, self._reason


def _linear(
    issue: dict[str, Any], *, description: str | None = _DESCRIPTION
) -> LinearClientProtocol:
    client = MagicMock(spec=LinearClientProtocol)
    client.list_issues.return_value = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [issue],
            }
        }
    }
    client.list_children.return_value = {
        "data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
    }
    client.list_issue_history.return_value = {
        "data": {"issue": {"history": {"nodes": []}}}
    }
    if description is None:
        client.get_issue.side_effect = RuntimeError("linear unreachable")
    else:
        client.get_issue.return_value = {
            "data": {"issue": {"id": issue["id"], "description": description}}
        }
    return cast(LinearClientProtocol, client)


def _issue() -> dict[str, Any]:
    return {
        "id": "uuid-1",
        "identifier": "OMN-820368",
        "title": "Test ticket",
        "state": {"name": "In Progress"},
        "updatedAt": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        "branchName": "",
        "parent": None,
        "labels": {"nodes": []},
    }


def _github_with_merged_implementing_pr() -> GitHubClientProtocol:
    gh = MagicMock(spec=GitHubClientProtocol)
    gh.search_prs.return_value = [
        {
            "number": "42",
            "title": "fix(OMN-820368): the work",
            "body": "",
            "state": "closed",
            "mergedAt": "2026-04-08T10:00:00Z",
            "url": "https://github.com/OmniNode-ai/omnimarket/pull/42",
            "repo": "omnimarket",
        }
    ]
    gh.search_prs_in_repo.return_value = []
    gh.list_prs_by_head.return_value = []
    gh.pr_closing_ticket_refs.return_value = []
    return cast(GitHubClientProtocol, gh)


def _ticket() -> ModelLinearTicket:
    return ModelLinearTicket(
        id="uuid-1",
        identifier="OMN-820368",
        title="Test ticket",
        state="In Progress",
        updated_at="2026-10-01T00:00:00+00:00",
    )


# --------------------------------------------------------------------------
# The sweep: a merged PR alone no longer closes
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestMergedPrAloneDoesNotClose:
    async def test_merged_pr_with_no_bound_receipt_leaves_the_ticket_and_says_why(
        self,
    ) -> None:
        client = _linear(_issue())
        probe = _Probe(_UNBOUND)
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=probe,
        )
        result = await handler.handle(
            ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        client.save_comment.assert_not_called()
        held = [
            a
            for a in result.actions
            if a.action == EnumTriageAction.HELD_NO_BOUND_RECEIPT
        ]
        assert len(held) == 1
        assert held[0].ticket_id == "OMN-820368"
        assert "AC2" in held[0].evidence
        assert probe.calls == ["OMN-820368"]

    async def test_merged_pr_with_a_bound_pass_receipt_closes(self) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=_Probe(_BOUND),
        )
        result = await handler.handle(
            ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
        )

        assert result.marked_done == 1
        client.save_issue.assert_called_once_with(issue_id="uuid-1", state="Done")

    async def test_merged_pr_when_the_verifier_reaches_no_verdict_leaves_the_ticket(
        self,
    ) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=_Probe(None, "Timeout running dod_verify for OMN-820368"),
        )
        result = await handler.handle(
            ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert any(
            a.action == EnumTriageAction.HELD_NO_BOUND_RECEIPT
            and "Timeout" in a.evidence
            for a in result.actions
        )

    async def test_merged_pr_when_the_description_cannot_be_read_leaves_the_ticket(
        self,
    ) -> None:
        client = _linear(_issue(), description=None)
        probe = _Probe(_BOUND)
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=probe,
        )
        result = await handler.handle(
            ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        assert probe.calls == []

    async def test_a_failed_verdict_is_not_a_pass(self) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=_Probe(
                _verdict(binds={"t1": ["AC1"], "t2": ["AC2"]}, status="failed")
            ),
        )
        result = await handler.handle(
            ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
        )

        assert result.marked_done == 0
        client.save_issue.assert_not_called()


# --------------------------------------------------------------------------
# The chokepoint: every evidence kind meets the same bar
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("kind", list(EnumCloseEvidenceKind))
class TestEveryCloseKindMeetsTheBar:
    def test_unbound_receipt_refuses_before_any_write(
        self, kind: EnumCloseEvidenceKind
    ) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(client=client, dod_verdict_probe=_Probe(_UNBOUND))
        with pytest.raises(DoneWriteReceiptRefusedError) as refused:
            handler._mark_done(
                client=client,
                ticket=_ticket(),
                comment="closing",
                evidence=ModelCloseEvidence(kind=kind, detail="evidence"),
            )
        assert refused.value.ticket_id == "OMN-820368"
        client.save_issue.assert_not_called()
        client.save_comment.assert_not_called()

    def test_bound_pass_receipt_writes_done(self, kind: EnumCloseEvidenceKind) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(client=client, dod_verdict_probe=_Probe(_BOUND))
        handler._mark_done(
            client=client,
            ticket=_ticket(),
            comment="closing",
            evidence=ModelCloseEvidence(kind=kind, detail="evidence"),
        )
        client.save_issue.assert_called_once_with(issue_id="uuid-1", state="Done")
        client.save_comment.assert_called_once()


# --------------------------------------------------------------------------
# The default probe: onex skill dod_verify, parsed off either receipt arm
# --------------------------------------------------------------------------


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="boom"
    )


def _fake_run(
    stdout: str, returncode: int = 0
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(stdout, returncode)

    return run


@pytest.mark.unit
class TestSubprocessProbe:
    def test_argv_runs_the_verifier_node_under_this_interpreter(self) -> None:
        assert dod_verify_argv("OMN-1") == [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dod_verify",
            "--ticket-id",
            "OMN-1",
            "--execution-audience",
            "hosted",
        ]

    @pytest.mark.parametrize(
        "receipt",
        [
            {"result_model": _STATE_MODEL, "result": _BOUND},
            {"result_model": _SUMMARY_MODEL, "result": {"terminal_payload": _BOUND}},
            _BOUND,  # the node entry point prints the verdict itself, flat
        ],
    )
    def test_reads_whichever_shape_it_prints_whatever_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, receipt: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(
            _PROBE_RUN,
            _fake_run(json.dumps(receipt), returncode=1),
        )
        verdict, why = DodVerifySubprocessProbe().verdict_for(ticket_id="OMN-1")
        assert verdict == _BOUND
        assert why == ""

    def test_junk_output_is_no_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            _PROBE_RUN,
            _fake_run("not json", returncode=2),
        )
        verdict, why = DodVerifySubprocessProbe().verdict_for(ticket_id="OMN-1")
        assert verdict is None
        assert "printed no JSON verdict" in why

    def test_a_receipt_with_no_verdict_is_no_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _PROBE_RUN,
            _fake_run(json.dumps({"result": {}})),
        )
        verdict, _ = DodVerifySubprocessProbe().verdict_for(ticket_id="OMN-1")
        assert verdict is None

    def test_a_timeout_is_no_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="onex", timeout=1)

        monkeypatch.setattr(_PROBE_RUN, boom)
        verdict, why = DodVerifySubprocessProbe().verdict_for(ticket_id="OMN-1")
        assert verdict is None
        assert "Timeout" in why

    def test_a_missing_binary_is_no_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError("onex")

        monkeypatch.setattr(_PROBE_RUN, boom)
        verdict, why = DodVerifySubprocessProbe().verdict_for(ticket_id="OMN-1")
        assert verdict is None
        assert "OS error" in why
