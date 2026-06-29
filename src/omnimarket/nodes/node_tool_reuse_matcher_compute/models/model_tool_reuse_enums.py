# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enums for the tool-reuse matcher compute node (OMN-13356)."""

from __future__ import annotations

from enum import StrEnum


class EnumToolReuseMatchStrategy(StrEnum):
    """Selection of the matching algorithm.

    SIGNATURE  — exact input/output contract-signature match only (0-latency,
                 deterministic). Preferred fast path.
    SEMANTIC   — deterministic lexical similarity over the task description
                 (token-set Jaccard). NO LLM, NO external embedding service.
    HYBRID     — signature match first; lexical-similarity fallback when no
                 signature hit. Default.
    """

    SIGNATURE = "signature"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class EnumToolReuseVerdict(StrEnum):
    """Reuse-matcher verdict.

    MATCHED               — single high-confidence reuse candidate found.
    NO_MATCH              — no candidate cleared the threshold; escalate to
                            fresh generation.
    AMBIGUOUS             — two or more candidates cleared the threshold; the
                            caller must rank or escalate.
    REGISTRY_UNAVAILABLE  — the injected registry query failed.
    """

    MATCHED = "matched"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    REGISTRY_UNAVAILABLE = "registry_unavailable"


__all__ = ["EnumToolReuseMatchStrategy", "EnumToolReuseVerdict"]
