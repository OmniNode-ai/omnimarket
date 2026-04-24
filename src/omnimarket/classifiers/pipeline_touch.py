# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pipeline-touching PR classifier (OMN-9577).

Implements the automatic-trigger policy for `node_data_verification` (plan
OMN-7621, lines 202-205). The classifier is a pure function with no I/O, so it
can be invoked cheaply from PR webhooks, `dod_verify`, and tests.

Decision inputs in priority order (per plan line 203):

1. File path patterns (`migrations/`, `projections/`, `kafka/`, `handlers/`).
2. Ticket labels (`pipeline`, `migration`, `projection`).
3. Contract-declared `touches_pipeline: true`.

A PR is pipeline-touching if **any** input matches. The first matching input
(in the order above) is recorded as the primary `reason`; evidence from every
input that matched is preserved on the classification for audit trails.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.enums.enum_pipeline_touch_reason import EnumPipelineTouchReason
from omnimarket.models.model_pipeline_touch_classification import (
    ModelPipelineTouchClassification,
)

PIPELINE_PATH_PREFIXES: tuple[str, ...] = (
    "migrations/",
    "projections/",
    "kafka/",
    "handlers/",
)

PIPELINE_TICKET_LABELS: frozenset[str] = frozenset(
    {"pipeline", "migration", "projection"}
)


def _matching_paths(changed_files: Iterable[str]) -> tuple[str, ...]:
    matches: list[str] = []
    for path in changed_files:
        normalized = path.lstrip("/")
        for prefix in PIPELINE_PATH_PREFIXES:
            if prefix in normalized:
                matches.append(path)
                break
    return tuple(matches)


def _matching_labels(ticket_labels: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        label for label in ticket_labels if label.lower() in PIPELINE_TICKET_LABELS
    )


def is_pipeline_touching_pr(
    changed_files: Iterable[str],
    ticket_labels: Iterable[str] = (),
    contract_touches_pipeline: bool = False,
) -> ModelPipelineTouchClassification:
    """Classify whether a PR touches the data pipeline.

    Args:
        changed_files: Paths touched by the PR (e.g. from `git diff --name-only`
            or the GitHub API files endpoint). A substring match against
            `PIPELINE_PATH_PREFIXES` is used so nested paths like
            `src/foo/migrations/001.sql` still match.
        ticket_labels: Linear labels on the associated ticket. Matching is
            case-insensitive against `PIPELINE_TICKET_LABELS`.
        contract_touches_pipeline: Value of `touches_pipeline` declared in the
            ticket contract, if any.

    Returns:
        A `ModelPipelineTouchClassification`. The `reason` reflects the
        highest-priority input that matched; evidence from lower-priority
        inputs is still preserved on the model for audit receipts.
    """
    matched_paths = _matching_paths(changed_files)
    matched_labels = _matching_labels(ticket_labels)

    if matched_paths:
        reason = EnumPipelineTouchReason.FILE_PATH_MATCH
    elif matched_labels:
        reason = EnumPipelineTouchReason.TICKET_LABEL_MATCH
    elif contract_touches_pipeline:
        reason = EnumPipelineTouchReason.CONTRACT_DECLARATION
    else:
        reason = EnumPipelineTouchReason.NONE

    is_touching = reason is not EnumPipelineTouchReason.NONE

    return ModelPipelineTouchClassification(
        is_pipeline_touching=is_touching,
        reason=reason,
        matched_paths=matched_paths,
        matched_labels=matched_labels,
        contract_flag=bool(contract_touches_pipeline),
    )
