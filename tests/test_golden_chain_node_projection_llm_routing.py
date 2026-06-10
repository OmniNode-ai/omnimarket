"""Golden-chain coverage for node_projection_llm_routing dashboard exposure."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_routing_decision_projection_api_contract_and_view_are_bound() -> None:
    contract_path = Path(
        "src/omnimarket/nodes/node_projection_llm_routing/contract.yaml"
    )
    migration_path = Path(
        "src/omnimarket/nodes/node_projection_llm_routing/migrations/"
        "0001_create_routing_dashboard_projection_view.sql"
    )

    contract = yaml.safe_load(contract_path.read_text())
    exposure = next(
        item
        for item in contract["projection_api"]["exposures"]
        if item["topic"] == "onex.snapshot.projection.routing-decision.v1"
    )

    assert exposure["table"] == "projection_routing_decision"
    assert exposure["json_columns"] == [
        "models",
        "intents",
        "task_presets",
        "routing_rules",
    ]
    assert "onex.snapshot.projection.routing-decision.v1" in contract["event_bus"][
        "publish_topics"
    ]
    assert "CREATE OR REPLACE VIEW projection_routing_decision" in (
        migration_path.read_text()
    )
