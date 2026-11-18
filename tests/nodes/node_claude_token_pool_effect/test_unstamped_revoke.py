# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A revoke that exists only as an unstamped source mark is stamped at sync time (OMN-23887).

The rule: a revoke the marks source reports without a revoke time is stamped with the time of the sync (the source
snapshot time). A revoke is stale only when the same fingerprint was limited after that stamp plus the overlay margin,
so a success older than the sync-time stamp never clears it. Every token is fake and built at run time; the pool is the
real handler over a temporary store and pool file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    fingerprint,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
)

from .test_stale_revoke import (
    LABEL,
    LIMITED_AFTER,
    FakeSource,
    Rig,
    fake_token,
)

pytestmark = pytest.mark.unit

SYNC_TIME_STAMP = "2026-11-10T12:00:00Z"  # T0 of the rig, the source snapshot time
AFTER_STAMP_AND_MARGIN = "2026-11-10T12:05:00Z"


def unstamped_source(limited_at: str) -> FakeSource:
    return FakeSource(
        (
            ModelRotationMark(
                label=LABEL,
                revoked=True,
                revoked_at=None,
                fingerprint=fingerprint(fake_token(LABEL)),
                limited_at=limited_at,
            ),
        )
    )


def test_golden_unstamped_source_revoke_is_stamped_at_sync_time(
    tmp_path: Path,
) -> None:
    # the only success the source holds is from before the sync, so the revoke stays, stamped at the sync time
    rig = Rig(tmp_path, "h101", source=unstamped_source("2026-11-01T00:00:00Z"))
    result = rig.tick()
    assert rig.entry(LABEL)["revoked_at"] == SYNC_TIME_STAMP
    assert result.revokes_kept == (LABEL,)
    assert result.revokes_cleared == ()


def test_control_a_success_older_than_the_sync_time_stamp_does_not_clear(
    tmp_path: Path,
) -> None:
    # limited the day before the sync: older than the stamp, so it proves nothing about this revoke
    rig = Rig(tmp_path, "h101", source=unstamped_source(LIMITED_AFTER))
    result = rig.tick()
    assert rig.entry(LABEL)["revoked_at"] == SYNC_TIME_STAMP
    assert result.revokes_cleared == ()
    assert result.revokes_kept == (LABEL,)


def test_unstamped_revoke_with_a_success_after_the_stamp_is_cleared(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h101", source=unstamped_source(AFTER_STAMP_AND_MARGIN))
    result = rig.tick()
    assert result.revokes_cleared == (LABEL,)
    assert rig.entry(LABEL)["revoked_at"] is None
