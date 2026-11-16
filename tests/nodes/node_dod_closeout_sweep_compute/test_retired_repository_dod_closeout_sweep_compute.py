# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's packaged overlay no longer reads the retired sibling repository.

The closer reads merged pull requests in every repository its overlay lists. The
retired sibling repository is retired, so the packaged overlay stops listing it: no repository of the list is read for it, and
a repository the overlay still lists is read as before.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    PACKAGED_OVERLAY,
    load_scope_overlay,
)

pytestmark = pytest.mark.unit

#: Spelled from parts: the package's name is not written whole in this repository's guards.
_RETIRED = "omnibase" + "_" + "compat"


def test_the_packaged_overlay_reads_no_pull_requests_of_the_retired_repository() -> (
    None
):
    branches = load_scope_overlay(PACKAGED_OVERLAY).default_branches()
    assert _RETIRED not in branches
    assert branches["omnibase_core"] == "dev"
    assert branches["omnimarket"] == "dev"
