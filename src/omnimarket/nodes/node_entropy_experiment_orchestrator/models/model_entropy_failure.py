# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed failure taxonomy for entropy experiment track results (OMN-13614).

Absorbed from the SEA ``entropy_comparison/failure_taxonomy.py`` module as part of
the SEA->canonical migration (epic OMN-13604). Pure value objects and pure
classification helpers only -- no I/O, no executor coupling -- so the orchestrator
handler stays stateless and deterministic.

The SEA original mapped ``EnumSemanticFailure`` (a delegation-pipeline enum local
to the hackathon repo) into this taxonomy. omnimarket has no such enum, so
``entropy_failure_from_semantic`` accepts the legacy semantic-failure string
labels directly and maps them by value; unknown labels fall through to
``UNKNOWN`` (fail-soft classification, not silent data loss).
"""

from __future__ import annotations

import re
from enum import StrEnum, unique

from pydantic import BaseModel, ConfigDict

__all__ = [
    "EntropyFailureClass",
    "ModelEntropyFailure",
    "entropy_failure_from_exception",
    "entropy_failure_from_semantic",
    "sanitize_failure_message",
]

_LOCAL_PATH_RE = re.compile(r"(/Users|/Volumes)/[^\s:;,\")']+")
_MAX_SANITIZED_MESSAGE_LEN = 500


@unique
class EntropyFailureClass(StrEnum):
    """Closed failure classes for entropy experiment track failures."""

    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    COVERAGE_FAILED = "COVERAGE_FAILED"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    TOKEN_USAGE_MISSING = "TOKEN_USAGE_MISSING"
    UNKNOWN = "UNKNOWN"


class ModelEntropyFailure(BaseModel):
    """Reusable entropy failure record shape for track and result surfaces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_class: EntropyFailureClass
    run_id: str = ""
    framework: str = ""
    sample_index: int | None = None
    track_identity: str = ""
    terminal_status: str = ""
    correlation_id: str = ""
    retryable: bool = True
    original_exception_type: str = ""
    sanitized_message: str = ""
    evidence_path: str = ""
    model_registry_hash: str = ""
    track_contract_hash: str = ""
    prompt_hash: str = ""
    feature_spec_hash: str = ""


# Legacy SEA EnumSemanticFailure string labels -> entropy taxonomy. The SEA enum
# lived in the hackathon delegation pipeline; we map by its string values so the
# canonical node carries the same classification without importing SEA code.
_SEMANTIC_FAILURE_MAP: dict[str, EntropyFailureClass] = {
    "syntax_error": EntropyFailureClass.INVALID_RESPONSE,
    "schema_violation": EntropyFailureClass.CONTRACT_INVALID,
    "hallucinated_authority": EntropyFailureClass.INVALID_RESPONSE,
    "hidden_state_inference": EntropyFailureClass.INVALID_RESPONSE,
    "hardcoded_path": EntropyFailureClass.CONTRACT_INVALID,
    "hardcoded_topic": EntropyFailureClass.CONTRACT_INVALID,
    "replay_violation": EntropyFailureClass.CONTRACT_INVALID,
    "fixture_overfit": EntropyFailureClass.INVALID_RESPONSE,
    "nondeterministic_output": EntropyFailureClass.INVALID_RESPONSE,
    "unclassified": EntropyFailureClass.UNKNOWN,
}


def entropy_failure_from_semantic(failure_label: str | None) -> EntropyFailureClass:
    """Translate a legacy delegation semantic-failure label into the entropy taxonomy."""
    if failure_label is None:
        return EntropyFailureClass.UNKNOWN
    return _SEMANTIC_FAILURE_MAP.get(failure_label, EntropyFailureClass.UNKNOWN)


def entropy_failure_from_exception(exc: BaseException) -> EntropyFailureClass:
    """Classify execution exceptions into the entropy taxonomy."""
    declared_failure = getattr(exc, "failure_class", None)
    if isinstance(declared_failure, EntropyFailureClass):
        return declared_failure
    exception_name = type(exc).__name__.lower()
    message = str(exc).lower()
    if (
        isinstance(exc, TimeoutError)
        or "timeout" in exception_name
        or "timed out" in message
    ):
        return EntropyFailureClass.TIMEOUT
    if (
        isinstance(exc, (ConnectionError, OSError))
        or "unavailable" in message
        or "connection" in message
    ):
        return EntropyFailureClass.MODEL_UNAVAILABLE
    if isinstance(exc, ValueError):
        return EntropyFailureClass.INVALID_RESPONSE
    return EntropyFailureClass.UNKNOWN


def sanitize_failure_message(message: str) -> str:
    """Return a bounded, single-line failure message without local absolute paths."""
    sanitized = " ".join(message.split())
    sanitized = _LOCAL_PATH_RE.sub("<local_path>", sanitized)
    if len(sanitized) <= _MAX_SANITIZED_MESSAGE_LEN:
        return sanitized
    return f"{sanitized[: _MAX_SANITIZED_MESSAGE_LEN - 3]}..."
