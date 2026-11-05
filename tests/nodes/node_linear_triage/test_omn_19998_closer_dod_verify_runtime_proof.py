# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the triage closer reads runtime proof from the dod_verify verdict.

The recorded case: sprint triage held every merged ticket as "merged, not yet proven
running", because the handler had no runtime-proof source unless one was injected, so a
merged ticket whose dod_verify PASS had read the running system at a revision holding the
merge stayed open. The verdict the Done-write gate already reads is that source: a verified
``readback`` check (it reads live state from a running system) whose product clone sat at a
revision that contains the merged change is a ``dod_verify_pass`` runtime receipt.

Everything else still holds: a verdict that is not a PASS, a PASS with only behaviour
checks, a readback at a revision lacking the merge, a clone of a different repository, and
no verdict at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.runtime_proof_close_rules import MERGED_NOT_PROVEN_RUNNING
from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumProofAction,
    EnumRuntimeReceiptKind,
)
from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTriageStartCommand,
)
from omnimarket.nodes.node_linear_triage.services.runtime_proof_from_dod_verify import (
    CachedDodVerdictProbe,
    DodVerifyRuntimeProofProbe,
)
from tests.nodes.node_linear_triage.test_omn_20368_done_write_receipt_gate import (
    _github_with_merged_implementing_pr,
    _issue,
    _linear,
    _Probe,
)

USES_REAL_RUNTIME_PROOF_GATE = True

_MERGED_PR = {
    "number": "42",
    "title": "fix(OMN-820368): the work",
    "mergedAt": "2026-04-08T10:00:00Z",
    "url": "https://github.com/OmniNode-ai/omnimarket/pull/42",
    "repo": "omnimarket",
}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **scrub_git_location_env(os.environ),
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        },
    )
    return done.stdout.strip()


def _commit(clone: Path, name: str, subject: str) -> str:
    (clone / name).write_text(subject, encoding="utf-8")
    _git(clone, "add", name)
    _git(clone, "commit", "-q", "-m", subject)
    return _git(clone, "rev-parse", "HEAD")


def _clone(tmp_path: Path, repo: str = "omnimarket") -> tuple[Path, str, str]:
    """A clone of ``repo``: (path, sha before the merge, sha of the squash merge #42)."""
    clone = tmp_path / repo
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "dev")
    _git(clone, "remote", "add", "origin", f"https://github.com/OmniNode-ai/{repo}.git")
    before = _commit(clone, "base.txt", "chore: base (#41)")
    merged = _commit(clone, "work.txt", "fix(OMN-820368): the work (#42)")
    _git(clone, "update-ref", "refs/remotes/origin/dev", merged)
    return clone, before, merged


def _verdict(
    *, clone: Path, head: str, proof_class: str = "readback", status: str = "verified"
) -> dict[str, Any]:
    return {
        "status": status,
        "total_checks": 1,
        "verified_count": 1 if status == "verified" else 0,
        "failed_count": 0 if status == "verified" else 1,
        "non_probative_count": 0,
        "checks": [
            {
                "evidence_id": "dod-001",
                "status": "verified" if status == "verified" else "failed",
                "proof_class": proof_class,
                "binds_ac": ["AC1"],
                "product_clones": [
                    {"repo_root": str(clone), "freshness": "fresh", "head_sha": head}
                ],
            }
        ],
    }


async def _triage(handler: HandlerLinearTriage) -> Any:
    return await handler.handle(
        ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
    )


@pytest.mark.unit
class TestRecordedCase:
    async def test_merged_ticket_with_running_proof_in_dod_verify_closes(
        self, tmp_path: Path
    ) -> None:
        clone, _, merged = _clone(tmp_path)
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=_Probe(_verdict(clone=clone, head=merged)),
        )
        result = await _triage(handler)

        assert result.marked_done == 1
        client.save_issue.assert_called_once()
        assert not [
            a
            for a in result.actions
            if a.action == EnumTriageAction.HELD_MERGED_NOT_PROVEN_RUNNING
        ]

    async def test_no_verdict_still_holds(self) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=_Probe(None, "dod_verify printed no JSON verdict"),
        )
        result = await _triage(handler)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        held = [
            a
            for a in result.actions
            if a.action == EnumTriageAction.HELD_MERGED_NOT_PROVEN_RUNNING
        ]
        assert len(held) == 1
        assert MERGED_NOT_PROVEN_RUNNING in held[0].evidence


@pytest.mark.unit
class TestDodVerifyRuntimeProofProbe:
    def test_readback_at_containing_revision_is_runtime_proof(
        self, tmp_path: Path
    ) -> None:
        clone, _, merged = _clone(tmp_path)
        probe = DodVerifyRuntimeProofProbe(_Probe(_verdict(clone=clone, head=merged)))
        decision = probe.proof_for(ticket_id="OMN-820368", merged_pr=_MERGED_PR)

        assert decision is not None
        assert decision.allowed
        assert decision.action is EnumProofAction.CLOSE
        assert decision.proving_receipt is not None
        assert decision.proving_receipt.kind is EnumRuntimeReceiptKind.DOD_VERIFY_PASS
        assert decision.proving_receipt.exercises_running_system
        assert merged in decision.reason

    def test_readback_at_revision_lacking_the_merge_holds(self, tmp_path: Path) -> None:
        clone, before, _ = _clone(tmp_path)
        probe = DodVerifyRuntimeProofProbe(_Probe(_verdict(clone=clone, head=before)))
        decision = probe.proof_for(ticket_id="OMN-820368", merged_pr=_MERGED_PR)

        assert decision is not None
        assert not decision.allowed
        assert decision.action is EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING
        assert "lacks the merged change" in decision.reason

    def test_behavior_only_pass_holds(self, tmp_path: Path) -> None:
        clone, _, merged = _clone(tmp_path)
        probe = DodVerifyRuntimeProofProbe(
            _Probe(_verdict(clone=clone, head=merged, proof_class="behavior"))
        )
        decision = probe.proof_for(ticket_id="OMN-820368", merged_pr=_MERGED_PR)

        assert decision is not None
        assert not decision.allowed
        assert MERGED_NOT_PROVEN_RUNNING in decision.reason

    def test_failed_verdict_holds(self, tmp_path: Path) -> None:
        clone, _, merged = _clone(tmp_path)
        probe = DodVerifyRuntimeProofProbe(
            _Probe(_verdict(clone=clone, head=merged, status="failed"))
        )
        decision = probe.proof_for(ticket_id="OMN-820368", merged_pr=_MERGED_PR)

        assert decision is not None
        assert not decision.allowed

    def test_clone_of_another_repository_holds(self, tmp_path: Path) -> None:
        clone, _, merged = _clone(tmp_path, repo="omnibase_core")
        probe = DodVerifyRuntimeProofProbe(_Probe(_verdict(clone=clone, head=merged)))
        decision = probe.proof_for(ticket_id="OMN-820368", merged_pr=_MERGED_PR)

        assert decision is not None
        assert not decision.allowed
        assert decision.action is EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING

    def test_no_verdict_is_no_proof(self) -> None:
        probe = DodVerifyRuntimeProofProbe(_Probe(None, "timed out"))
        assert probe.proof_for(ticket_id="OMN-820368", merged_pr=_MERGED_PR) is None


@pytest.mark.unit
def test_cached_verdict_probe_runs_the_verifier_once_per_ticket() -> None:
    inner = _Probe({"status": "verified", "total_checks": 0, "checks": []})
    cached = CachedDodVerdictProbe(inner)
    cached.verdict_for(ticket_id="OMN-1")
    cached.verdict_for(ticket_id="OMN-1")
    cached.verdict_for(ticket_id="OMN-2")
    assert inner.calls == ["OMN-1", "OMN-2"]


@pytest.mark.unit
def test_cached_verdict_probe_reads_fresh_after_clear() -> None:
    inner = _Probe({"status": "verified", "total_checks": 0, "checks": []})
    cached = CachedDodVerdictProbe(inner)
    cached.verdict_for(ticket_id="OMN-1")
    cached.clear()
    cached.verdict_for(ticket_id="OMN-1")
    assert inner.calls == ["OMN-1", "OMN-1"]
