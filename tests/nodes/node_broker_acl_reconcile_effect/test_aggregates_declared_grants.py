# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared ACL aggregation and broker reconciliation without a remote broker."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    driver_broker_acl as driver,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    aggregate_declared_grants,
    contract,
    declared_grants,
    render_declaration,
)

pytestmark = pytest.mark.unit

PUBLISHER = "${ONEX_ACL_DESIRED_PUBLISHER_PRINCIPAL}"
RECONCILER = "${ONEX_ACL_HOST_RECONCILER_PRINCIPAL}"
DESIRED = "onex.evt.omnimarket.host-desired-state.v1"
ENV = {
    "ONEX_LAB_BROKER_SSH_TARGET": "broker",
    "ONEX_ACL_GUARD_PRINCIPAL": "User:guard",
    "ONEX_ACL_FLEET_PACKAGE": "fleet",
    "ONEX_ACL_DESIRED_PUBLISHER_PRINCIPAL": "User:publisher",
    "ONEX_ACL_LAYER_REQUEST_PRINCIPAL": "User:requester",
    "ONEX_ACL_HOST_RECONCILER_PRINCIPAL": "User:reconciler",
    "ONEX_ACL_DEV_ROLL_CAPTURE_PRINCIPAL": "User:dev-roll-capture",
}
HEADER = (
    "PRINCIPAL HOST RESOURCE-TYPE RESOURCE-NAME RESOURCE-PATTERN-TYPE "
    "OPERATION PERMISSION ERROR\n"
)


def _row(
    principal: str, resource: str, pattern: str, name: str, operation: str
) -> dict[str, str]:
    return {
        "principal": principal,
        "resource_type": resource,
        "pattern": pattern,
        "name": name,
        "operation": operation,
    }


def _grants() -> list[driver.AclGrant]:
    rendered = render_declaration({"rows": aggregate_declared_grants()}, ENV)
    return [driver.AclGrant(**row) for row in rendered["rows"]]


def _connection() -> driver.AclConnection:
    return driver.AclConnection(
        lane="dev",
        host="broker",
        compose_project="broker-stack",
        broker_container="broker",
        superuser_source_container="credentials",
        ssh_target="broker",
    )


class FakeRemote:
    """Model broker rows from actual shell-quoted rpk create arguments."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.commands: list[list[str]] = []
        self.live: set[driver.AclGrant] = set()

    def __call__(self, target: str, script: str) -> str:
        assert target == "broker"
        self.scripts.append(script)
        for line in script.splitlines():
            if not line.startswith("docker exec "):
                continue
            argv = shlex.split(line)
            args = argv[argv.index("rpk") + 1 : argv.index("-X")]
            self.commands.append(args)
            if args[:3] != ["security", "acl", "create"]:
                continue
            resource, name = "CLUSTER", "kafka-cluster"
            for kind, flag in (
                ("TOPIC", "--topic"),
                ("GROUP", "--group"),
                ("TRANSACTIONAL_ID", "--transactional-id"),
            ):
                if flag in args:
                    resource, name = kind, args[args.index(flag) + 1]
            self.live.add(
                driver.AclGrant(
                    args[args.index("--allow-principal") + 1],
                    resource,
                    args[args.index("--resource-pattern-type") + 1].upper(),
                    name,
                    args[args.index("--operation") + 1].upper(),
                )
            )
        return HEADER + "".join(
            f"{g.principal} * {g.resource_type} {g.name} {g.pattern} {g.operation} ALLOW\n"
            for g in sorted(self.live)
        )


def test_aggregates_declared_grants() -> None:
    rows = aggregate_declared_grants()
    own = declared_grants(contract()["broker_acl_declaration"])
    assert len(own) == 10
    assert all(row in rows for row in own)
    assert _row(PUBLISHER, "TOPIC", "LITERAL", DESIRED, "WRITE") in rows
    assert (
        _row(
            PUBLISHER,
            "TRANSACTIONAL_ID",
            "LITERAL",
            "host-desired-state-publisher",
            "WRITE",
        )
        in rows
    )
    assert (
        _row(
            RECONCILER,
            "TRANSACTIONAL_ID",
            "PREFIXED",
            "host-observed-state/",
            "DESCRIBE",
        )
        in rows
    )
    assert (
        _row(
            RECONCILER,
            "TOPIC",
            "LITERAL",
            "onex.evt.omnimarket.host-observed-state.v1",
            "WRITE",
        )
        in rows
    )
    keys = [
        tuple(
            row[key]
            for key in ("principal", "resource_type", "pattern", "name", "operation")
        )
        for row in rows
    ]
    assert keys == sorted(set(keys))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [{"principal": "User:broken", "grants": "bad"}],
        [{"principal": "User:broken", "grants": [{"resource_type": "UNKNOWN"}]}],
    ],
)
def test_aggregates_declared_grants_malformed_contract(
    tmp_path: Path, bad: object
) -> None:
    good = tmp_path / "good" / "contract.yaml"
    good.parent.mkdir()
    block = [
        {
            "principal": "User:test",
            "grants": [
                {
                    "resource_type": "GROUP",
                    "pattern": "PREFIXED",
                    "name": "test.",
                    "operation": "READ",
                }
            ],
        }
    ]
    good.write_text(
        yaml.safe_dump({"broker_acl_grants": block + block}), encoding="utf-8"
    )
    broken = tmp_path / "broken" / "contract.yaml"
    broken.parent.mkdir()
    broken.write_text(yaml.safe_dump({"broker_acl_grants": bad}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"broken.*contract.yaml"):
        aggregate_declared_grants(tmp_path)
    broken.unlink()
    rows = aggregate_declared_grants(tmp_path)
    assert _row("User:test", "GROUP", "PREFIXED", "test.", "READ") in rows
    assert len(rows) == len(declared_grants(contract()["broker_acl_declaration"])) + 1


def test_transactional_id_grants_applied() -> None:
    remote = FakeRemote()
    grants = _grants()
    report = driver.apply(grants, _connection(), "apply", remote)
    created = [
        args for args in remote.commands if args[:3] == ["security", "acl", "create"]
    ]
    for principal, pattern, name in (
        ("User:publisher", "LITERAL", "host-desired-state-publisher"),
        ("User:reconciler", "PREFIXED", "host-observed-state/"),
    ):
        for operation in ("WRITE", "DESCRIBE"):
            assert [
                "security",
                "acl",
                "create",
                "--allow-principal",
                principal,
                "--operation",
                operation.lower(),
                "--transactional-id",
                name,
                "--resource-pattern-type",
                pattern.lower(),
            ] in created
        assert [
            "security",
            "acl",
            "create",
            "--allow-principal",
            principal,
            "--operation",
            "idempotent_write",
            "--cluster",
            "--resource-pattern-type",
            "literal",
        ] in created
    assert any("--transactional-id" in args and "prefixed" in args for args in created)
    assert remote.live == set(grants)
    assert (
        report.splitlines()[-1]
        == f"lane=dev declared={len(grants)} live={len(grants)} missing=0 undeclared=0"
    )
    assert (
        "MISSING User:publisher TRANSACTIONAL_ID LITERAL host-desired-state-publisher WRITE"
        in report
    )
    assert len(remote.scripts) == 3
    assert all(
        "docker inspect credentials" in script
        and "sasl.mechanism=SCRAM-SHA-256" in script
        for script in remote.scripts
    )


def test_desired_topic_write_publisher_only() -> None:
    writers = {
        row["principal"]
        for row in aggregate_declared_grants()
        if row["resource_type"] == "TOPIC"
        and row["operation"] in {"WRITE", "ALL"}
        and (
            (row["pattern"] == "LITERAL" and row["name"] in {DESIRED, "*"})
            or (row["pattern"] == "PREFIXED" and DESIRED.startswith(row["name"]))
        )
    }
    assert writers == {PUBLISHER}


def test_declared_topics() -> None:
    topics = contract()["broker_topics"]
    assert topics == [
        {
            "name": "onex.cmd.omnimarket.host-layer-requested.v1",
            "cleanup_policy": "delete",
            "retention_ms": 604800000,
            "partitions": 1,
        },
        {
            "name": "onex.evt.omnimarket.host-layer-decided.v1",
            "cleanup_policy": "compact",
            "partitions": 1,
        },
        {"name": DESIRED, "cleanup_policy": "compact", "partitions": 1},
        {
            "name": "onex.evt.omnimarket.host-observed-state.v1",
            "cleanup_policy": "compact",
            "partitions": 1,
        },
    ]
    assert type(topics[0]["retention_ms"]) is int
    assert all(
        re.fullmatch(r"onex\.(cmd|evt)\.omnimarket\.[a-z0-9-]+\.v1", topic["name"])
        for topic in topics
    )


def test_dry_run_makes_no_create() -> None:
    remote = FakeRemote()
    report = driver.apply(_grants(), _connection(), "dry_run", remote)
    assert len(remote.scripts) == 1
    assert all(args[:3] == ["security", "acl", "list"] for args in remote.commands)
    assert not remote.live
    assert "MISSING " in report
    assert "CREATE " in report


def test_driver_resource_types() -> None:
    assert {"TOPIC", "GROUP", "CLUSTER", "TRANSACTIONAL_ID"} == driver.RESOURCE_TYPES
    for kind in driver.RESOURCE_TYPES:
        name = "kafka-cluster" if kind == "CLUSTER" else "resource"
        grant = driver.AclGrant("User:test", kind, "LITERAL", name, "DESCRIBE")
        assert driver.rpk_create_args(grant)[:3] == ["security", "acl", "create"]
    for kind, operation in (
        ("UNKNOWN", "READ"),
        ("TOPIC", "DELETE"),
        ("TOPIC", "IDEMPOTENT_WRITE"),
        ("TRANSACTIONAL_ID", "READ"),
    ):
        with pytest.raises(
            ValueError, match=r"invalid resource_type|allowed on|allows WRITE"
        ):
            driver.AclGrant("User:test", kind, "LITERAL", "resource", operation)
    for principal in ("User:*", "test"):
        with pytest.raises(ValueError, match="principal"):
            driver.AclGrant(principal, "TOPIC", "LITERAL", "resource", "WRITE")


def test_driver_resource_types_parse_and_diff() -> None:
    output = HEADER + (
        "User:test * TRANSACTIONAL_ID resource PREFIXED WRITE ALLOW\n"
        "User:test * TOPIC topic LITERAL READ DENY\n"
        "User:test localhost TOPIC topic LITERAL WRITE ALLOW\n"
    )
    grant = driver.AclGrant(
        "User:test", "TRANSACTIONAL_ID", "PREFIXED", "resource", "WRITE"
    )
    assert driver.parse_rpk_acl_list(output) == {grant}
    unrelated = driver.AclGrant("User:other", "GROUP", "LITERAL", "group", "READ")
    assert driver.diff(
        frozenset(), frozenset({grant, unrelated}), frozenset({"User:test"})
    ) == ((), (grant,))
    with pytest.raises(ValueError, match="header"):
        driver.parse_rpk_acl_list("unexpected output")
    with pytest.raises(ValueError, match="row"):
        driver.parse_rpk_acl_list(HEADER + "incomplete row\n")


@pytest.mark.parametrize("mode", ["dry_run", "apply"])
def test_handler_aggregates_declared_grants_default_driver(mode: str) -> None:
    remote = FakeRemote()
    request = ModelBrokerAclReconcileRequest.model_validate({"mode": mode})
    result = HandlerBrokerAclReconcile(environ=ENV, remote_runner=remote).handle(
        request
    )
    assert result.declared == len(aggregate_declared_grants())
    assert result.exit_code == (1 if mode == "dry_run" else 0)
    assert result.missing == (result.declared if mode == "dry_run" else 0)
    assert bool(remote.live) == (mode == "apply")


def test_handler_aggregates_declared_grants_requires_all_variables() -> None:
    remote = FakeRemote()
    env = {
        key: value
        for key, value in ENV.items()
        if key
        not in {
            "ONEX_ACL_HOST_RECONCILER_PRINCIPAL",
            "ONEX_ACL_LAYER_REQUEST_PRINCIPAL",
        }
    }
    result = HandlerBrokerAclReconcile(environ=env, remote_runner=remote).handle(
        ModelBrokerAclReconcileRequest(mode="apply")
    )
    assert result.exit_code == 2
    assert "ONEX_ACL_HOST_RECONCILER_PRINCIPAL" in result.error
    assert "ONEX_ACL_LAYER_REQUEST_PRINCIPAL" in result.error
    assert remote.scripts == []


def test_transactional_id_grants_applied_requires_readback_and_is_idempotent() -> None:
    grant = driver.AclGrant(
        "User:publisher", "TRANSACTIONAL_ID", "LITERAL", "publisher", "WRITE"
    )
    remote = FakeRemote()
    remote.live.add(grant)
    report = driver.apply([grant], _connection(), "apply", remote)
    assert "declared=1 live=1 missing=0 undeclared=0" in report
    assert len(remote.scripts) == 1
    assert all(args[:3] == ["security", "acl", "list"] for args in remote.commands)

    scripts: list[str] = []

    def never_persists(target: str, script: str) -> str:
        scripts.append(script)
        return HEADER

    report = driver.apply([grant], _connection(), "apply", never_persists)
    assert len(scripts) == 3
    assert report.count("declared=1 live=0 missing=1 undeclared=0") == 2


def test_driver_resource_types_live_wildcards_are_reported_but_not_created() -> None:
    live = driver.parse_rpk_acl_list(HEADER + "User:test * TOPIC * LITERAL ALL ALLOW\n")
    missing, undeclared = driver.diff(frozenset(), live, frozenset({"User:test"}))
    assert not missing
    assert len(undeclared) == 1
    with pytest.raises(ValueError, match="invalid operation"):
        driver.rpk_create_args(undeclared[0])


def test_driver_resource_types_load_declaration(tmp_path: Path) -> None:
    declaration = {
        "lane": "dev",
        "host": "broker",
        "compose_project": "broker-stack",
        "broker_container": "broker",
        "superuser_source_container": "credentials",
        "principals": [
            {
                "principal": "User:producer",
                "grants": [
                    {
                        "resource_type": "TRANSACTIONAL_ID",
                        "pattern": "PREFIXED",
                        "name": "producer/",
                        "operation": "WRITE",
                    }
                ],
            }
        ],
    }
    path = tmp_path / "declaration.yaml"
    path.write_text(yaml.safe_dump(declaration), encoding="utf-8")
    loaded = driver.load_declaration(path, "broker")
    assert loaded.connection == _connection()
    assert loaded.grants == (
        driver.AclGrant(
            "User:producer", "TRANSACTIONAL_ID", "PREFIXED", "producer/", "WRITE"
        ),
    )
    path.write_text("principals: [", encoding="utf-8")
    with pytest.raises(ValueError, match="valid YAML"):
        driver.load_declaration(path, "broker")


def test_handler_aggregates_declared_grants_remote_failure() -> None:
    def failed_remote(target: str, script: str) -> str:
        raise RuntimeError("remote ACL command failed")

    result = HandlerBrokerAclReconcile(environ=ENV, remote_runner=failed_remote).handle(
        ModelBrokerAclReconcileRequest(mode="apply")
    )
    assert result.exit_code == 2
    assert result.error == "remote ACL command failed"
