# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Duplicate tier text keeps only chunk refs the built pack names (OMN-23629).

The pack builder drops a content duplicate (OMN-21125), so the orchestrator must
hand the renderer only the chunk refs of the chunks that survived: a ref for a
dropped chunk names no chunk of the pack, fails the render ``UNKNOWN_CHUNK_REF``
and the build ``INVALID_PROFILE``.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
)
from omnimarket.models.context_pack_render import (
    EnumContextPackRenderFailure,
    EnumContextPackRenderStatus,
    ModelContextPackRenderRequest,
)
from omnimarket.nodes.node_context_pack_render_compute.handlers.handler_context_pack_render import (
    HandlerContextPackRender,
)
from tests.nodes.node_context_pack_render_compute.conftest import (
    chunk,
    identity,
    pack,
    ref,
)

from .conftest import FixtureSource, command, orchestrator, sources

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

SAME_TEXT = "same text"


def test_duplicate_tier_text_across_two_tiers_still_builds() -> None:
    """Two tiers answering the same text build once, with the text rendered once."""
    bound = sources(
        factors=(),
        canonical_shapes=FixtureSource(
            EnumContextFactor.CANONICAL_SHAPES, text=SAME_TEXT
        ),
        architecture_patterns=FixtureSource(
            EnumContextFactor.ARCHITECTURE_PATTERNS, text=SAME_TEXT
        ),
    )
    built = orchestrator(bound).handle(command(profile_name="delegation_payload_local"))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    assert not built.errors
    text = "\n".join(m.text for m in built.messages)
    assert text.count(SAME_TEXT) == 1


def test_a_chunk_ref_outside_the_pack_fails_the_render_unknown_chunk_ref() -> None:
    """The failure the unfiltered refs produced, injected at the renderer seam."""
    kept = chunk(EnumContextFactor.CANONICAL_SHAPES, SAME_TEXT)
    stray = chunk(EnumContextFactor.ARCHITECTURE_PATTERNS, SAME_TEXT)
    result = HandlerContextPackRender().handle(
        ModelContextPackRenderRequest(
            pack=pack((kept,)),
            chunk_refs=(ref(kept), ref(stray)),
            identity=identity(),
        )
    )
    assert result.status is EnumContextPackRenderStatus.FAILED
    assert result.failure_class is EnumContextPackRenderFailure.UNKNOWN_CHUNK_REF
