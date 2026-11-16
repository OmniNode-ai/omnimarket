# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's merged-history read no longer names the retired sibling repository.

The merged-ticket skip reads each listed canonical clone's default branches for
``evidence(`` merges inside ``since_days``. The retired sibling repository is
archived, so its clone is not listed: the read names the remaining clones, in
order, over the same window and refs.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_merged_history import (
    load_merged_history,
)

pytestmark = pytest.mark.unit

_RETIRED = "omnibase" + "_" + "compat"


def test_the_merged_history_read_lists_no_clone_of_the_retired_repository() -> None:
    settings = load_merged_history({})
    assert _RETIRED not in settings.repos
    assert settings.repos == (
        "omnimarket",
        "omnibase_core",
        "omnibase_infra",
        "omnibase_spi",
        "omniclaude",
        "omniintelligence",
        "omnimemory",
    )
    assert settings.since_days == 120
    assert settings.evidence_prefix == "evidence("
