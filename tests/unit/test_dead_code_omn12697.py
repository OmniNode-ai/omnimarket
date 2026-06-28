# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12697: dead re-export stubs have been removed.

Regression guard: these five local files duplicated classes that are already
canonical in omnibase_core.  They had zero importers within the omnimarket
package tree and are dead weight.  This test confirms they are gone and that
the canonical omnibase_core source continues to serve the symbols.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "omnimarket"

# --- files that must NOT exist after the cleanup ---
DELETED_FILES: list[str] = [
    "adapters/database.py",
    "models/delegation/wire/model_event_envelope.py",
    "models/delegation/wire/model_orchestrator_intents.py",
    "models/delegation/wire/model_routing_config.py",
    "models/delegation/wire/model_task_delegated_event.py",
]


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", DELETED_FILES)
def test_dead_stub_file_is_removed(rel_path: str) -> None:
    """Confirm each dead re-export stub no longer exists on disk."""
    full = SRC_ROOT / rel_path
    assert not full.exists(), (
        f"{rel_path} still exists — this is a dead re-export stub with zero "
        "callers in the omnimarket tree.  Delete it as part of OMN-12697."
    )


@pytest.mark.unit
def test_canonical_symbols_still_importable_from_omnibase_core() -> None:
    """Symbols that were duplicated locally are still canonical in omnibase_core."""
    # model_event_envelope duplicate
    from omnibase_core.models.delegation.wire.model_delegation_wire_envelope import (
        ModelDelegationEventEnvelope,
    )

    assert ModelDelegationEventEnvelope.__name__ == "ModelDelegationEventEnvelope"

    # model_orchestrator_intents duplicates
    from omnibase_core.models.delegation.wire.model_orchestrator_intents import (
        ModelInferenceIntent,
        ModelInferenceResponseData,
        ModelRoutingIntent,
    )

    assert ModelInferenceIntent.__name__ == "ModelInferenceIntent"
    assert ModelInferenceResponseData.__name__ == "ModelInferenceResponseData"
    assert ModelRoutingIntent.__name__ == "ModelRoutingIntent"

    # model_routing_config duplicates
    from omnibase_core.models.delegation.wire.model_routing_config import (
        ModelDelegationConfig,
        ModelRoutingTier,
        ModelTierModel,
    )

    assert ModelDelegationConfig.__name__ == "ModelDelegationConfig"
    assert ModelRoutingTier.__name__ == "ModelRoutingTier"
    assert ModelTierModel.__name__ == "ModelTierModel"

    # model_task_delegated_event duplicate
    from omnibase_core.models.delegation.wire.model_task_delegated_event import (
        ModelTaskDelegatedEvent,
    )

    assert ModelTaskDelegatedEvent.__name__ == "ModelTaskDelegatedEvent"

    # adapters/database.py re-exported DatabaseAdapter from omnibase_compat;
    # projection handlers already import it from the canonical projection path.
    from omnimarket.projection.protocol_database import DatabaseAdapter

    assert DatabaseAdapter is not None


@pytest.mark.unit
def test_wire_init_re_exports_from_omnibase_core() -> None:
    """models.delegation.wire.__init__ re-exports canonical symbols from omnibase_core."""
    from omnimarket.models.delegation.wire import (
        ModelDelegationConfig,
        ModelDelegationEventEnvelope,
        ModelInferenceIntent,
        ModelRoutingTier,
        ModelTaskDelegatedEvent,
        ModelTierModel,
    )

    # Verify they're the omnibase_core classes (not local duplicates).
    assert "omnibase_core" in ModelDelegationConfig.__module__, (
        f"ModelDelegationConfig came from {ModelDelegationConfig.__module__}, "
        "expected omnibase_core"
    )
    assert "omnibase_core" in ModelDelegationEventEnvelope.__module__, (
        f"ModelDelegationEventEnvelope came from {ModelDelegationEventEnvelope.__module__}"
    )
    assert "omnibase_core" in ModelInferenceIntent.__module__
    assert "omnibase_core" in ModelRoutingTier.__module__
    assert "omnibase_core" in ModelTaskDelegatedEvent.__module__
    assert "omnibase_core" in ModelTierModel.__module__
