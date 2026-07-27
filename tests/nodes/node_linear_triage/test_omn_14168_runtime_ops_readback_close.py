# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14168: RUNTIME_OPS_READBACK close-evidence kind (Surface B).

Proves the close-gate half of the no-PR runtime-ops evidence class:

* the pure ``_receipt_is_runtime_ops_readback_for_ticket`` validator accepts only
  a fully-guardrailed readback receipt and rejects every abuse (self-attested,
  PR-bearing, git verb, no-source-change false, empty readback, missing
  prevention follow-up, prod target) — fail-closed;
* the git-backed ``RuntimeOpsReadbackSubprocessProbe`` positively verifies a
  tracked receipt end-to-end against a real temp repo and returns ``None`` for a
  plain OCC receipt or any failed guardrail; and
* ``_apply_runtime_ops_readback`` constructs
  ``ModelCloseEvidence(kind=RUNTIME_OPS_READBACK, ...)`` and closes through the
  ``_mark_done`` chokepoint, honoring flag_only / open-children guards.

The verifier receipt itself is minted by the autogen verification tick / an
independent verifier (feedback_no_self_authored_evidence); the auto-sweep caller
wiring is deferred to OMN-13856. These tests exercise the probe + apply directly.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
    RuntimeOpsReadbackProbe,
    RuntimeOpsReadbackSubprocessProbe,
    _is_prod_target,
    _occ_receipt_dir,
    _receipt_is_runtime_ops_readback_for_ticket,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTicket,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
)


def _readback_receipt(ticket_id: str = "OMN-14159", **overrides: object) -> dict:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "evidence_item_id": "dod-runtime",
        "check_type": "runtime_readback",
        "status": "PASS",
        "run_timestamp": datetime.now(tz=UTC).isoformat(),
        "commit_sha": "0000000",
        "runner": "impl-agent",
        "verifier": "verify-agent",
        "probe_command": "kubectl -n onex-dev get pods",
        "probe_stdout": "omnidash 1/1 Running 0 117m\n",
        "evidence_class": "runtime_ops",
        "mutation_command": "kubectl -n onex-dev patch deployment omnidash ...",
        "mutation_verb": "patch",
        "target_identity": "onex-dev/Deployment/omnidash",
        "no_source_change": True,
        "prevention_followup": "OMN-14161",
    }
    payload.update(overrides)
    return payload


def _ticket(identifier: str = "OMN-14159") -> ModelLinearTicket:
    return ModelLinearTicket(
        id=f"id-{identifier}",
        identifier=identifier,
        title=f"{identifier} runtime-ops fix",
        state="Backlog",
        updated_at="2026-07-08T18:35:39.782Z",
        branch_name="",
        parent_id="",
    )


def _init_occ_repo(tmp_path: Path, receipts: dict[str, dict]) -> Path:
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


def _probe(repo: Path) -> RuntimeOpsReadbackSubprocessProbe:
    return RuntimeOpsReadbackSubprocessProbe(occ_repo_path=repo, governance_ref="HEAD")


# ---------------------------------------------------------------------------
# Pure validator
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuntimeOpsReadbackValidator:
    def test_well_formed_receipt_accepted(self) -> None:
        assert _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(), "OMN-14159"
        )

    def test_non_runtime_ops_class_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(evidence_class="backend"), "OMN-14159"
        )

    def test_self_attested_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(verifier="impl-agent"), "OMN-14159"
        )

    def test_pr_bearing_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(pr_number=1349), "OMN-14159"
        )

    def test_git_verb_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(mutation_verb="git"), "OMN-14159"
        )

    def test_no_source_change_false_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(no_source_change=False), "OMN-14159"
        )

    def test_empty_readback_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(probe_stdout="  "), "OMN-14159"
        )

    def test_missing_prevention_followup_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(prevention_followup=""), "OMN-14159"
        )

    def test_prod_target_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt(target_identity="onex-prod/Deployment/omnidash"),
            "OMN-14159",
        )

    def test_mismatched_ticket_rejected(self) -> None:
        assert not _receipt_is_runtime_ops_readback_for_ticket(
            _readback_receipt("OMN-1111"), "OMN-14159"
        )

    def test_is_prod_target_helper(self) -> None:
        assert _is_prod_target("onex-prod/x") is True
        assert _is_prod_target("onex-dev/x") is False


# ---------------------------------------------------------------------------
# Git-backed probe
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuntimeOpsReadbackSubprocessProbe:
    def test_is_runtime_checkable_protocol(self, tmp_path: Path) -> None:
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-14159/dod-runtime/command.yaml": _readback_receipt()
            },
        )
        assert isinstance(_probe(repo), RuntimeOpsReadbackProbe)

    def test_tracked_readback_returns_receipt_dir(self, tmp_path: Path) -> None:
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-14159/dod-runtime/command.yaml": _readback_receipt()
            },
        )
        detail = _probe(repo).runtime_ops_readback_detail(ticket_id="OMN-14159")
        assert detail == _occ_receipt_dir("OMN-14159")

    def test_plain_occ_receipt_returns_none(self, tmp_path: Path) -> None:
        # A PASS receipt with no runtime-ops fields is NOT a readback receipt.
        plain = _readback_receipt()
        for k in (
            "evidence_class",
            "mutation_verb",
            "mutation_command",
            "no_source_change",
            "prevention_followup",
            "target_identity",
        ):
            plain.pop(k, None)
        repo = _init_occ_repo(
            tmp_path,
            {"drift/dod_receipts/OMN-14159/dod-runtime/command.yaml": plain},
        )
        assert _probe(repo).runtime_ops_readback_detail(ticket_id="OMN-14159") is None

    def test_self_attested_receipt_returns_none(self, tmp_path: Path) -> None:
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-14159/dod-runtime/command.yaml": _readback_receipt(
                    verifier="impl-agent"
                )
            },
        )
        assert _probe(repo).runtime_ops_readback_detail(ticket_id="OMN-14159") is None

    def test_prod_target_receipt_returns_none(self, tmp_path: Path) -> None:
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-14159/dod-runtime/command.yaml": _readback_receipt(
                    target_identity="onex-prod/Deployment/omnidash"
                )
            },
        )
        assert _probe(repo).runtime_ops_readback_detail(ticket_id="OMN-14159") is None

    def test_no_tracked_receipt_returns_none(self, tmp_path: Path) -> None:
        repo = _init_occ_repo(
            tmp_path,
            {
                "drift/dod_receipts/OMN-1111/dod-runtime/command.yaml": _readback_receipt(
                    "OMN-1111"
                )
            },
        )
        assert _probe(repo).runtime_ops_readback_detail(ticket_id="OMN-14159") is None

    def test_missing_repo_dir_returns_none(self, tmp_path: Path) -> None:
        probe = RuntimeOpsReadbackSubprocessProbe(
            occ_repo_path=tmp_path / "nope", governance_ref="HEAD"
        )
        assert probe.runtime_ops_readback_detail(ticket_id="OMN-14159") is None


# ---------------------------------------------------------------------------
# Handler: _apply_runtime_ops_readback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyRuntimeOpsReadback:
    def test_close_constructs_runtime_ops_evidence_and_writes_done(self) -> None:
        handler = HandlerLinearTriage()
        handler._mark_done = MagicMock()  # type: ignore[method-assign]
        ticket = _ticket()

        actions, count, suppressed = handler._apply_runtime_ops_readback(
            ticket,
            _occ_receipt_dir("OMN-14159"),
            MagicMock(),
            dry_run=False,
            flag_only=False,
        )

        assert count == 1
        assert suppressed is None
        assert actions[0].action == EnumTriageAction.MARK_DONE
        handler._mark_done.assert_called_once()
        evidence = handler._mark_done.call_args.kwargs["evidence"]
        assert isinstance(evidence, ModelCloseEvidence)
        assert evidence.kind == EnumCloseEvidenceKind.RUNTIME_OPS_READBACK
        assert "RUNTIME_OPS readback receipt tracked" in evidence.detail

    def test_flag_only_suppresses_without_mutation(self) -> None:
        handler = HandlerLinearTriage()
        handler._mark_done = MagicMock()  # type: ignore[method-assign]
        actions, count, suppressed = handler._apply_runtime_ops_readback(
            _ticket(),
            _occ_receipt_dir("OMN-14159"),
            MagicMock(),
            dry_run=False,
            flag_only=True,
        )
        assert count == 0
        assert suppressed is not None
        assert actions[0].action == EnumTriageAction.WOULD_MARK_DONE
        handler._mark_done.assert_not_called()

    def test_open_children_is_noop(self) -> None:
        handler = HandlerLinearTriage()
        handler._mark_done = MagicMock()  # type: ignore[method-assign]
        actions, count, suppressed = handler._apply_runtime_ops_readback(
            _ticket(),
            _occ_receipt_dir("OMN-14159"),
            MagicMock(),
            dry_run=False,
            flag_only=False,
            has_open_children=True,
        )
        assert (actions, count, suppressed) == ([], 0, None)
        handler._mark_done.assert_not_called()

    def test_dry_run_does_not_mutate(self) -> None:
        handler = HandlerLinearTriage()
        handler._mark_done = MagicMock()  # type: ignore[method-assign]
        actions, count, _ = handler._apply_runtime_ops_readback(
            _ticket(),
            _occ_receipt_dir("OMN-14159"),
            MagicMock(),
            dry_run=True,
            flag_only=False,
        )
        assert count == 0
        assert actions[0].action == EnumTriageAction.WOULD_MARK_DONE
        handler._mark_done.assert_not_called()
