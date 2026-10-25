# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Executor-supplied identity shared by producers and historical readers."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


def engine_model(value: str) -> str | None:
    """Normalize executor aliases; a lab commit identity names only a host."""
    text = value.strip().lower()
    if "<" in text or ">" in text:
        return None
    tokens = set(re.split(r"[\s:_-]+", text))
    for token, model in (
        ("glm", "glm"),
        ("codex", "codex"),
        ("opus", "claude-opus"),
        ("sonnet", "claude-sonnet"),
        ("haiku", "claude-haiku"),
    ):
        if token in tokens:
            return model
    if text == "code-edit" or "qwen" in tokens:
        return "code-edit"
    if text == "claude":
        return "claude"
    return None


def commit_host(identity: str, prefix: str | None) -> str | None:
    if not prefix:
        return None
    match = re.fullmatch(
        re.escape(prefix) + r"([a-zA-Z0-9][a-zA-Z0-9._-]*)(?:\s+<[^>]+>)?",
        identity.strip(),
    )
    return match[1] if match else None


class ModelExecutionIdentity(BaseModel):
    """Actual placed executor, never the requested rung or the publishing host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    host: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

    @field_validator("engine")
    @classmethod
    def known_engine(cls, value: str) -> str:
        if engine_model(value) is None:
            raise ValueError("executor engine must name a supported engine")
        return value

    @field_validator("host")
    @classmethod
    def known_host(cls, value: str) -> str:
        if value.lower() in {"none", "unknown", "local"}:
            raise ValueError("executor host must name the actual host")
        return value

    def cells(self) -> dict[str, str]:
        return {
            "engine": self.engine,
            "model": engine_model(self.engine) or self.engine,
            "host": self.host,
        }
