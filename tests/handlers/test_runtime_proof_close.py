# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket closes only on runtime proof (OMN-22334).

Merge alone never closes. A passing runtime receipt at a revision that contains the
ticket's merged change does. A receipt at a revision lacking the change does not.
A docs-only or plan ticket closes on the proof its definition of done names.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.runtime_proof_close_rules import (
    MERGED_NOT_PROVEN_RUNNING,
    GitRevisionContainment,
    evaluate_proof_close,
)
from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumProofAction,
    EnumProofTicketKind,
    EnumRuntimeReceiptKind,
    ModelDeclaredProof,
    ModelProofCloseRequest,
    ModelRuntimeReceipt,
)

CHANGE = "a" * 40
CONTAINING = "b" * 40
LACKING = "c" * 40


class FakeContainment:
    """Revisions that contain the change; any other revision does not."""

    def __init__(self, containing: set[str], unreadable: set[str] | None = None):
        self._containing = containing
        self._unreadable = unreadable or set()

    def contains(self, *, change_sha: str, revision: str) -> bool | None:
        if revision in self._unreadable:
            return None
        return change_sha == CHANGE and revision in self._containing


def _receipt(
    revision: str,
    *,
    kind: EnumRuntimeReceiptKind = EnumRuntimeReceiptKind.LAB_PASS,
    passed: bool = True,
    exercises_running_system: bool = True,
) -> ModelRuntimeReceipt:
    return ModelRuntimeReceipt(
        kind=kind,
        passed=passed,
        revision=revision,
        host="h201",
        exercises_running_system=exercises_running_system,
    )


def _request(**overrides: object) -> ModelProofCloseRequest:
    base: dict[str, object] = {
        "ticket_id": "OMN-1",
        "ticket_kind": EnumProofTicketKind.CODE,
        "change_merged": True,
        "change_sha": CHANGE,
        "receipts": (),
    }
    return ModelProofCloseRequest.model_validate({**base, **overrides})


CONTAINMENT = FakeContainment({CONTAINING})


def test_merged_only_does_not_close() -> None:
    decision = evaluate_proof_close(_request(), CONTAINMENT)
    assert decision.allowed is False
    assert decision.action is EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING
    assert decision.reason.startswith(MERGED_NOT_PROVEN_RUNNING)
    assert MERGED_NOT_PROVEN_RUNNING == "merged, not yet proven running"


def test_merged_only_does_not_close_unmerged_change_is_held_too() -> None:
    decision = evaluate_proof_close(
        _request(change_merged=False, receipts=(_receipt(CONTAINING),)), CONTAINMENT
    )
    assert decision.allowed is False
    assert decision.action is EnumProofAction.HELD_NOT_MERGED


def test_merged_only_does_not_close_failed_or_non_runtime_receipts() -> None:
    for receipt in (
        _receipt(CONTAINING, passed=False),
        _receipt(
            CONTAINING,
            kind=EnumRuntimeReceiptKind.DOD_VERIFY_PASS,
            exercises_running_system=False,
        ),
    ):
        decision = evaluate_proof_close(_request(receipts=(receipt,)), CONTAINMENT)
        assert decision.allowed is False
        assert decision.reason.startswith(MERGED_NOT_PROVEN_RUNNING)


@pytest.mark.parametrize("kind", list(EnumRuntimeReceiptKind))
def test_merged_plus_runtime_receipt_closes(kind: EnumRuntimeReceiptKind) -> None:
    receipt = _receipt(CONTAINING, kind=kind)
    decision = evaluate_proof_close(_request(receipts=(receipt,)), CONTAINMENT)
    assert decision.allowed is True
    assert decision.action is EnumProofAction.CLOSE
    assert decision.proving_receipt == receipt


def test_receipt_without_change_does_not_close() -> None:
    decision = evaluate_proof_close(
        _request(receipts=(_receipt(LACKING),)), CONTAINMENT
    )
    assert decision.allowed is False
    assert decision.action is EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING
    assert decision.reason.startswith(MERGED_NOT_PROVEN_RUNNING)
    assert LACKING in decision.reason


def test_receipt_without_change_does_not_close_when_containment_unreadable() -> None:
    containment = FakeContainment({CONTAINING}, unreadable={CONTAINING})
    decision = evaluate_proof_close(
        _request(receipts=(_receipt(CONTAINING),)), containment
    )
    assert decision.allowed is False
    assert decision.reason.startswith(MERGED_NOT_PROVEN_RUNNING)
    assert "unreadable" in decision.reason


def test_receipt_without_change_does_not_close_one_good_receipt_still_closes() -> None:
    decision = evaluate_proof_close(
        _request(receipts=(_receipt(LACKING), _receipt(CONTAINING))), CONTAINMENT
    )
    assert decision.allowed is True


def test_receipt_without_change_does_not_close_blank_change_sha() -> None:
    decision = evaluate_proof_close(
        _request(change_sha="", receipts=(_receipt(CONTAINING),)), CONTAINMENT
    )
    assert decision.allowed is False
    assert decision.reason.startswith(MERGED_NOT_PROVEN_RUNNING)


@pytest.mark.parametrize(
    "ticket_kind", [EnumProofTicketKind.DOCS, EnumProofTicketKind.PLAN]
)
def test_docs_or_plan_names_declared_proof(ticket_kind: EnumProofTicketKind) -> None:
    # merge alone, no declared proof: held
    held = evaluate_proof_close(_request(ticket_kind=ticket_kind), CONTAINMENT)
    assert held.allowed is False
    assert held.action is EnumProofAction.HELD_DECLARED_PROOF_MISSING

    # a runtime receipt is not the proof a docs or plan ticket names
    other = evaluate_proof_close(
        _request(ticket_kind=ticket_kind, receipts=(_receipt(CONTAINING),)),
        CONTAINMENT,
    )
    assert other.allowed is False

    # the named proof, not yet evidenced: held with the proof named
    unmet = ModelDeclaredProof(name="plan registered on the board", satisfied=False)
    pending = evaluate_proof_close(
        _request(ticket_kind=ticket_kind, declared_proof=unmet), CONTAINMENT
    )
    assert pending.allowed is False
    assert "plan registered on the board" in pending.reason

    # the named proof, evidenced: closes
    met = ModelDeclaredProof(
        name="plan registered on the board",
        satisfied=True,
        evidence="board readback 2026-11-03",
    )
    closed = evaluate_proof_close(
        _request(ticket_kind=ticket_kind, declared_proof=met), CONTAINMENT
    )
    assert closed.allowed is True
    assert closed.action is EnumProofAction.CLOSE


def test_docs_or_plan_names_declared_proof_unmerged_is_held() -> None:
    met = ModelDeclaredProof(name="p", satisfied=True, evidence="e")
    decision = evaluate_proof_close(
        _request(
            ticket_kind=EnumProofTicketKind.DOCS,
            change_merged=False,
            declared_proof=met,
        ),
        CONTAINMENT,
    )
    assert decision.allowed is False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def test_receipt_without_change_does_not_close_real_git_ancestry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("0")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("1")
    _git(repo, "commit", "-qam", "change")
    change = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("2")
    _git(repo, "commit", "-qam", "after")
    after = _git(repo, "rev-parse", "HEAD")

    containment = GitRevisionContainment(repo)
    assert containment.contains(change_sha=change, revision=after) is True
    assert containment.contains(change_sha=change, revision=base) is False
    assert containment.contains(change_sha=change, revision="f" * 40) is None

    def decide(revision: str) -> bool:
        return evaluate_proof_close(
            _request(change_sha=change, receipts=(_receipt(revision),)), containment
        ).allowed

    assert decide(after) is True
    assert decide(base) is False
