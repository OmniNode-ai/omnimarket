# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the review-gate context sweep tests: subjects, verdict facts, the policy and the real handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.model_landing_review import (
    EnumReviewVerdict,
    ModelLandingReviewFacts,
    ModelLandingReviewVerdictRow,
)
from omnimarket.models.model_review_gate_context import (
    ModelReviewGateContextPolicy,
    ModelReviewGatePrSubject,
)
from omnimarket.nodes.node_review_gate_context_compute.handlers.handler_review_gate_context import (
    HandlerReviewGateContext,
)
from omnimarket.nodes.node_review_gate_context_compute.overlay import (
    ModelReviewGateContextOverlay,
)
from omnimarket.nodes.node_review_gate_context_effect.adapters.inmemory_head_check_runs import (
    InMemoryHeadCheckRuns,
)
from omnimarket.nodes.node_review_gate_context_effect.handlers.handler_review_gate_context_effect import (
    HandlerReviewGateContextEffect,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.handlers.handler_review_gate_context_orchestrator import (
    HandlerReviewGateContextOrchestrator,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ProtocolOpenPullRequestSource,
    ProtocolReviewVerdictSource,
)

HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
NEW_HEAD = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"
SECOND_HEAD = "1111111111111111111111111111111111111111"
THIRD_HEAD = "2222222222222222222222222222222222222222"
NOW = datetime(2026, 11, 15, 13, 0, 0, tzinfo=UTC)
OWNER = "example-org"
BOT = "example-release-bot[bot]"
COMPUTE_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_review_gate_context_compute"
    / "overlay.example.yaml"
)


def policy() -> ModelReviewGateContextPolicy:
    """The policy of the compute node's own synthetic overlay, so the sweep and the decision read one policy."""
    document = yaml.safe_load(COMPUTE_EXAMPLE.read_text(encoding="utf-8"))
    return ModelReviewGateContextOverlay.model_validate(document).policy


def subject(
    repo: str,
    number: int,
    head: str = HEAD,
    *,
    author: str = "lane-author",
    head_ref: str = "omn-1-change",
    title: str = "OMN-1: a change",
) -> ModelReviewGatePrSubject:
    return ModelReviewGatePrSubject(
        repository=f"{OWNER}/{repo}",
        pr_number=number,
        head_sha=head,
        author_login=author,
        head_ref=head_ref,
        title=title,
    )


def verdict_facts(head: str, verdict: EnumReviewVerdict) -> ModelLandingReviewFacts:
    return ModelLandingReviewFacts(
        new_work=False,
        verdicts=(
            ModelLandingReviewVerdictRow(head_sha=head, verdict=verdict, at=NOW),
        ),
    )


class Rig:
    """The real compute and effect handlers over in-memory runs, around the sources a test injects."""

    def __init__(
        self,
        open_prs: ProtocolOpenPullRequestSource,
        verdicts: ProtocolReviewVerdictSource,
        *,
        runs: InMemoryHeadCheckRuns | None = None,
        orchestrator_class: type[
            HandlerReviewGateContextOrchestrator
        ] = HandlerReviewGateContextOrchestrator,
    ) -> None:
        self.runs = runs or InMemoryHeadCheckRuns()
        self.sink = InMemoryEmissionSink()
        effect = HandlerReviewGateContextEffect(
            reader=self.runs,
            poster=self.runs,
            sink=self.sink,
            emission_node="node_review_gate_context_effect",
            emission_piece=9,
            emission_backend="inmemory",
            route_backend="inmemory",
            clock=lambda: NOW,
        )
        self.handler = orchestrator_class(
            open_prs=open_prs,
            verdicts=verdicts,
            decide=HandlerReviewGateContext(),
            poster=effect,
            policy=policy(),
            clock=lambda: NOW,
        )
