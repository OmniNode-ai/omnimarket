# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed disposition, reason and artifact vocabulary (OMN-20242)."""

from collections.abc import Mapping
from enum import StrEnum, unique
from types import MappingProxyType


@unique
class EnumDelegationDisposition(StrEnum):
    """How the caller used a delegate's output."""

    ACCEPTED_AS_IS = "accepted_as_is"
    EDITED = "edited"
    REJECTED = "rejected"
    IGNORED = "ignored"


@unique
class EnumDelegationDispositionReason(StrEnum):
    """The caller's reason for the disposition."""

    CORRECT_AS_IS = "correct_as_is"
    VERIFIED_AGAINST_SOURCE = "verified_against_source"
    MINOR_FIX = "minor_fix"
    PARTIAL_USE = "partial_use"
    REFORMATTED = "reformatted"
    WRONG_ANSWER = "wrong_answer"
    HALLUCINATED = "hallucinated"
    OFF_TASK = "off_task"
    INCOMPLETE = "incomplete"
    UNUSABLE_FORMAT = "unusable_format"
    NOT_NEEDED = "not_needed"
    SUPERSEDED = "superseded"
    ENGINE_FAILED = "engine_failed"
    LANE_DECIDED_FIRST = "lane_decided_first"


@unique
class EnumDelegationArtifactKind(StrEnum):
    """Artifact in which the caller used the answer, if any."""

    PULL_REQUEST = "pull_request"
    COMMIT = "commit"
    DOCUMENT = "document"
    NONE = "none"


DISPOSITION_REASONS: Mapping[
    EnumDelegationDisposition, frozenset[EnumDelegationDispositionReason]
] = MappingProxyType(
    {
        EnumDelegationDisposition.ACCEPTED_AS_IS: frozenset(
            {
                EnumDelegationDispositionReason.CORRECT_AS_IS,
                EnumDelegationDispositionReason.VERIFIED_AGAINST_SOURCE,
            }
        ),
        EnumDelegationDisposition.EDITED: frozenset(
            {
                EnumDelegationDispositionReason.MINOR_FIX,
                EnumDelegationDispositionReason.PARTIAL_USE,
                EnumDelegationDispositionReason.REFORMATTED,
            }
        ),
        EnumDelegationDisposition.REJECTED: frozenset(
            {
                EnumDelegationDispositionReason.WRONG_ANSWER,
                EnumDelegationDispositionReason.HALLUCINATED,
                EnumDelegationDispositionReason.OFF_TASK,
                EnumDelegationDispositionReason.INCOMPLETE,
                EnumDelegationDispositionReason.UNUSABLE_FORMAT,
            }
        ),
        EnumDelegationDisposition.IGNORED: frozenset(
            {
                EnumDelegationDispositionReason.NOT_NEEDED,
                EnumDelegationDispositionReason.SUPERSEDED,
                EnumDelegationDispositionReason.ENGINE_FAILED,
                EnumDelegationDispositionReason.LANE_DECIDED_FIRST,
            }
        ),
    }
)

__all__ = [
    "DISPOSITION_REASONS",
    "EnumDelegationArtifactKind",
    "EnumDelegationDisposition",
    "EnumDelegationDispositionReason",
]
