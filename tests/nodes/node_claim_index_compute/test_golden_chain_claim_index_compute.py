# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The claim index node: contract, overlay refusal, answers over a synthetic ledger, bus golden chain.

Every ledger here is synthetic. The ledger location comes from the ``claim_index`` overlay; with no
overlay the node refuses naming the key.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_claim_index_compute as node_package
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.claim_index.model_claim_index_config import ModelClaimIndexConfig
from omnimarket.nodes.node_claim_index_compute.handlers.handler_claim_index import (
    HandlerClaimIndex,
)
from omnimarket.nodes.node_claim_index_compute.models.model_claim_index import (
    ModelClaimIndexRequest,
    ModelClaimIndexResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.claim-index-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.claim-index-completed.v1"
NOW = "2026-09-13T03:00:00Z"
LEDGER_ROWS = (
    "2026-09-13T00:00:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | first\n"
    "2026-09-13T00:30:00Z | CLAIM | lane=lane-b | ticket=OMN-2 | second\n"
    "2026-09-13T01:00:00Z | RELEASE | lane=lane-b | ticket=OMN-2 | done\n"
)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text(LEDGER_ROWS, encoding="utf-8")
    return path


def ask(config: ModelClaimIndexConfig, **fields: Any) -> ModelClaimIndexResult:
    return HandlerClaimIndex(config).handle(
        ModelClaimIndexRequest.model_validate(fields)
    )


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeClaimIndexCompute, handler_type)
    for side, model in (
        ("input_model", ModelClaimIndexRequest),
        ("output_model", ModelClaimIndexResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_claim_index_compute"].load() is node_package


def test_holder_names_the_lane_the_fence_and_the_claim_row(ledger: Path) -> None:
    result = ask(
        ModelClaimIndexConfig(ledger_path=str(ledger)),
        operation="holder",
        ticket="OMN-1",
        now=NOW,
    )
    assert result.holder is not None
    assert (result.holder.lane, result.holder.fence, result.holder.claim_line) == (
        "lane-a",
        1,
        1,
    )
    assert result.holder.state == "held"
    assert result.sources == ["ledger.md"]


def test_a_released_ticket_is_unclaimed(ledger: Path) -> None:
    result = ask(
        ModelClaimIndexConfig(ledger_path=str(ledger)),
        operation="holder",
        ticket="OMN-2",
        now=NOW,
    )
    assert result.holder is None
    assert result.admitted is True


def test_live_holders_lists_held_tickets_in_order(ledger: Path) -> None:
    result = ask(
        ModelClaimIndexConfig(ledger_path=str(ledger)),
        operation="live_holders",
        now=NOW,
    )
    assert [h.ticket for h in result.holders] == ["OMN-1"]
    assert result.stale_count == 0


def test_a_claim_on_a_held_ticket_is_refused_naming_the_holder(ledger: Path) -> None:
    config = ModelClaimIndexConfig(ledger_path=str(ledger))
    refused = ask(
        config, operation="refusal_for_claim", ticket="OMN-1", lane="lane-z", now=NOW
    )
    assert refused.admitted is False
    assert refused.refusal is not None
    assert "lane-a" in refused.refusal
    own = ask(
        config, operation="refusal_for_claim", ticket="OMN-1", lane="lane-a", now=NOW
    )
    assert own.admitted is True
    assert own.refusal is None


def test_a_push_carrying_a_stale_fence_is_refused(ledger: Path) -> None:
    config = ModelClaimIndexConfig(ledger_path=str(ledger))
    stale = ask(
        config,
        operation="refusal_for_push",
        ticket="OMN-1",
        lane="lane-a",
        fence=0,
        now=NOW,
    )
    assert stale.admitted is False
    current = ask(
        config,
        operation="refusal_for_push",
        ticket="OMN-1",
        lane="lane-a",
        fence=1,
        now=NOW,
    )
    assert current.admitted is True


def test_an_index_path_caches_the_derived_index(ledger: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "index.json"
    config = ModelClaimIndexConfig(ledger_path=str(ledger), index_path=str(cache))
    first = ask(config, operation="holder", ticket="OMN-1", now=NOW)
    assert cache.is_file()
    assert json.loads(cache.read_text())["tickets"]["OMN-1"]["lane"] == "lane-a"
    assert ask(config, operation="holder", ticket="OMN-1", now=NOW) == first


def test_an_absent_overlay_is_refused_naming_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    with pytest.raises(NodeOverlayError, match=r"claim_index\.ledger_path"):
        HandlerClaimIndex().handle(ModelClaimIndexRequest(operation="live_holders"))


def test_the_overlay_supplies_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "overlays"
    (root / "claim_index").mkdir(parents=True)
    (root / "claim_index" / "overlay.yaml").write_text(
        yaml.safe_dump({"ledger_path": str(ledger)})
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    result = HandlerClaimIndex().handle(
        ModelClaimIndexRequest(operation="holder", ticket="OMN-1", now=NOW)
    )
    assert result.holder is not None
    assert result.holder.lane == "lane-a"


def test_a_missing_ledger_fails_closed(tmp_path: Path) -> None:
    config = ModelClaimIndexConfig(ledger_path=str(tmp_path / "absent.md"))
    with pytest.raises(FileNotFoundError):
        ask(config, operation="live_holders", now=NOW)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "other"},
        {"operation": "holder"},
        {"operation": "holder", "ticket": "not-a-ticket"},
        {"operation": "refusal_for_claim", "ticket": "OMN-1"},
        {"operation": "live_holders", "now": "yesterday"},
        {"operation": "live_holders", "surprise": 1},
    ],
)
def test_error_chain_refuses_before_the_handler_runs(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelClaimIndexRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ledger: Path
) -> None:
    root = tmp_path / "overlays"
    (root / "claim_index").mkdir(parents=True)
    (root / "claim_index" / "overlay.yaml").write_text(
        yaml.safe_dump({"ledger_path": str(ledger)})
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    runtime = await _run(
        tmp_path, {"operation": "holder", "ticket": "OMN-1", "now": NOW}, "holder"
    )
    result = runtime.handler_result
    assert isinstance(result, ModelClaimIndexResult)
    assert result.holder is not None
    assert result.holder.lane == "lane-a"
    assert ModelClaimIndexResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {}, "empty")
    assert runtime.handler_result is None
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=tmp_path / "bad.json",
        state_root=tmp_path / "state-bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    (tmp_path / "bad.json").write_text(json.dumps({"operation": "holder"}))
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
