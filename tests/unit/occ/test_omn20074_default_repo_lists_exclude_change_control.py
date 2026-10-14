# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Health-report repo lists no longer name the change-control repository.

OMN-20074. The baseline PR probe, platform diagnostics and platform readiness each
carry a default list of repositories they report on. None of them reads change-control
state; the repository is a reporting row only, and the move off change control drops it.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_baseline_capture.handlers.probes import probe_github_prs
from omnimarket.nodes.node_platform_diagnostics.handlers import (
    dimension_checks as diagnostics_checks,
)
from omnimarket.nodes.node_platform_readiness.handlers import (
    dimension_checks as readiness_checks,
)

_RETIRED_FRAGMENT = "change_control"


@pytest.mark.parametrize(
    ("label", "repos"),
    [
        ("baseline probe", probe_github_prs._DEFAULT_OMNINODE_REPOS),
        ("platform diagnostics", diagnostics_checks._GITHUB_REPOS),
        ("platform readiness", readiness_checks._GITHUB_REPOS),
    ],
)
def test_default_repo_list_omits_change_control(
    label: str, repos: tuple[str, ...] | list[str]
) -> None:
    assert not [r for r in repos if _RETIRED_FRAGMENT in r], label
    assert "OmniNode-ai/omnimarket" in repos, f"{label}: positive control"
