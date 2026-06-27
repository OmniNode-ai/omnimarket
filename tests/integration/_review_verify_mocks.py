# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared mock collaborators for the WS-5 Wave 4 review/verify integration suite.

These mocks satisfy ONLY the external I/O boundary (LLM inference, gh diff/review)
via constructor injection — the real orchestration / parsing / aggregation /
FSM-fold logic runs unmodified. Never monkeypatch subprocess/httpx; inject these.

Wave 4 nodes covered: node_pr_review_orchestrator,
node_hostile_reviewer_orchestrator, node_verify_effect,
node_verification_receipt_generator, node_two_strike_arbiter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.review.node_io import ModelGithubDiffResolvedEvent
from omnimarket.review.pr_review_io import EnumThreadStatus, ThreadState
from omnimarket.review.pr_review_node_io import (
    EnumGithubReviewOperation,
    ModelGithubReviewResultEvent,
)


def finding_payload(
    *,
    description: str,
    title: str | None = None,
    category: str = "logic_error",
    severity: str = "major",
    location: str = "src/example.py",
    confidence: str = "high",
) -> dict[str, str]:
    """Build one raw-finding dict in the shape the response parser consumes."""
    return {
        "title": title or description[:60],
        "description": description,
        "category": category,
        "severity": severity,
        "location": location,
        "confidence": confidence,
    }


def findings_json(payloads: list[dict[str, str]]) -> str:
    """Serialize raw findings to the JSON-array text a reviewer model would emit."""
    return json.dumps(payloads)


class _MockInferenceAdapter(ModelInferenceAdapter):
    """Deterministic inference adapter.

    Reviewer calls return ``review_raw`` (a JSON findings array, possibly empty);
    judge calls (system prompt names a "judge") return ``judge_raw``. No network,
    no env, no global mutation — the real prompt-builder / response-parser /
    aggregator run against this fixed output.
    """

    def __init__(
        self,
        *,
        review_raw: str = "[]",
        judge_raw: str = '{"verdict": "PASS", "reasoning": "fix confirmed in diff"}',
        per_model: dict[str, str] | None = None,
    ) -> None:
        self._review_raw = review_raw
        self._judge_raw = judge_raw
        self._per_model = per_model or {}
        self.calls: list[str] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(model_key)
        if "judge" in system_prompt.lower():
            return self._judge_raw
        if model_key in self._per_model:
            return self._per_model[model_key]
        return self._review_raw


class _DiffOutput:
    """Minimal stand-in for the github-diff EFFECT's ModelHandlerOutput."""

    def __init__(self, event: ModelGithubDiffResolvedEvent) -> None:
        self.events: tuple[ModelGithubDiffResolvedEvent, ...] = (event,)


class _MockGithubDiffEffect:
    """Resolve the review target to fixed diff content (no gh/network)."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[Any] = []

    async def handle(self, command: Any) -> _DiffOutput:
        self.calls.append(command)
        return _DiffOutput(
            ModelGithubDiffResolvedEvent(
                correlation_id=command.correlation_id,
                repo=getattr(command, "repo", None),
                pr_number=getattr(command, "pr_number", None),
                file_path=getattr(command, "file_path", None),
                content=self._content,
                content_chars=len(self._content),
            )
        )


class _ReviewOutput:
    """Minimal stand-in for the github-review EFFECT's ModelHandlerOutput."""

    def __init__(self, event: ModelGithubReviewResultEvent) -> None:
        self.events: tuple[ModelGithubReviewResultEvent, ...] = (event,)


class _MockGithubReviewEffect:
    """Mock GitHub review-side EFFECT (post threads / watch / report).

    POST_THREADS posts one POSTED thread per finding, capped at
    ``max_findings_per_pr`` (so threads_posted in the verdict is a real,
    param-driven count). WATCH returns the incoming thread states unchanged.
    POST_REPORT is a no-op. No gh writes.
    """

    def __init__(self) -> None:
        self.operations: list[EnumGithubReviewOperation] = []

    async def handle(self, command: Any) -> _ReviewOutput:
        self.operations.append(command.operation)
        op = command.operation
        if op is EnumGithubReviewOperation.POST_THREADS:
            now = datetime.now(tz=UTC)
            posted = tuple(
                ThreadState(
                    finding_id=_finding_id(f),
                    github_thread_id=1000 + idx,
                    status=EnumThreadStatus.POSTED,
                    posted_at=now,
                )
                for idx, f in enumerate(command.findings[: command.max_findings_per_pr])
            )
            return _ReviewOutput(
                ModelGithubReviewResultEvent(
                    correlation_id=command.correlation_id,
                    operation=op,
                    repo=command.repo,
                    pr_number=command.pr_number,
                    thread_states=posted,
                )
            )
        if op is EnumGithubReviewOperation.WATCH_THREADS:
            return _ReviewOutput(
                ModelGithubReviewResultEvent(
                    correlation_id=command.correlation_id,
                    operation=op,
                    repo=command.repo,
                    pr_number=command.pr_number,
                    thread_states=command.thread_states,
                )
            )
        # POST_REPORT
        return _ReviewOutput(
            ModelGithubReviewResultEvent(
                correlation_id=command.correlation_id,
                operation=op,
                repo=command.repo,
                pr_number=command.pr_number,
                thread_states=command.thread_states,
                report_comment_id=42,
            )
        )


def _finding_id(finding: Any) -> UUID:
    """Extract a finding id from either a ReviewFinding or a raw object."""
    return finding.id
