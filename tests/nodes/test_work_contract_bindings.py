"""The same pure verdict crosses a real in-memory bus and a file binding."""

from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.work_contracts.bindings import finish_check
from omnimarket.handlers.work_contracts.loader import load_corpus
from omnimarket.nodes.node_model_work_contract_compute.handlers.checks import (
    HandlerModelWorkContract,
)
from omnimarket.nodes.node_model_work_contract_compute.models.model_check import (
    ModelCheckRequest,
    ModelCheckResult,
)

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"


@pytest.mark.asyncio
async def test_inmemory_and_file_binding_agree(tmp_path):
    contract = load_corpus(CORPUS, input_root=ROOT)[0]
    request = ModelCheckRequest(
        contract=contract,
        result=ModelCheckResult(contract_digest=contract.digest(), exit_code=0),
    )
    expected = HandlerModelWorkContract().handle(request)
    received = []

    async def consume(message):
        parsed = ModelCheckRequest.model_validate_json(message.value)
        received.append(HandlerModelWorkContract().handle(parsed))

    bus = EventBusInmemory()
    await bus.start()
    try:
        await bus.subscribe("work-contract-test", group_id="test", on_message=consume)
        await bus.publish(
            "work-contract-test",
            key=b"contract",
            value=request.model_dump_json(by_alias=True).encode(),
        )
        assert received == [expected]
    finally:
        await bus.close()
    path = tmp_path / "record.json"
    path.write_text(request.model_dump_json(by_alias=True))
    assert (
        HandlerModelWorkContract().handle(
            ModelCheckRequest.model_validate_json(path.read_text())
        )
        == expected
    )
    assert finish_check(contract, ROOT, exit_code=1).passed is False


def test_node_contract_and_overlay_are_pure_offline():
    node = ROOT / "src/omnimarket/nodes/node_model_work_contract_compute"
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    overlay = yaml.safe_load((node / "overlay.yaml").read_text())
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert overlay["enabled"] is False
    from importlib.metadata import entry_points

    assert any(e.name == node.name for e in entry_points(group="onex.nodes"))
