# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the landing orchestrator tests: messages, facts and doubles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel

from omnimarket.events.pr_arm_gate import (
    EnumArmDecision,
    ModelArmGateDecision,
    ModelArmGateRequest,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubCheckRunFact,
    ModelGithubPrStateFact,
    ModelGithubQuotaReading,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_gate_facts import (
    EnumPrLandingFactState,
    ModelPrLandingGateFacts,
    ModelPrLandingLabPass,
    ModelPrLandingLedgerHold,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    ModelPrLandingAutobindPrompt,
    ModelPrLandingGithubCompletedIngress,
    ModelPrLandingMergedIngress,
    ModelPrLandingReconcileCommand,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)

REPO = "OmniNode-ai/omnimarket"
PR = 4242
KEY = f"{REPO}#{PR}"
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
T0 = datetime(2026, 9, 27, 8, 0, 0, tzinfo=UTC)
NODE_ID = "PR_kwDOtest0000000001"


def prompt(at: datetime = T0, **overrides: object) -> ModelPrLandingAutobindPrompt:
    """A push prompt exactly as publish_occ_autobind_command.py sends it (no op)."""
    payload: dict[str, object] = {
        "block_reason": "receipt_evidence_source_autobind",
        "correlation_id": str(uuid4()),
        "pr_number": PR,
        "repo": REPO,
        "requested_at": at.isoformat(),
        "ticket_id": "OMN-19829",
    }
    payload.update(overrides)
    return ModelPrLandingAutobindPrompt.model_validate(payload)


def reconcile(at: datetime, tick: str = "tick-1") -> ModelPrLandingReconcileCommand:
    return ModelPrLandingReconcileCommand.model_validate(
        {"repository": REPO, "pr_number": PR, "requested_at": at, "tick_id": tick}
    )


def merged(at: datetime, event_id: str = "merged-1") -> ModelPrLandingMergedIngress:
    return ModelPrLandingMergedIngress.model_validate(
        {
            "event_id": event_id,
            "repo": REPO,
            "branch": "dev",
            "pr_number": PR,
            "ticket": "OMN-19829",
            "merged_at": at.isoformat(),
        }
    )


def pr_fact(
    *,
    head: str = HEAD_1,
    draft: bool = False,
    title: str = "feat(OMN-19829): land the orchestrator",
    labels: tuple[str, ...] = (),
    state: str = "open",
    merged_: bool = False,
    auto_merge: bool = False,
) -> ModelGithubPrStateFact:
    return ModelGithubPrStateFact.model_validate(
        {
            "pr_number": PR,
            "head_sha": head,
            "base_ref": "dev",
            "state": state,
            "merged": merged_,
            "draft": draft,
            "title": title,
            "labels": labels,
            "auto_merge_armed": auto_merge,
            "pr_node_id": NODE_ID,
        }
    )


QUOTA = ModelGithubQuotaReading(
    limit=5000,
    remaining=4000,
    used=1000,
    reset=1790496983,
    resource="core",
    identity="GITHUB_TOKEN",
)


def answer(
    request: ModelPrLandingGithubRequest,
    *,
    pr_state: ModelGithubPrStateFact | None = None,
    not_modified: bool = False,
    check_runs: tuple[ModelGithubCheckRunFact, ...] = (),
    etag: str | None = None,
) -> ModelPrLandingGithubCompletedIngress:
    """The effect's completion for one request, as the effect would publish it."""
    requests = request.to_http_requests()
    enforce = request.mode is EnumPrLandingGithubMode.ENFORCE
    status = 304 if not_modified else 200
    is_read = request.operation in (
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    )
    return ModelPrLandingGithubCompletedIngress(
        correlation_id=request.correlation_id,
        operation=request.operation,
        mode=request.mode,
        repository=request.repository,
        pr_number=request.pr_number,
        head_sha=request.head_sha,
        requests=requests,
        http_statuses=tuple(status for _ in requests) if enforce else (),
        not_modified=not_modified,
        etag=(etag or 'W/"e"') if is_read else None,
        check_runs=check_runs,
        pr_state=pr_state,
        quota=QUOTA if enforce else None,
    )


def check_run(
    name: str, run_id: int, check_run_id: int, head: str = HEAD_1
) -> ModelGithubCheckRunFact:
    return ModelGithubCheckRunFact(
        check_run_id=check_run_id,
        name=name,
        status="completed",
        conclusion="success",
        head_sha=head,
        details_url=f"https://github.com/{REPO}/actions/runs/{run_id}/job/{check_run_id}",
        app_slug="github-actions",
        check_suite_id=None,
    )


def requests_in(emitted: list[BaseModel]) -> list[ModelPrLandingGithubRequest]:
    return [e for e in emitted if isinstance(e, ModelPrLandingGithubRequest)]


def only_request(emitted: list[BaseModel]) -> ModelPrLandingGithubRequest:
    found = requests_in(emitted)
    assert len(found) == 1, found
    return found[0]


class FixedClassifier:
    """Answers every head-check read with one verdict for that head."""

    def __init__(
        self, verdict: EnumHeadCheckVerdict = EnumHeadCheckVerdict.GREEN
    ) -> None:
        self.verdict = verdict
        self.calls = 0

    async def classify(
        self, completed: ModelPrLandingGithubCompleted, row: ModelPrLandingWorkflowRow
    ) -> ModelHeadCheckVerdict:
        self.calls += 1
        assert completed.head_sha is not None
        return ModelHeadCheckVerdict(
            repository=completed.repository,
            pr_number=completed.pr_number,
            head_sha=completed.head_sha,
            verdict=self.verdict,
        )


class ArmingGate:
    """An arm gate that arms: stands in for an enforce policy with every fact green."""

    def __init__(self) -> None:
        self.requests: list[ModelArmGateRequest] = []

    async def handle(self, request: ModelArmGateRequest) -> ModelArmGateDecision:
        self.requests.append(request)
        return ModelArmGateDecision(
            repo=request.candidate.repo,
            pr_number=request.candidate.pr_number,
            decision=EnumArmDecision.ARM,
        )


def later(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class FixedGateFacts:
    """The ledger projection as a test states it: holds, PASS heads, or unreadable."""

    def __init__(
        self,
        *,
        holds: tuple[ModelPrLandingLedgerHold, ...] = (),
        lab_heads: tuple[str, ...] = (),
        unreadable: str | None = None,
        raise_on_read: bool = False,
        pass_every_head: bool = False,
    ) -> None:
        self.holds = holds
        self.lab_heads = lab_heads
        self.pass_every_head = pass_every_head
        self.unreadable = unreadable
        self.raise_on_read = raise_on_read
        self.reads: list[tuple[str, int, str]] = []

    async def read(
        self, repository: str, pr_number: int, head_sha: str, now: datetime
    ) -> ModelPrLandingGateFacts:
        self.reads.append((repository, pr_number, head_sha))
        if self.raise_on_read:
            msg = "the reader broke"
            raise RuntimeError(msg)
        state = (
            EnumPrLandingFactState.UNKNOWN
            if self.unreadable is not None
            else EnumPrLandingFactState.KNOWN
        )
        return ModelPrLandingGateFacts(
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            read_at=now,
            holds_state=state,
            holds=self.holds if self.unreadable is None else (),
            ledger_newest_projected_at=now if self.unreadable is None else None,
            lab_state=state,
            lab_passes=tuple(
                ModelPrLandingLabPass(
                    source="lab_proof_receipt",
                    ref=f"{repository}#{pr_number}@{head}:runtime@1",
                    head_sha=head,
                    result="PASS",
                    verifier_token="PASS",
                )
                for head in ((head_sha,) if self.pass_every_head else self.lab_heads)
            )
            if self.unreadable is None
            else (),
            unknown_detail=self.unreadable,
        )


def passing_gate_facts(*heads: str) -> FixedGateFacts:
    """No hold in force and a PASS lab proof for each head named (every head if none)."""
    return FixedGateFacts(lab_heads=heads, pass_every_head=not heads)
