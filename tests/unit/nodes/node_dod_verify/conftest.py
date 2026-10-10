# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared fixtures for node_dod_verify unit tests (OMN-20838).

``HandlerDodEvidenceGithubEffect`` reads the PR watcher's state and the
canonical clones under ``$OMNI_HOME`` before it reads GitHub. On an operator or
lab host both exist, so a test that feeds the handler GitHub-shaped fixtures
through a patched ``subprocess.run`` would otherwise be answered from the
host's real state instead. Every test here starts with a local source that
holds nothing and an empty required-context cache; a test of the local source
installs its own.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.handlers import (
    handler_dod_evidence_github_effect as hd_mod,
)
from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    DodEvidenceLocalSource,
)


class EmptyLocalSource(DodEvidenceLocalSource):
    """A local source that holds no fact, so every read falls to the fixture."""

    def pr_view(self, repo: str, number: int) -> dict[str, Any] | None:
        return None

    def commit_parents(self, repo: str, sha: str) -> list[str] | None:
        return None

    def changed_files(
        self, repo: str, parent_sha: str, merge_sha: str
    ) -> list[dict[str, object]] | None:
        return None

    def merged_pr_candidates(
        self, repo: str, ticket_id: str, ticket_pattern: re.Pattern[str]
    ) -> list[dict[str, Any]]:
        return []

    def cwd_repo(self, cwd: object = None) -> str | None:
        return None

    def head_check_runs(
        self, repo: str, number: int, head_sha: str, required: list[str]
    ) -> tuple[list[dict[str, object]] | None, str]:
        return None, "empty local source"


@pytest.fixture(autouse=True)
def _no_host_local_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hd_mod, "_default_local_source", EmptyLocalSource, raising=False
    )
    monkeypatch.setattr(hd_mod, "_REQUIRED_CONTEXTS_CACHE", {}, raising=False)
    monkeypatch.setattr(hd_mod, "_MERGED_CHECKS_GREEN_CACHE", {}, raising=False)
