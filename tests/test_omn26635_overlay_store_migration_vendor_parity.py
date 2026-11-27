# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay store migrations are byte-identical to the omnibase_infra copies.

The Sibling Release Compatibility Gate compares each node migration with the copy
omnibase_infra vendors under docker/migrations/forward/nodes/<node>/. Migration
0003 here had drifted from that copy (it lacked the ADD COLUMN IF NOT EXISTS
repair statements), so the gate failed at the pinned revision and at every
published release. The blob ids below are the git blob ids of the files carried
by omnibase_infra release v0.38.84 and by the pinned dev tag, spelled in this
repository so the check runs everywhere with no sibling clone.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_overlay_store/migrations"
)

# git blob ids of omnibase_infra v0.38.84 docker/migrations/forward/nodes/
# node_projection_overlay_store/<name>
VENDORED_BLOBS = {
    "0003_overlay_store_revision_provenance_audit.sql": "60156bb7dc7cdda27c50688d7b6ff1cb07721b63",
    "0004_grant_omninode_runtime_overlay_store_write.sql": "21dce056bbccaf3e787804f4a2554575f07d0bc1",
}


def _git_blob_id(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.mark.parametrize("name", sorted(VENDORED_BLOBS))
def test_migration_is_byte_identical_to_the_omnibase_infra_release(name: str) -> None:
    assert _git_blob_id(MIGRATIONS / name) == VENDORED_BLOBS[name]
