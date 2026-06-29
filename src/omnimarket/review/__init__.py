# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared review primitives for omnimarket review nodes.

OWNER package for the prompt-builder / response-parser pure COMPUTE primitives
(OMN-13208 / A1) and the PR-review domain + FSM + node-I/O models (OMN-13212 /
B2) shared across the canonical review nodes (node_hostile_reviewer_orchestrator,
node_pr_review_orchestrator, node_pr_review_fsm_reducer, node_github_review_effect,
node_judge_verdict_parse_compute). No node reaches into a sibling node's private
models package — shared types live here.
"""

from omnimarket.review.pr_review_fsm import (
    MAX_CONSECUTIVE_FAILURES,
    TERMINAL_PHASES,
    ModelPhaseTransitionEvent,
    ModelPrReviewBotState,
    advance,
    make_verdict,
    next_phase,
    start_state,
)
from omnimarket.review.pr_review_io import (
    DiffHunk,
    EnumFsmPhase,
    EnumPrVerdict,
    EnumThreadStatus,
    PrReviewFindingEvidence,
    ReviewFinding,
    ReviewRequest,
    ReviewVerdict,
    ThreadState,
)
from omnimarket.review.pr_review_node_io import (
    EnumGithubReviewOperation,
    ModelGithubReviewCommand,
    ModelGithubReviewResultEvent,
    ModelJudgeParseRequest,
    ModelJudgeParseResult,
)
from omnimarket.review.prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)
from omnimarket.review.response_parser import (
    EnumParseStatus,
    ModelParseResult,
    parse_model_response,
)

__all__: list[str] = [
    "MAX_CONSECUTIVE_FAILURES",
    "TERMINAL_PHASES",
    "DiffHunk",
    "EnumFsmPhase",
    "EnumGithubReviewOperation",
    "EnumParseStatus",
    "EnumPrVerdict",
    "EnumThreadStatus",
    "ModelGithubReviewCommand",
    "ModelGithubReviewResultEvent",
    "ModelJudgeParseRequest",
    "ModelJudgeParseResult",
    "ModelParseResult",
    "ModelPhaseTransitionEvent",
    "ModelPrReviewBotState",
    "ModelPromptBuilderInput",
    "ModelPromptBuilderOutput",
    "PrReviewFindingEvidence",
    "ReviewFinding",
    "ReviewRequest",
    "ReviewVerdict",
    "ThreadState",
    "advance",
    "build_prompt",
    "make_verdict",
    "next_phase",
    "parse_model_response",
    "start_state",
]
