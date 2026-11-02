# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Node announcement step: a package announcer and the catalog built from it (OMN-21597).

Every installed node package announces its contracts, with their canonical content
hash, on a bus before any container exists, and the runtime reads the catalog built
from those announcements. The announcement record, the pure catalog and admission
live in omnibase_core; this node is the effect shell around them. See
``contract.yaml`` for the announce lifecycle.
"""

from omnimarket.nodes.node_package_announce_effect.handlers.handler_package_announce import (
    HandlerPackageAnnounce,
)

__all__ = ["HandlerPackageAnnounce"]
