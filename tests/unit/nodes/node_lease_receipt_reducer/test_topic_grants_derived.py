# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lease reducer's broker grants come from its contract, never by hand (OMN-24484).

The contract names three topics — one command, two terminals — and its
``broker_acl_grants`` block declares the runtime principal's rows on them:
READ+DESCRIBE on the command, WRITE+DESCRIBE on each terminal. A principal with
no grant on a topic is refused by the broker, so these tests hold the only path
a grant may take:

* the fleet-wide aggregate (``aggregate_declared_grants``) holds exactly those
  six rows for the placeholder principal, and nothing else on the three topics;
* a principals overlay that names ``node_lease_receipt_reducer`` derives the
  same topic rows for its login through ``HandlerBrokerAclReconcile`` — the
  derive node reads the contract's ``event_bus``, so the rows follow the node
  that owns the topics — plus the consumer-group prefix rows a subscribing node
  needs;
* with no overlay nothing is derived: a lane's login gets no grant by silence,
  and the lease topics carry only the contract's own rows. The rows follow the
  contract block itself: remove ``broker_acl_grants`` from a copy and the lease
  topics leave the aggregate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    contract_config,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    aggregate_declared_grants,
    contract,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

pytestmark = pytest.mark.unit

NODE = "node_lease_receipt_reducer"
RUNTIME = "${ONEX_ACL_RUNTIME_PRINCIPAL}"
LANE_PRINCIPAL = "User:lease-lane"
KEYS = ("principal", "resource_type", "pattern", "name", "operation")

CONTRACT_PATH = (
    Path(__file__).parents[4] / "src" / "omnimarket" / "nodes" / NODE / "contract.yaml"
)


def _lease_contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _lease_topics() -> set[str]:
    """The contract's three topics, read from its runtime_dispatch, never retyped."""
    dispatch = _lease_contract()["runtime_dispatch"]
    return {dispatch["command_topic"], *dispatch["terminal_events"].values()}


def _lease_declared_rows() -> list[dict[str, str]]:
    """The grant rows the lease contract's ``broker_acl_grants`` block declares."""
    return [
        {"principal": entry["principal"], **grant}
        for entry in _lease_contract()["broker_acl_grants"]
        for grant in entry["grants"]
    ]


def _expected_derived_rows(principal: str) -> set[tuple[str, str, str, str, str]]:
    """What the derive node yields for a principal that runs the lease node.

    The operations come from the render contract's config (a publisher needs
    WRITE+DESCRIBE, a subscriber READ+DESCRIBE plus its consumer-group prefix);
    the topics come from the lease contract's own ``event_bus``.
    """
    config = contract_config()
    bus = _lease_contract()["event_bus"]
    rows: set[tuple[str, str, str, str, str]] = set()
    for topic in bus["subscribe_topics"]:
        rows.update(
            (principal, "TOPIC", "LITERAL", topic, operation)
            for operation in config["derive_subscribe_operations"]
        )
    for topic in bus["publish_topics"]:
        rows.update(
            (principal, "TOPIC", "LITERAL", topic, operation)
            for operation in config["derive_publish_operations"]
        )
    prefix = config["derive_group_prefix_template"].format(node=NODE)
    rows.update(
        (principal, "GROUP", "PREFIXED", prefix, operation)
        for operation in config["derive_group_operations"]
    )
    return rows


def _env(tmp_path: Path, principal: str = LANE_PRINCIPAL) -> dict[str, str]:
    """A principals overlay whose login runs the lease node, the way a lane declares one."""
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    target = tmp_path / scope
    target.mkdir(parents=True)
    document = {
        "scope": scope,
        "ticket": "OMN-24484",
        "why": "a test scope",
        "principals": [{"principal": principal, "role": "a login", "nodes": [NODE]}],
    }
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {**ENV, "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def _handle(env: dict[str, str], remote: FakeRemote, mode: str):
    return HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode=mode)
    )


def _live(remote: FakeRemote) -> set[tuple[str, str, str, str, str]]:
    return {
        (g.principal, g.resource_type, g.pattern, g.name, g.operation)
        for g in remote.live
    }


# ------------------------------------------------------- what the fleet declares
def test_the_aggregate_holds_exactly_the_six_contract_rows() -> None:
    declared = _lease_declared_rows()
    assert len(declared) == 6
    assert {row["principal"] for row in declared} == {RUNTIME}
    # the lease contract's own rows, compared as computed from its YAML
    on_lease_topics = {
        tuple(row[key] for key in KEYS)
        for row in aggregate_declared_grants()
        if row["name"] in _lease_topics()
    }
    assert on_lease_topics == {tuple(row[key] for key in KEYS) for row in declared}


# --------------------------------------------- derivation through the owning node
def test_an_overlay_naming_the_node_derives_the_lease_rows_on_apply(
    tmp_path: Path,
) -> None:
    remote = FakeRemote()
    result = _handle(_env(tmp_path), remote, "apply")
    assert result.exit_code == 0
    expected = _expected_derived_rows(LANE_PRINCIPAL)
    assert result.derived == len(expected)
    # exactly what the derive node yields for the login — no more, no less
    assert {row for row in _live(remote) if row[0] == LANE_PRINCIPAL} == expected
    # the derived topic rows are the contract's six with the principal swapped:
    # the grants follow the contract's own declaration, only the login differs
    assert {row[1:] for row in expected if row[1] == "TOPIC"} == {
        tuple(row[key] for key in KEYS[1:]) for row in _lease_declared_rows()
    }


def test_an_overlay_naming_the_node_reports_the_lease_rows_missing_on_dry_run(
    tmp_path: Path,
) -> None:
    remote = FakeRemote()
    result = _handle(_env(tmp_path), remote, "dry_run")
    assert not remote.live
    assert result.exit_code == 1
    expected = _expected_derived_rows(LANE_PRINCIPAL)
    assert result.derived == len(expected)
    missing = {
        tuple(line.split())
        for line in result.missing_rows
        if line.startswith(LANE_PRINCIPAL)
    }
    assert missing == expected


# --------------------------------------------------------------- the controls
def test_no_principals_overlay_derives_nothing_for_the_lease_topics() -> None:
    """No overlay: nothing is derived, so a lane's login gets no grant by silence."""
    remote = FakeRemote()
    result = _handle(ENV, remote, "apply")
    assert result.exit_code == 0
    assert result.derived == 0
    assert result.uncovered_topics == 0
    live = _live(remote)
    assert not any(row[0] == LANE_PRINCIPAL for row in live)
    # the lease topics carry only the contract's own six, rendered for the runtime
    runtime = ENV["ONEX_ACL_RUNTIME_PRINCIPAL"]
    declared = {
        (runtime, *(row[key] for key in KEYS[1:])) for row in _lease_declared_rows()
    }
    assert {row for row in live if row[3] in _lease_topics()} == declared


def test_the_lease_rows_follow_the_contract_block_not_the_node_directory(
    tmp_path: Path,
) -> None:
    """A copy of the contract with ``broker_acl_grants`` removed lists no lease topics."""
    copied = tmp_path / NODE / "contract.yaml"
    copied.parent.mkdir(parents=True)
    copied.write_text(CONTRACT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    with_block = {row["name"] for row in aggregate_declared_grants(tmp_path)}
    assert _lease_topics() <= with_block
    loaded = yaml.safe_load(copied.read_text(encoding="utf-8"))
    del loaded["broker_acl_grants"]
    copied.write_text(yaml.safe_dump(loaded), encoding="utf-8")
    assert not (
        _lease_topics() & {row["name"] for row in aggregate_declared_grants(tmp_path)}
    )
