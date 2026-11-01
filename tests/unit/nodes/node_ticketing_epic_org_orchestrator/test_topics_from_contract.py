# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The epic-org handler takes its topics from the node contract by injection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_ticketing_epic_org_orchestrator.handlers import (
    handler_ticketing_epic_org,
)
from omnimarket.nodes.node_ticketing_epic_org_orchestrator.handlers.handler_ticketing_epic_org import (
    HandlerTicketingEpicOrg,
    load_ticketing_epic_org_topics,
)
from omnimarket.nodes.node_ticketing_epic_org_orchestrator.models import (
    ModelTicketingEpicOrgTopics,
)

_NODE_DIR = Path(handler_ticketing_epic_org.__file__).resolve().parents[1]
_CONTRACT = _NODE_DIR / "contract.yaml"
_HANDLER_SOURCE = Path(handler_ticketing_epic_org.__file__)


@pytest.mark.unit
def test_loader_binds_every_field_to_a_declared_publish_topic() -> None:
    topics = load_ticketing_epic_org_topics(_CONTRACT)
    declared = contract_publish_topics(_CONTRACT)
    bound = topics.model_dump()
    assert set(bound.values()) == set(declared)
    for field, topic in bound.items():
        assert topic.endswith(f".{field.replace('_', '-')}.v1")


@pytest.mark.unit
def test_handler_defaults_to_the_contract_topics() -> None:
    assert HandlerTicketingEpicOrg().topics == load_ticketing_epic_org_topics(_CONTRACT)


@pytest.mark.unit
def test_injected_topics_win_over_the_contract() -> None:
    declared = load_ticketing_epic_org_topics(_CONTRACT)
    injected = ModelTicketingEpicOrgTopics(
        **{name: f"{value}-overlay" for name, value in declared.model_dump().items()}
    )
    assert HandlerTicketingEpicOrg(topics=injected).topics == injected


@pytest.mark.unit
@pytest.mark.parametrize("drop", ["linear-epic-create", "epic-org-completed"])
def test_loader_refuses_a_contract_missing_a_topic(tmp_path: Path, drop: str) -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    raw["event_bus"]["publish_topics"] = [
        topic for topic in raw["event_bus"]["publish_topics"] if drop not in topic
    ]
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        load_ticketing_epic_org_topics(contract)


@pytest.mark.unit
def test_loader_refuses_an_ambiguous_contract(tmp_path: Path) -> None:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    duplicate = next(
        topic
        for topic in raw["event_bus"]["publish_topics"]
        if "linear-epic-create" in topic
    )
    raw["event_bus"]["publish_topics"].append(duplicate.replace("omnimarket", "other"))
    contract = tmp_path / "contract.yaml"
    contract.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        load_ticketing_epic_org_topics(contract)


@pytest.mark.unit
def test_contracts_and_di_reports_no_literal_topic_in_the_handler() -> None:
    result = ValidatorContractsAndDi().validate([_HANDLER_SOURCE])
    assert result.metadata is not None
    assert result.metadata.files_processed == 1
    offenders = [
        str(issue)
        for issue in result.issues
        if issue.rule_name in {"literal_topic", "unparseable_file"}
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.unit
def test_contract_publish_topics_are_the_four_declared_events() -> None:
    assert set(contract_publish_topics(_CONTRACT)) == {
        "onex.cmd.omnimarket.linear-epic-create.v1",
        "onex.cmd.omnimarket.linear-ticket-reparent.v1",
        "onex.evt.omnimarket.ticketing-epic-org-completed.v1",
        "onex.evt.omnimarket.ticketing-epic-org-proposal-ready.v1",
    }
