# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-bypass ratchet (OMN-24432): golden and error chain through the in-memory runtime.

Follows tests/test_golden_chain_bus_model_seam_check_effect.py: a typed request goes through ``RuntimeLocal`` on the in-memory bus
and the handler's typed verdict is read back. Golden: a count at its baseline is admitted, a rise is refused. Error: a request
the model refuses ends FAILED with no verdict.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.models.effect_bypass import (
    EnumBypassFinding,
    ModelBypassRatchetVerdict,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_effect_bypass_ratchet_compute")))
    / "contract.yaml"
)


def _request(now: int, baseline: int) -> dict[str, object]:
    return {
        "inventory": {
            "repository": "demo",
            "files_scanned": 2,
            "counts": {"counts": {"gh_cli": now}},
        },
        "baseline": {
            "schema_version": 1,
            "ticket": "OMN-24432",
            "repositories": {"demo": {"counts": {"gh_cli": baseline}}},
        },
    }


def _runtime(tmp_path: Path, request: dict[str, object]) -> RuntimeLocal:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )


def test_golden_chain_declares_the_terminal_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    terminal = contract["runtime_dispatch"]["terminal_events"]
    assert terminal["success"] in contract["event_bus"]["publish_topics"]
    assert terminal["failure"] in contract["event_bus"]["publish_topics"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rose", [False, True])
async def test_golden_chain_typed_request_returns_the_ratchet_verdict(
    tmp_path: Path, rose: bool
) -> None:
    runtime = _runtime(tmp_path, _request(now=4 + int(rose), baseline=4))
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelBypassRatchetVerdict)
    assert result.ok is (not rose)
    assert (EnumBypassFinding.CATEGORY_ROSE in result.findings) is rose


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, {"inventory": {"repository": "demo"}, "extra": 1})
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
