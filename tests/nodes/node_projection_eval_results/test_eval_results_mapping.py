# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provider-to-engine table of the code try fold (OMN-24171 plan 5.1).

A mapped provider names the item's engine; a provider outside the table is refused as
``unmapped_provider``: nothing is stored, and the row is never stored as a local engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.enums.enum_eval_engine import EnumEvalEngine
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_mapping import (
    code_try_items,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    ModelEvalProjectionRequest,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRefusal,
)
from tests.nodes.node_projection_eval_results.support import (
    TENANT,
    at,
    code_event,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"


def _request(payload: dict[str, Any]) -> ModelEvalProjectionRequest:
    return ModelEvalProjectionRequest(
        topic=TOPIC_CODE_TRY,
        payload=payload,
        tenant_id=TENANT,
        observed_at=at(40),
        lane="dev",
        host="h202",
        install_revision="rev-1",
    )


@pytest.mark.parametrize(
    ("provider", "engine"),
    [
        ("local", EnumEvalEngine.LOCAL),
        ("glm", EnumEvalEngine.GLM),
        ("codex", EnumEvalEngine.CODEX),
        ("claude", EnumEvalEngine.CLAUDE),
        ("anthropic", EnumEvalEngine.CLAUDE),
    ],
)
def test_eval_results_mapping_a_mapped_provider_names_its_engine(
    provider: str, engine: EnumEvalEngine
) -> None:
    items, refused = code_try_items(
        _request(code_event(RUN, try_row(RUN, "t1", provider=provider)))
    )
    assert refused == ()
    (item,) = items
    assert item.engine == engine
    assert item.task_id == "t1"


@pytest.mark.parametrize("provider", ["gemini", "openrouter", ""])
def test_eval_results_mapping_an_unmapped_provider_is_refused(provider: str) -> None:
    items, refused = code_try_items(
        _request(code_event(RUN, try_row(RUN, "t1", provider=provider)))
    )
    assert items == ()
    (part,) = refused
    assert part.reason is EnumEvalRefusal.UNMAPPED_PROVIDER
    assert part.detail == provider


def test_eval_results_mapping_a_mixed_event_keeps_only_its_mapped_row() -> None:
    """A local row and a gemini row in one event: only the mapped row is stored."""
    items, refused = code_try_items(
        _request(
            code_event(RUN, try_row(RUN, "t1"), try_row(RUN, "t2", provider="gemini"))
        )
    )
    assert len(items) == 1
    item = items[0]
    assert item.engine == EnumEvalEngine.LOCAL
    assert item.task_id == "t1"
    assert len(refused) == 1
    part = refused[0]
    assert part.reason is EnumEvalRefusal.UNMAPPED_PROVIDER
    assert part.subject.startswith(f"{RUN}:t2:")
    assert part.detail == "gemini"
