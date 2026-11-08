# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared pull request fixtures: the example application with a planted module, in memory."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.model_code_graph import ModelShadowPullRequest
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from tests.nodes.node_code_graph_effect.code_graph_support import APP, CORE, REF

PLANTED = {"src/example_app/planted.py": "PLANTED = 1\n"}
WIRED = {
    "src/example_app/nodes/node_a/extra.py": "EXTRA = 1\n",
    "src/example_app/nodes/node_a/handler.py": APP[
        "src/example_app/nodes/node_a/handler.py"
    ]
    + "from .extra import EXTRA\n",
}
CLEAN_SHA = "1" * 40
PLANTED_SHA = "2" * 40
ALLOWED_SHA = "3" * 40
NOT_YET_WIRED = {"src/example_app/not_yet_wired/node_b/handler.py": "B = 1\n"}


def pull_request(number: int, sha: str) -> ModelShadowPullRequest:
    return ModelShadowPullRequest(
        repo="example_app",
        number=number,
        base_ref=REF,
        head_ref=f"pr-{number}",
        head_sha=sha,
    )


def memory_source(extra_heads: Mapping[str, Mapping[str, str]]) -> InMemoryRepoSource:
    """The example repositories at ``origin/main``, and each head sha as the base plus its files."""
    refs: dict[str, tuple[str, Mapping[str, str]]] = {REF: ("a" * 40, APP)}
    for sha, files in extra_heads.items():
        refs[sha] = (sha, {**APP, **files})
    return InMemoryRepoSource(
        {"example_core": {REF: ("c" * 40, CORE)}, "example_app": refs}
    )
