# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repo-state answer topics are provisioned at boot (OMN-24612).

`repo-state query` publishes its request on the installer contract's
repo-state-query-requested command topic and waits, group-less, for the answer on the completed
or failed topic, timing out after 30 s with "Topic ... not found in cluster metadata" when the
topic does not exist. The dev runtime's per-contract boot provisioning creates only the topics
_contract_provision_topics returns (subscribe, publish, dlq and projection_api topics), and the
repo-state serve contract declares no event_bus block, so nothing creates the two answers. The
node_host_installer_registry_units contract lists them (OMN-23991's pattern) beside the two serve
command topics; the control test is the one that fails the whole fix if the repo-state
contract ever provisions its own answers alone.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths

# omnibase_infra exports no public entry point for the boot provision set (grep of its auto_wiring package and
# tools: only handler_wiring._contract_provision_topics builds it), so this is the one private import; a rename
# upstream fails at collection, which is the signal to move this test to the public name.
from omnibase_infra.runtime.auto_wiring.handler_wiring import _contract_provision_topics

from omnimarket.models.broker_acl import (
    EnumAclResourceType,
    ModelBrokerAclCoverageRequest,
    ModelBrokerAclCoverageResult,
    ModelBrokerAclRow,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_coverage_gate import (
    HandlerBrokerAclCoverageGate,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    aggregate_declared_grants,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
REPO_STATE_DIR = NODES / "node_repo_state_query_effect"
HOST_INSTALLER_DIR = NODES / "node_host_installer_registry_units"


def _contract(node_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((node_dir / "contract.yaml").read_text(encoding="utf-8"))


def _boot_provision_topics(node_dir: Path) -> set[str]:
    """The topics the dev runtime's boot provisioning creates for one node contract."""
    manifest = discover_contracts_from_paths([node_dir / "contract.yaml"])
    # A discovery that finds nothing must fail here, not return an empty set that a control then accepts.
    assert len(manifest.contracts) == 1, f"no contract discovered at {node_dir}"
    provisioned: set[str] = set()
    for contract in manifest.contracts:
        provisioned.update(_contract_provision_topics(contract))
    return provisioned


def _installer_boot_provision_topics() -> set[str]:
    return _boot_provision_topics(HOST_INSTALLER_DIR)


@pytest.mark.parametrize("role", ["success", "failure"])
def test_each_answer_topic_is_boot_provisioned(role: str) -> None:
    terminal = _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"]

    assert terminal[role] in _installer_boot_provision_topics()


def test_answer_topics_equal_the_published_events() -> None:
    contract = _contract(REPO_STATE_DIR)
    terminal = set(contract["runtime_dispatch"]["terminal_events"].values())
    published = {event["topic"] for event in contract["published_events"]}

    assert terminal == published


def test_answer_topics_are_declared_externally_consumed_and_never_subscribed() -> None:
    installer = _contract(HOST_INSTALLER_DIR)
    terminal = set(
        _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"].values()
    )

    assert terminal <= set(installer["externally_consumed_topics"])
    assert not terminal & set(installer["event_bus"]["subscribe_topics"])


def test_the_repo_state_contract_alone_provisions_nothing() -> None:
    # The repo-state serve contract declares no event_bus block, so its boot provisioning
    # returns no topics at all; this is why the installer contract lists the two answers
    # beside its serve command topics.
    assert _boot_provision_topics(REPO_STATE_DIR) == set(), (
        "the repo-state contract now declares its own publish_topics, which is the truthful "
        "declaration; follow-up: remove the two answer rows from the installer contract's "
        "event_bus publish_topics, broker_acl_grants and externally_consumed_topics, and drop this control"
    )


class _NoContracts:
    contracts: tuple[object, ...] = ()


def test_a_discovery_that_finds_no_contract_fails_the_boot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two reads above and the control below loop over the discovered contracts; with no contract
    # the loop never runs and the control's empty set would pass without asking the runtime anything.
    monkeypatch.setattr(
        f"{__name__}.discover_contracts_from_paths", lambda _paths: _NoContracts()
    )

    with pytest.raises(AssertionError):
        _boot_provision_topics(HOST_INSTALLER_DIR)
    with pytest.raises(AssertionError):
        _boot_provision_topics(REPO_STATE_DIR)


def _answer_topics() -> set[str]:
    return set(
        _contract(REPO_STATE_DIR)["runtime_dispatch"]["terminal_events"].values()
    )


def _gate_over_installer_contract(
    tmp_path: Path, *, drop_grants: Any = None
) -> ModelBrokerAclCoverageResult:
    """Run the broker permission coverage gate over a copy of the installer contract (OMN-24005).

    The base is the same contract without the two answer topics, which is the base branch the change
    added them to; `drop_grants` receives the grants block and returns the block to keep (None keeps it).
    """
    nodes_root = tmp_path / "nodes"
    node_dir = nodes_root / HOST_INSTALLER_DIR.name
    node_dir.mkdir(parents=True)
    contract = _contract(HOST_INSTALLER_DIR)
    if drop_grants is None:
        shutil.copy(HOST_INSTALLER_DIR / "contract.yaml", node_dir / "contract.yaml")
    else:
        kept = drop_grants(contract.pop("broker_acl_grants", []))
        if kept is not None:
            contract["broker_acl_grants"] = kept
        (node_dir / "contract.yaml").write_text(
            yaml.safe_dump(contract), encoding="utf-8"
        )
    uses = load_topic_uses(nodes_root)
    answers = _answer_topics()
    base_uses = tuple(use for use in uses if use.topic not in answers)
    assert len(uses) - len(base_uses) == len(answers), "both answers are publish uses"
    rows = tuple(
        ModelBrokerAclRow(**row)
        for row in aggregate_declared_grants(nodes_root)
        if row["resource_type"] == EnumAclResourceType.TOPIC.value
    )
    return HandlerBrokerAclCoverageGate().handle(
        ModelBrokerAclCoverageRequest(
            topic_uses=uses, base_topic_uses=base_uses, declared_rows=rows
        )
    )


def test_the_gate_covers_the_two_added_answer_uses(tmp_path: Path) -> None:
    result = _gate_over_installer_contract(tmp_path)

    assert (result.status, result.new_uses, result.uncovered_new_uses) == (
        "covered",
        2,
        0,
    )


def test_the_gate_refuses_the_installer_contract_without_its_grants_block(
    tmp_path: Path,
) -> None:
    result = _gate_over_installer_contract(tmp_path, drop_grants=lambda _block: None)

    assert result.status == "refused"
    assert {finding.detail.split(": ")[1] for finding in result.findings} == (
        _answer_topics()
    )


@pytest.mark.parametrize("operation", ["WRITE", "DESCRIBE"])
def test_the_gate_accepts_nothing_narrower_than_write_and_describe_for_a_publish_entry(
    tmp_path: Path, operation: str
) -> None:
    # A publish_topics entry needs both rows (the render contract's derive_publish_operations), so the runtime's
    # WRITE row cannot be narrowed to CREATE or dropped without the gate refusing the entry.
    def without(block: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **principal,
                "grants": [
                    g for g in principal["grants"] if g["operation"] != operation
                ],
            }
            for principal in block
        ]

    result = _gate_over_installer_contract(tmp_path, drop_grants=without)

    assert result.status == "refused"
    assert result.uncovered_new_uses == 2


def test_the_done_gate_check_of_ac3_queries_both_hosts() -> None:
    """OMN-24612 AC3 stays open until repo-state query answers on h053 and h201."""
    contract = yaml.safe_load(
        (Path(__file__).parents[3] / "contracts" / "OMN-24612.yaml").read_text()
    )
    commands = [
        check["check_value"]
        for item in contract["dod_evidence"]
        for check in item["checks"]
        if "AC3" in item.get("binds_ac", [])
        and item.get("execution_scope") == "local_done_gate"
    ]
    assert len(commands) == 1
    assert "--host h053" in commands[0]
    assert "--host h201" in commands[0]
