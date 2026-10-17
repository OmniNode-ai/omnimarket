# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_context_invariants_select_compute: the invariants tier of the context service (OMN-21126).

Pure COMPUTE node: admits the active invariants tagged with a profile's scopes,
verbatim from the registry at a pinned commit, and lists superseded entries as
excluded. No token budget and no pack hash.
"""

from omnimarket.nodes.node_context_invariants_select_compute.handlers import (
    HandlerContextInvariantsSelect,
)

__all__ = ["HandlerContextInvariantsSelect"]
