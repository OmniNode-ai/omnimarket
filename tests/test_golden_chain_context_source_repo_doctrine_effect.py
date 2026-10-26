# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the repository doctrine context source (OMN-21090).

A ``ModelContextSourceRequest`` command reaches the node through the runtime's
typed dispatch and the terminal event carries the named canonical shape section whole.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.nodes.node_context_source_repo_doctrine_effect import (
    NodeContextSourceRepoDoctrineEffect,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_context_source_repo_doctrine_effect/contract.yaml"
)
REPO_GUIDE = "# Repo\n\n## Adding A Node\n\n- Declare its handler in `contract.yaml`.\n\n## Other\n\nNot a shape.\n"


def test_context_source_repo_doctrine_golden_chain_contract_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    handler = NodeContextSourceRepoDoctrineEffect(env={})
    assert handler.terminal_event == contract["terminal_event"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert handler.terminal_event in contract["event_bus"]["publish_topics"]
    route = contract["handler_routing"]["handlers"][0]
    assert route["event_model"].endswith("ModelContextSourceRequest")
    assert contract["output_model"].endswith("ModelContextSourceResult")


def test_context_source_repo_doctrine_golden_chain_real_runtime_typed_dispatch(
    tmp_path: Path,
) -> None:
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    (tmp_path / "omnimarket").mkdir()
    (tmp_path / "omnimarket" / "CLAUDE.md").write_text(REPO_GUIDE, encoding="utf-8")
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    node = NodeContextSourceRepoDoctrineEffect(env={"OMNI_HOME": str(tmp_path)})
    callback = _make_dispatch_callback(node, event_model=route.event_model)
    request = ModelContextSourceRequest(
        factor=EnumContextFactor.CANONICAL_SHAPES,
        caller_scope=ModelContextCallerScope(repository="omnimarket"),
        access=ModelContextAccess(tenant_id="lab"),
        revision="2026-10-19T08:00:00Z",
    )
    dispatch = asyncio.run(
        callback(
            {
                "payload": request.model_dump(mode="json"),
                "correlation_id": "00000000-0000-4000-8000-000000021090",
            }
        )
    )
    assert dispatch is not None
    terminal = ModelContextSourceResult.model_validate(dispatch.output_events[0])
    assert terminal.status is EnumContextSourceStatus.RESOLVED
    (entry,) = terminal.entries
    assert entry.source_ref == "omnimarket/CLAUDE.md#Adding A Node"
    assert "Declare its handler in `contract.yaml`." in entry.text
