# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B handler: assemble evidence on the lab, return a terminal payload."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import overload
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from omnimarket.handlers.work_ledger_text import ledger_row_stamp
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_branch_claim_check_effect.handlers.branch_claim_parity_gate import (
    missing_claim_rows,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.branch_claim_resolution import (
    build_index,
    resolve_with_index,
    ticket_from_branch,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.check_run_poster import (
    GitHubCheckRunPoster,
    ProtocolCheckRunPoster,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.ledger_witness_reader import (
    FileLedgerWitness,
    ProtocolLedgerWitness,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.pr_commit_reader import (
    GitHubPrCommitReader,
    ProtocolPrCommitReader,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.work_ledger_row_reader import (
    PostgresWorkLedgerRowReader,
    ProtocolWorkLedgerRowReader,
    format_database_error,
)
from omnimarket.nodes.node_branch_claim_check_effect.models import (
    EnumBranchClaimOutcome as Outcome,
)
from omnimarket.nodes.node_branch_claim_check_effect.models import (
    ModelBranchClaimCheckRequest,
    ModelBranchClaimCheckResult,
    ModelBranchClaimPolicy,
    load_branch_claim_policy,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_pr_state_observation import (
    ModelGitHubPrStateObservation,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"


def _iso(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else "-"


class HandlerBranchClaimCheck:
    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        reader: ProtocolWorkLedgerRowReader | None = None,
        witness: ProtocolLedgerWitness | None = None,
        commit_reader: ProtocolPrCommitReader | None = None,
        poster: ProtocolCheckRunPoster | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.policy: ModelBranchClaimPolicy = load_branch_claim_policy(
            self._contract_path
        )
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._reader = (
            reader
            if reader is not None
            else PostgresWorkLedgerRowReader(self.policy.ledger_source)
        )
        self._witness = witness
        self._commits = (
            commit_reader
            if commit_reader is not None
            else GitHubPrCommitReader(
                self._contract_path,
                per_page=self.policy.commits_per_page,
                max_commits=self.policy.max_pr_commits,
            )
        )
        self._poster = (
            poster if poster is not None else GitHubCheckRunPoster(self._contract_path)
        )

    @overload
    def handle(
        self, request: ModelBranchClaimCheckRequest
    ) -> ModelBranchClaimCheckResult: ...

    @overload
    def handle(
        self, request: ModelGitHubPrStateObservation
    ) -> ModelBranchClaimCheckResult: ...

    def handle(self, request: BaseModel) -> ModelBranchClaimCheckResult:
        # The runtime selects the typed def-B arm from a BaseModel class, not
        # a union annotation. Each topic's event_model supplies its concrete
        # payload; overloads keep the two accepted inputs explicit to callers.
        if not isinstance(
            request, (ModelBranchClaimCheckRequest, ModelGitHubPrStateObservation)
        ):
            raise TypeError(
                "branch claim handler requires its contract-declared payload"
            )
        correlation_id = (
            request.correlation_id
            if isinstance(request, ModelBranchClaimCheckRequest)
            else uuid5(
                NAMESPACE_URL, f"{request.repo}#{request.pr_number}@{request.head_sha}"
            )
        )
        result = ModelBranchClaimCheckResult(
            correlation_id=correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
            head_ref=request.head_ref,
            ticket=None,
            outcome=Outcome.NOT_APPLICABLE,
            conclusion=None,
            check_name=self.policy.check_name,
        )
        if request.repo not in self.policy.repositories:
            return result
        if isinstance(request, ModelGitHubPrStateObservation) and not (
            request.github_event == "pull_request"
            and request.ci_status == "PENDING"
            and request.head_sha
            and request.head_ref
        ):
            return result
        stage = "pull request head unreadable"
        try:
            if isinstance(request, ModelGitHubPrStateObservation):
                request = ModelBranchClaimCheckRequest(
                    correlation_id=correlation_id,
                    repo=request.repo,
                    pr_number=request.pr_number,
                    head_sha=request.head_sha,
                    head_ref=request.head_ref,
                )
            ticket = ticket_from_branch(request.head_ref)
            result = result.model_copy(update={"ticket": ticket})
            stage = "evaluation clock unreadable"
            until = self._now().astimezone(UTC)
            since = until - timedelta(hours=self.policy.read_window_hours)
            result = result.model_copy(
                update={"window_since": since, "window_until": until}
            )
            if ticket is None:
                result = result.model_copy(
                    update={
                        "outcome": Outcome.NO_TICKET,
                        "conclusion": Outcome.NO_TICKET.conclusion,
                    }
                )
            else:
                result = self._decide(request, result, since, until)
        except Exception as exc:
            result = self._failed(result, f"{stage}: {type(exc).__name__}: {exc}")
        return self._post(result)

    def _failed(
        self, result: ModelBranchClaimCheckResult, cause: str
    ) -> ModelBranchClaimCheckResult:
        return result.model_copy(
            update={
                "outcome": Outcome.DID_NOT_RUN,
                "conclusion": Outcome.DID_NOT_RUN.conclusion,
                "cause": f"{cause}; the check did not decide",
            }
        )

    def _decide(
        self,
        request: ModelBranchClaimCheckRequest,
        result: ModelBranchClaimCheckResult,
        since: datetime,
        until: datetime,
    ) -> ModelBranchClaimCheckResult:
        ticket = result.ticket
        assert ticket is not None
        stage = "work ledger database unreadable"
        try:
            rows = self._reader.read_rows(ticket=ticket, since=since, until=until)
            result = result.model_copy(update={"rows_read": len(rows)})
            row_ids = self._reader.read_row_ids(since=since, until=until)
            newest = self._reader.newest_row_ts(until=until)
            stage = "parity witness unreadable"
            witness = (
                self._witness
                if self._witness is not None
                else FileLedgerWitness(self.policy.parity_witness, since=since)
            )
            witness_rows = witness.read_rows()
            witness_newest = max(
                (ledger_row_stamp(row) for row in witness_rows), default=None
            )
            if (
                newest is not None
                and newest >= since
                and (witness_newest is None or witness_newest < newest)
            ):
                return self._failed(
                    result,
                    f"parity witness is behind the database (witness newest {_iso(witness_newest)}, database newest {_iso(newest)}): rows after {_iso(witness_newest)} cannot be measured",
                )
            stage = "parity evidence unreadable"
            all_missing = missing_claim_rows(
                ticket=None,
                witness_rows=witness_rows,
                db_row_ids=row_ids,
                since=since,
                until=until,
                transition_rows=self.policy.claim_transition_rows,
            )
            missing = missing_claim_rows(
                ticket=ticket,
                witness_rows=witness_rows,
                db_row_ids=row_ids,
                since=since,
                until=until,
                transition_rows=self.policy.claim_transition_rows,
            )
            result = result.model_copy(
                update={"window_missing_claim_rows": len(all_missing)}
            )
            if missing:
                result = result.model_copy(
                    update={
                        "findings": tuple(
                            row[: self.policy.max_finding_chars] for row in missing
                        )
                    }
                )
                return self._failed(
                    result,
                    f"parity: {len(missing)} CLAIM/TERMINAL rows for {ticket} in the window are missing from work_ledger_rows",
                )
            stage = "pull request commits unreadable"
            commits = self._commits.list_commits(request.repo, request.pr_number)
            if len(commits) >= self.policy.max_pr_commits:
                return self._failed(
                    result,
                    "pull request commits unreadable: commit list may be truncated",
                )
            stage = "work ledger claim resolution unreadable"
            index = build_index(
                rows, now=until, staleness_hours=self.policy.staleness_hours
            )
            verdict = resolve_with_index(
                index, branch=request.head_ref, commits=commits, policy=self.policy
            )
            held = verdict.holder
            return result.model_copy(
                update={
                    "outcome": verdict.outcome,
                    "conclusion": verdict.outcome.conclusion,
                    "holder_lane": held.lane if held else None,
                    "holder_fence": held.fence if held else None,
                    "holder_row": held.citation(self.policy.short_sha_length)
                    if held
                    else None,
                    "claimed_at": held.claimed_at if held else None,
                    "last_activity_at": held.last_activity_at if held else None,
                    "lanes": verdict.lanes,
                    "findings": verdict.findings,
                }
            )
        except Exception as exc:
            error = (
                format_database_error(exc, self.policy.ledger_source)
                if stage == "work ledger database unreadable"
                else f"{type(exc).__name__}: {exc}"
            )
            return self._failed(result, f"{stage}: {error}")

    def _post(self, result: ModelBranchClaimCheckResult) -> ModelBranchClaimCheckResult:
        title = f"{result.outcome.value}: {result.ticket or '-'} {result.holder_lane or result.cause or '-'}"[
            : self.policy.max_title_chars
        ]
        lines = [
            f"{self.policy.outcome_marker_prefix} {result.outcome.value} ticket={result.ticket or '-'} holder={result.holder_lane or '-'} window={_iso(result.window_since)}..{_iso(result.window_until)} rows={result.rows_read}"
        ]
        if result.cause:
            lines.append(result.cause)
        lines.extend(result.findings)
        lines.append(
            f"source: {self.policy.ledger_source.relation} (read on the lab by node_branch_claim_check_effect)"
        )
        try:
            if result.head_sha is None or result.conclusion is None:
                raise ValueError("check post lacks head SHA or conclusion")
            self._poster.post(
                repo=result.repo,
                head_sha=result.head_sha,
                name=result.check_name,
                conclusion=result.conclusion,
                title=title,
                summary="\n".join(lines),
            )
            return result.model_copy(update={"check_posted": True})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "Branch claim check post failed for %s#%s@%s: %s",
                result.repo,
                result.pr_number,
                result.head_sha,
                error,
            )
            return result.model_copy(update={"post_error": error})
