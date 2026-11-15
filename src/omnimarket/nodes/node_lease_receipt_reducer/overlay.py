# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lease overlay: which lease kinds exist and what each admits, none in code.

Found at ``<root>/node_lease_receipt_reducer/overlay.yaml`` under a root named
in ``ONEX_SKILL_OVERLAY_ROOTS``. Without one, the packaged contract's
``config`` section is the in-memory default: one kind, ``landing``, whose
subjects are landing PR keys and cause keys and whose admitted caller is the
landing controller. Nothing here has a default inside the models: an absent
key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.lease_receipt.model_lease_overlay import (
    ModelLeaseOverlay,
)

NODE_NAME = "node_lease_receipt_reducer"


def load_lease_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLeaseOverlay:
    """Read the deployment's lease overlay, or the packaged contract's default kind.

    An invalid selected overlay refuses composition: a broken overlay never
    silently narrows to, or widens past, what the packaged contract ships.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelLeaseOverlay, environ)
    return ModelLeaseOverlay.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    )
