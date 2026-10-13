# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub documents one audit judges, keyed by the API path that returned them.

A key absent from ``documents`` is a read that failed; ``errors`` carries the
failure text for it when the reader had one. The audit judges an absent
document exactly as the change-control script judged a failed ``gh api`` call.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue


def protection_key(org: str, repo: str, branch: str) -> str:
    return f"repos/{org}/{repo}/branches/{branch}/protection"


def repo_key(org: str, repo: str) -> str:
    return f"repos/{org}/{repo}"


def rulesets_key(org: str, repo: str) -> str:
    return f"repos/{org}/{repo}/rulesets"


def ruleset_key(org: str, repo: str, ruleset_id: int | str) -> str:
    return f"repos/{org}/{repo}/rulesets/{ruleset_id}"


def review_rules_key(org: str, repo: str) -> str:
    """Key of the GraphQL branchProtectionRules read, stored as ``{"data": ...}``."""
    return f"graphql:{org}/{repo}/branchProtectionRules"


class ModelBranchProtectionSnapshot(BaseModel):
    """Fetched GitHub documents for one audit run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    documents: dict[str, JsonValue] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "ModelBranchProtectionSnapshot",
    "protection_key",
    "repo_key",
    "review_rules_key",
    "ruleset_key",
    "rulesets_key",
]
