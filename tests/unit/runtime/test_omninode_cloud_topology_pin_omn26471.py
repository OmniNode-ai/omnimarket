# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pinned omnibase_infra topology declares omninode_cloud in every profile.

The tenant identity nodes (T3, T7) resolve ``omninode_cloud`` and its
``tenant_identity_writer`` login binding through the typed topology shipped in
omnibase_infra. Sibling of ``test_topology_strict_wiring_omn15655.py``.
"""

from __future__ import annotations

import pytest
from omnibase_infra.topology import SUPPORTED_TOPOLOGY_PROFILES, load_topology_profile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("profile", sorted(SUPPORTED_TOPOLOGY_PROFILES))
def test_omninode_cloud_resolves_with_writer_binding(profile: str) -> None:
    database = load_topology_profile(profile).databases["omninode_cloud"]

    assert database.physical_name == "omninode_cloud"
    binding = database.bindings["tenant_identity_writer"]
    assert binding.dsn_env == "TENANT_IDENTITY_DATABASE_DSN"
    assert database.principals[binding.principal].login is True
