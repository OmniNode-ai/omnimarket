# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The HTTP request and response shapes of the shared GitHub landing transport.

Frozen by OMN-19826 as part of node_pr_landing_github_effect's seam, and moved
here by OMN-19831 so the effect and the older landing nodes
(node_ci_rerun_effect, node_merge_sweep_auto_merge_arm_effect and
node_pr_lifecycle_fix_effect's auto-rebase) send through one transport.

A request never carries the credential: the transport adds the Authorization
header from the contract-declared ``GITHUB_TOKEN`` ref at send time, so a
request can be recorded (dry_run) or published without leaking it.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GITHUB_GRAPHQL_PATH = "/graphql"


class ModelGithubHttpRequest(BaseModel):
    """One GitHub API request, relative to the REST base URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH"]
    path: str = Field(
        min_length=2,
        description="Path from the API root, with any query string; /graphql for GraphQL.",
    )
    body: dict[str, object] | None = None
    if_none_match: str | None = Field(
        default=None, description="ETag for a conditional GET; a 304 costs no quota."
    )

    @field_validator("path")
    @classmethod
    def _path_is_relative_to_the_api_root(cls, value: str) -> str:
        if not value.startswith("/") or "://" in value:
            raise ValueError("path must start with '/' and carry no scheme or host")
        return value


class ModelGithubHttpResponse(BaseModel):
    """One GitHub API response as the transport received it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int = Field(ge=100, le=599)
    headers: dict[str, str]
    body: dict[str, object] | None = None

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None

    def message(self) -> str:
        """GitHub's error message, the GraphQL error messages, or the status."""
        body = self.body or {}
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            messages = [
                str(e.get("message"))
                for e in errors
                if isinstance(e, dict) and e.get("message")
            ]
            if messages:
                return "; ".join(messages)
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
        return (
            f"HTTP {self.status}"
            if not body
            else json.dumps(body, sort_keys=True)[:500]
        )

    def retry_after_seconds(self) -> int | None:
        value = self.header("retry-after")
        if value is None or not value.strip().isdigit():
            return None
        return int(value.strip())


class ModelGithubBytesResponse(BaseModel):
    """One GitHub response read as bytes under a size cap (OMN-20912).

    For the endpoints whose body is not JSON: a job's log (text) and an
    artifact's archive (zip). ``keep="head"`` stops reading past ``limit`` and
    sets ``over_limit``; ``keep="tail"`` reads the whole stream and keeps only
    its last ``limit`` bytes, with ``total_bytes`` the full length read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int = Field(ge=100, le=599)
    headers: dict[str, str]
    content: bytes
    total_bytes: int = Field(ge=0)
    over_limit: bool
    keep: Literal["head", "tail"]
