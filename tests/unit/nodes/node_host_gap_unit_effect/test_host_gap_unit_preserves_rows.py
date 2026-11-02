# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Updating one host's row preserves the other hosts sharing a table."""

from omnimarket.models.model_host_profile import EnumGapUnit
from tests.unit.nodes.node_host_gap_unit_effect.test_host_gap_unit import (
    FakeHost,
    bare,
    load_host,
    prepush_target_files,
    run,
)


def test_host_gap_unit_preserves_peer_rows_and_replaces_its_own_stale_row() -> None:
    profile = load_host("host-a")
    target, row = next(iter(prepush_target_files(profile).items()))
    peer = "host-other\tleave-this-row-alone"
    stale = f"{profile.host}\told-values"
    seen = bare(profile).model_copy(
        update={"workroot_present": True, "prepush_rows": {target: (peer, stale)}}
    )
    result = run(profile, FakeHost(seen), EnumGapUnit.G2_PREPUSH)
    written = next(a for a in result.planned if a.path == target)
    assert written.content.splitlines() == [peer, row]
