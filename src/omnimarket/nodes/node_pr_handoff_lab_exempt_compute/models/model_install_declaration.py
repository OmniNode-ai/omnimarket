# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host_install declaration pr-handoff judges a PR's install-first proof with (OMN-21815), as an overlay document.

Every key is required and nothing has a default: an absent or malformed key refuses the load, so a half-written
declaration never reads as "requires nothing". The declaration is data in the overlays repository
(``node_pr_handoff_reads_effect/overlay.yaml``), never a list in code.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelPrHandoffInstallDeclaration(BaseModel):
    """Which changed files change what runs on a host, and what an observation must say."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: fnmatch patterns (``*`` also crosses a slash) matched whole against each repo-relative changed path.
    paths: tuple[str, ...] = Field(min_length=1)
    #: patterns that count only in the named repository.
    repo_paths: dict[str, tuple[str, ...]]
    #: a path matching one of these never needs install proof (tests, documents, CI-only folders).
    excluded_paths: tuple[str, ...]
    #: prod and grant paths keep their own approval gate, so they are out of this rule.
    prod_paths: tuple[str, ...]
    installed_cell: str = Field(min_length=1)
    observed_cell: str = Field(min_length=1)
    #: an observation must contain one of these as a whole word.
    observed_words: tuple[str, ...] = Field(min_length=1)
    #: regular expressions; an observation matching one is only that tests passed.
    observed_refused: tuple[str, ...]
    min_observed_chars: int = Field(ge=1)
    ruling: str = Field(min_length=1)

    @field_validator(
        "paths", "excluded_paths", "prod_paths", "observed_words", "observed_refused"
    )
    @classmethod
    def _no_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v.strip() for v in values):
            raise ValueError("a blank entry")
        return values

    @field_validator("observed_refused")
    @classmethod
    def _compiles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in values:
            try:
                re.compile(pattern, re.I)
            except re.error as exc:
                raise ValueError(
                    "an observed_refused entry is not a regular expression"
                ) from exc
        return values

    @field_validator("repo_paths")
    @classmethod
    def _repo_paths(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for repo, patterns in value.items():
            if not repo.strip() or not patterns or any(not p.strip() for p in patterns):
                raise ValueError("a repo_paths entry is blank or empty")
        return value


class ModelPrHandoffReadsOverlay(BaseModel):
    """``<root>/node_pr_handoff_reads_effect/overlay.yaml``: the one document the install-proof read loads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_install: ModelPrHandoffInstallDeclaration
