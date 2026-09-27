# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effects runtime groups OCC companions by ticket by default (OMN-16336).

omnimarket#2940 shipped the per-ticket batch path with every default reading
``off``. The merge-sweep receipt repair builds its fix command without naming a
grouping, and the deploy-gate route passed none, so both kept minting one
companion per product PR whatever the publisher asked. These tests fail the
moment a runtime seam falls back to the per-PR path by default again.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from uuid import uuid4

import pytest

from omnimarket.events.occ_companion import EnumOccBatchMode
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    handler_pr_lifecycle_fix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)


@pytest.mark.unit
def test_command_without_a_grouping_is_ticket_batched() -> None:
    """The merge-sweep receipt repair builds the command without the field."""
    command = ModelPrLifecycleFixCommand.model_validate(
        {
            "correlation_id": str(uuid4()),
            "pr_number": 42,
            "repo": "OmniNode-ai/omnimarket",
            "block_reason": EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
            "requested_at": "2026-09-27T10:00:00+00:00",
        }
    )
    assert command.occ_batch_mode is EnumOccBatchMode.TICKET


@pytest.mark.unit
def test_emitter_and_adapter_seams_default_to_ticket() -> None:
    seams: list[Callable[..., object]] = [
        OccCompanionEmitter.autobind_evidence_source,
        OccCompanionEmitter.create_occ_contract,
        handler_pr_lifecycle_fix.ProtocolOccAutobindAdapter.autobind_evidence_source,
        handler_pr_lifecycle_fix._NoopOccAutobindAdapter.autobind_evidence_source,
    ]
    for seam in seams:
        default = inspect.signature(seam).parameters["batch_mode"].default
        assert default is EnumOccBatchMode.TICKET, seam.__qualname__


@pytest.mark.unit
def test_the_private_core_has_no_grouping_default() -> None:
    """Every caller of the core names its grouping; none inherits one."""
    parameter = inspect.signature(OccCompanionEmitter._emit_companion_sync).parameters[
        "batch_mode"
    ]
    assert parameter.default is inspect.Parameter.empty
