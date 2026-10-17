# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the default repository lists no longer name the change-control repo."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_git_query_mirror_effect import git_mirror
from omnimarket.nodes.node_merge_sweep_compute import __main__ as merge_sweep_main
from omnimarket.nodes.node_merge_sweep_compute import consumer as merge_sweep_consumer
from omnimarket.nodes.node_pr_snapshot_effect.models import model_pr_snapshot_input

_LISTS = {
    "git_mirror.REGISTRY_REPOS": git_mirror.REGISTRY_REPOS,
    "merge_sweep __main__._DEFAULT_REPOS": merge_sweep_main._DEFAULT_REPOS,
    "merge_sweep consumer._DEFAULT_REPOS": merge_sweep_consumer._DEFAULT_REPOS,
    "pr_snapshot DEFAULT_REPOS": model_pr_snapshot_input.DEFAULT_REPOS,
}


@pytest.mark.parametrize("name", sorted(_LISTS))
def test_default_repo_list_omits_change_control(name: str) -> None:
    assert not [repo for repo in _LISTS[name] if repo.endswith("_change_control")]


@pytest.mark.parametrize("name", sorted(_LISTS))
def test_default_repo_list_keeps_the_product_repos(name: str) -> None:
    """Positive control: the list is real and still names the fleet."""
    assert "OmniNode-ai/omnimarket" in _LISTS[name]
    assert "OmniNode-ai/omnibase_core" in _LISTS[name]
