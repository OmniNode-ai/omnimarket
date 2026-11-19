# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reconcile mode removes exactly the undeclared rows of the node's own logins, and nothing else (OMN-24566).

The incident in small: two managed lab logins hold eight rows (write and describe on the landing status topic, read and
describe on the branch claim check's terminal topic) that no declaration holds any more. Today's apply mode lists them
and leaves them; reconcile removes them, inside the contract's removal scope, for the named topics, at the expected count.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.broker_acl_reconcile import (
    ModelBrokerAclReconcileRequest,
    ModelBrokerAclReconcileResult,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers import (
    driver_broker_acl as driver,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    contract,
)
from tests.nodes.node_broker_acl_reconcile_effect.test_aggregates_declared_grants import (
    ENV,
    FakeRemote,
)

pytestmark = pytest.mark.unit

NODE = "node_broker_acl_overlay_render_compute"
STATUS_TOPIC = "onex.evt.github.pr-status.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.branch-claim-check-completed.v1"
LOGINS = ("User:lab-login-1", "User:lab-login-2")


def grant(
    login: str,
    name: str,
    operation: str,
    resource_type: str = "TOPIC",
    pattern: str = "LITERAL",
) -> driver.AclGrant:
    return driver.AclGrant(login, resource_type, pattern, name, operation)


def line(row: driver.AclGrant) -> str:
    return (
        f"{row.principal} {row.resource_type} {row.pattern} {row.name} {row.operation}"
    )


STALE = sorted(
    grant(login, topic, operation)
    for login in LOGINS
    for topic, operations in (
        (STATUS_TOPIC, ("WRITE", "DESCRIBE")),
        (TERMINAL_TOPIC, ("READ", "DESCRIBE")),
    )
    for operation in operations
)
STALE_LINES = tuple(line(row) for row in STALE)
NAMES = (STATUS_TOPIC, TERMINAL_TOPIC)


class FakeBroker(FakeRemote):
    """A FakeRemote that also executes `rpk security acl delete`, and insists it names every field."""

    def __init__(self, live: Iterable[driver.AclGrant] = ()) -> None:
        super().__init__()
        self.live = set(live)

    def __call__(self, target: str, script: str) -> str:
        for text in script.splitlines():
            if not text.startswith("docker exec "):
                continue
            argv = shlex.split(text)
            args = argv[argv.index("rpk") + 1 : argv.index("-X")]
            if args[:3] != ["security", "acl", "delete"]:
                continue
            assert args[args.index("--allow-host") + 1] == "*"
            assert "--no-confirm" in args
            resource, name = "CLUSTER", "kafka-cluster"
            for kind, flag in (("TOPIC", "--topic"), ("GROUP", "--group")):
                if flag in args:
                    resource, name = kind, args[args.index(flag) + 1]
            row = driver.AclGrant(
                args[args.index("--allow-principal") + 1],
                resource,
                args[args.index("--resource-pattern-type") + 1].upper(),
                name,
                args[args.index("--operation") + 1].upper(),
            )
            assert row in self.live, f"delete of a row the broker does not hold: {row}"
            self.live.remove(row)
        return super().__call__(target, script)

    def deletes(self) -> list[list[str]]:
        return [c for c in self.commands if c[:3] == ["security", "acl", "delete"]]

    def creates(self) -> list[list[str]]:
        return [c for c in self.commands if c[:3] == ["security", "acl", "create"]]


def _write(root: Path, scope: str, document: dict[str, Any]) -> None:
    target = root / scope
    target.mkdir(parents=True, exist_ok=True)
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def scope_overlay(
    rows: Iterable[driver.AclGrant], scope: str = "broker_acls_example"
) -> dict[str, Any]:
    """A scoped broker permission overlay (ModelBrokerAclScopeOverlay) declaring `rows`."""
    by_login: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_login.setdefault(row.principal, []).append(
            {
                "resource_type": row.resource_type,
                "pattern": row.pattern,
                "name": row.name,
                "operation": row.operation,
            }
        )
    return {
        "scope": scope,
        "ticket": "OMN-24566",
        "why": "a test scope",
        "operator_ssh_target": "broker-host-alias",
        "operator_command": "example-apply-tool apply --declaration {declaration} --ssh-target {ssh_target}",
        "declaration_path": "<overlays clone>/overlays/{scope}/overlay.yaml",
        "lane": "dev",
        "host": "dev-lane-broker",
        "compose_project": "omnibase-infra",
        "broker_container": "omnibase-infra-redpanda",
        "superuser_source_container": "omnibase-infra-redpanda-sasl-enable",
        "principals": [
            {"principal": login, "role": "a test login", "grants": grants}
            for login, grants in by_login.items()
        ],
    }


def make_env(
    tmp_path: Path, declared: Iterable[driver.AclGrant] = ()
) -> dict[str, str]:
    """Overlay roots holding the principals overlay (the two managed logins) and, when rows are given, one scoped overlay."""
    scope = contract()["broker_acl_reconcile"]["derive_principals_scope"]
    _write(
        tmp_path,
        scope,
        {
            "scope": scope,
            "ticket": "OMN-24566",
            "why": "two test logins running one node",
            "roles": [
                {
                    "name": "test_role",
                    "role": "a role of two lab logins",
                    "nodes": [NODE],
                    "principals": list(LOGINS),
                }
            ],
        },
    )
    declared = list(declared)
    if declared:
        _write(tmp_path, "broker_acls_example", scope_overlay(declared))
    return {**ENV, "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def handle(
    env: dict[str, str], broker: Any, **request: Any
) -> ModelBrokerAclReconcileResult:
    return HandlerBrokerAclReconcile(environ=env, remote_runner=broker).handle(
        ModelBrokerAclReconcileRequest(**request)
    )


def test_golden_dry_run_lists_exactly_the_stale_rows_and_changes_nothing(
    tmp_path: Path,
) -> None:
    broker = FakeBroker(STALE)
    result = handle(make_env(tmp_path), broker, mode="dry_run", remove_names=NAMES)
    assert result.would_remove_rows == STALE_LINES
    assert result.exit_code == 1
    assert broker.live == set(STALE)
    assert not broker.deletes()
    assert not broker.creates()


def test_golden_reconcile_removes_exactly_the_stale_rows_and_reads_back(
    tmp_path: Path,
) -> None:
    broker = FakeBroker(STALE)
    result = handle(
        make_env(tmp_path),
        broker,
        mode="reconcile",
        remove_names=NAMES,
        expected_removals=len(STALE),
    )
    assert result.error == ""
    assert result.exit_code == 0
    assert result.removed == len(STALE) == 8
    assert set(result.removed_rows) == set(STALE_LINES)
    assert not broker.live & set(STALE), "an undeclared row survived the reconcile"
    assert len(broker.deletes()) == 8
    # Readback: the declared rows were added as today, and nothing undeclared is left for those logins.
    assert broker.creates()
    assert (result.missing, result.undeclared) == (0, 0)
    assert result.derived


def test_failing_control_apply_mode_leaves_the_stale_rows(tmp_path: Path) -> None:
    """Today's code: apply adds what is missing and lists the undeclared rows, and every one of them survives."""
    broker = FakeBroker(STALE)
    result = handle(make_env(tmp_path), broker, mode="apply")
    assert broker.live >= set(STALE)
    assert result.undeclared == len(STALE)
    assert not broker.deletes()


def test_failing_control_named_break_split_never_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break: the split finds nothing removable. The golden reconcile's expectation (8 removed) can then not hold."""

    def nothing_removable(
        undeclared: Iterable[driver.AclGrant], *_: Any, **__: Any
    ) -> Any:
        return (), tuple(undeclared), ()

    monkeypatch.setattr(driver, "split_undeclared", nothing_removable)
    broker = FakeBroker(STALE)
    result = handle(
        make_env(tmp_path),
        broker,
        mode="reconcile",
        remove_names=NAMES,
        expected_removals=8,
    )
    assert result.removed != 8
    assert result.exit_code == 2
    assert broker.live == set(STALE)


def test_error_chain_expected_count_mismatch_changes_nothing(tmp_path: Path) -> None:
    broker = FakeBroker(STALE)
    result = handle(
        make_env(tmp_path),
        broker,
        mode="reconcile",
        remove_names=NAMES,
        expected_removals=7,
    )
    assert result.exit_code == 2
    assert "expected 7 removals, found 8; nothing changed" in result.error
    assert broker.live == set(STALE)
    assert not broker.deletes()
    assert not broker.creates()


def test_error_chain_request_without_expected_or_names_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(mode="reconcile")
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(mode="reconcile", remove_names=NAMES)
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(mode="reconcile", expected_removals=8)
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(
            mode="dry_run", remove_names=NAMES, expected_removals=8
        )
    with pytest.raises(ValidationError):
        ModelBrokerAclReconcileRequest(mode="apply", remove_names=NAMES)


def test_error_chain_unreadable_broker_changes_nothing(tmp_path: Path) -> None:
    calls: list[str] = []

    def unreachable(target: str, script: str) -> str:
        calls.append(script)
        raise RuntimeError("remote ACL command failed (exit 255)")

    result = handle(
        make_env(tmp_path),
        unreachable,
        mode="reconcile",
        remove_names=NAMES,
        expected_removals=8,
    )
    assert result.exit_code == 2
    assert result.removed is None
    assert "exit 255" in result.error
    assert len(calls) == 1, "only the listing was attempted"


def test_error_chain_unreadable_declaration_removes_nothing(tmp_path: Path) -> None:
    """A declaration that cannot be read is never read as empty: no overlay roots, or one broken scope, stops it."""
    broker = FakeBroker(STALE)
    no_roots = {
        k: v for k, v in make_env(tmp_path).items() if k != "ONEX_SKILL_OVERLAY_ROOTS"
    }
    result = handle(
        no_roots, broker, mode="reconcile", remove_names=NAMES, expected_removals=8
    )
    assert result.exit_code == 2
    assert "no overlay roots are configured" in result.error

    env = make_env(tmp_path)
    (tmp_path / "broker_acls_broken").mkdir()
    (tmp_path / "broker_acls_broken" / "overlay.yaml").write_text(
        "scope: [", encoding="utf-8"
    )
    result = handle(
        env, broker, mode="reconcile", remove_names=NAMES, expected_removals=8
    )
    assert result.exit_code == 2
    assert "broker_acls_broken" in result.error
    assert not broker.commands
    assert broker.live == set(STALE)


def test_scope_overlay_declared_rows_are_never_removed(tmp_path: Path) -> None:
    kept = STALE[0]
    declared = handle(
        make_env(tmp_path, declared=[kept]),
        FakeBroker(STALE),
        mode="dry_run",
        remove_names=NAMES,
    )
    assert line(kept) not in declared.would_remove_rows
    assert len(declared.would_remove_rows) == len(STALE) - 1
    # Control: without the overlay the same row is a removal candidate.
    control = handle(
        make_env(tmp_path / "bare"),
        FakeBroker(STALE),
        mode="dry_run",
        remove_names=NAMES,
    )
    assert line(kept) in control.would_remove_rows


def test_scope_rows_outside_scope_are_reported_not_removed(tmp_path: Path) -> None:
    collaborator = "User:collaborator-login"
    outside = [
        grant(LOGINS[0], "some-group", "READ", resource_type="GROUP"),
        grant(LOGINS[0], "local.other.topic.v1", "READ"),
        grant(collaborator, STATUS_TOPIC, "WRITE"),
    ]
    # The collaborator holds one declared row in a scoped overlay, so its login is listed; its other row is not ours.
    declared = [grant(collaborator, "onex.evt.collaborator.v1", "READ")]
    broker = FakeBroker(outside)
    result = handle(
        make_env(tmp_path, declared=declared),
        broker,
        mode="reconcile",
        remove_names=(STATUS_TOPIC,),
        expected_removals=0,
    )
    assert result.exit_code == 0
    assert set(result.out_of_scope_rows) == {line(row) for row in outside}
    assert not broker.deletes()
    assert set(outside) <= broker.live


def test_scope_names_not_asked_for_are_held(tmp_path: Path) -> None:
    asked = grant(LOGINS[0], STATUS_TOPIC, "WRITE")
    other = grant(LOGINS[0], "onex.evt.other.topic.v1", "WRITE")
    broker = FakeBroker([asked, other])
    result = handle(
        make_env(tmp_path),
        broker,
        mode="reconcile",
        remove_names=(STATUS_TOPIC,),
        expected_removals=1,
    )
    assert result.exit_code == 0
    assert result.removed_rows == (line(asked),)
    assert result.held_rows == (line(other),)
    assert other in broker.live
    assert asked not in broker.live


def test_removal_command_names_every_field(tmp_path: Path) -> None:
    only = grant(LOGINS[0], STATUS_TOPIC, "WRITE")
    broker = FakeBroker([only])
    result = handle(
        make_env(tmp_path),
        broker,
        mode="reconcile",
        remove_names=(STATUS_TOPIC,),
        expected_removals=1,
    )
    assert result.exit_code == 0
    assert broker.deletes() == [
        [
            "security",
            "acl",
            "delete",
            "--allow-principal",
            LOGINS[0],
            "--allow-host",
            "*",
            "--operation",
            "write",
            "--topic",
            STATUS_TOPIC,
            "--resource-pattern-type",
            "literal",
            "--no-confirm",
        ]
    ]
