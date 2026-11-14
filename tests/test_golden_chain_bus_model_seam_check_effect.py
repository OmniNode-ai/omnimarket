# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus-model seam check: typed command through the in-memory runtime."""

from __future__ import annotations

import json
import uuid
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_bus_model_seam_check_effect.models.model_bus_model_seam_check_result import (
    ModelBusModelSeamCheckResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_bus_model_seam_check_effect")))
    / "contract.yaml"
)
TOPIC = "onex.evt.seamfix.produced.v1"


def _make_tree(tmp_path: Path, pkg: str, consumer_extra: str = "forbid") -> Path:
    """Write a synthetic src/<pkg> tree with one producer and one consumer node."""
    root = tmp_path / "src" / pkg
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_producer").mkdir(parents=True, exist_ok=True)
    (root / "nodes" / "node_seam_consumer").mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("")
    (root / "models" / "__init__.py").write_text("")
    (root / "models" / "model_producer.py").write_text(
        "from pydantic import BaseModel\n\n\n"
        "class ModelSeamProducer(BaseModel):\n    a: int\n    b: int = 0\n"
    )
    (root / "models" / "model_consumer.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n\n\n"
        "class ModelSeamConsumer(BaseModel):\n"
        f'    model_config = ConfigDict(extra="{consumer_extra}")\n'
        "    a: int\n"
    )
    producer_contract = {
        "terminal_event": TOPIC,
        "output_model": {
            "name": "ModelSeamProducer",
            "module": f"{pkg}.models.model_producer",
        },
        "published_events": [{"event_type": "ModelSeamProducer", "topic": TOPIC}],
    }
    consumer_contract = {
        "input_model": {
            "name": "ModelSeamConsumer",
            "module": f"{pkg}.models.model_consumer",
        },
        "handler_routing": {
            "handlers": [
                {
                    "topic": TOPIC,
                    "event_model": {
                        "name": "ModelSeamConsumer",
                        "module": f"{pkg}.models.model_consumer",
                    },
                }
            ]
        },
    }
    (root / "nodes" / "node_seam_producer" / "contract.yaml").write_text(
        yaml.safe_dump(producer_contract)
    )
    (root / "nodes" / "node_seam_consumer" / "contract.yaml").write_text(
        yaml.safe_dump(consumer_contract)
    )
    return tmp_path


def test_golden_chain_declares_the_terminal_topic():
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.bus-model-seam-check-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_golden_chain_typed_request_returns_the_seam_verdict(
    tmp_path, monkeypatch, refused
):
    pkg = "seamfix_" + uuid.uuid4().hex[:8]
    tree = _make_tree(tmp_path, pkg, consumer_extra="forbid" if refused else "ignore")
    monkeypatch.syspath_prepend(tmp_path / "src")
    policy = tmp_path / "seam_policy.yaml"
    policy.write_text(
        yaml.safe_dump({"strict_models": [], "external_producers": [], "pins": []})
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"repo_root": str(tree), "package": pkg, "policy_path": str(policy)})
    )
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelBusModelSeamCheckResult)
    assert result.admitted is (not refused)
    assert result.exit_code == int(refused)
    assert result.pair_count == 1
    if refused:
        assert result.pairs[0].refused == ("b",)
        assert result.output_lines[0].startswith("REFUSED")


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"repo_root": str(tmp_path), "unexpected": True}))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
