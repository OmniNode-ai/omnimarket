"""The broker ACL reconcile node declares a DELETE row for every reapable guard family and drives the reconcile command."""

import re
import subprocess
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.broker_acl_reconcile import (
    ModelBrokerAclReconcileRequest,
    ModelBrokerAclReconcileResult,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
    contract,
    declared_grants,
    render_declaration,
)

pytestmark = pytest.mark.unit

# Tails a name of each reapable family can continue with after its row's prefix.
_HEX = "0123456789ab"
_TAILS = (
    "",
    "1",
    _HEX,
    f"{_HEX}.t",
    f"{_HEX}.cap",
    f"{_HEX}.consume.v1",
    f"5_run_{_HEX}.consume.v1",
    f"1.{_HEX}",
    f".{_HEX}",
    f"0_run_{_HEX}.consume.v1",
    f"a-{'0' * 32}",
    f"0.{_HEX}",
    "00000000-0000-0000-0000-000000000000",
)


_ENV = {
    "ONEX_LAB_BROKER_SSH_TARGET": "broker",
    "ONEX_ACL_RECONCILE_COMMAND": "reconcile-cmd --flag",
    "ONEX_ACL_GUARD_PRINCIPAL": "User:guard",
    "ONEX_ACL_FLEET_PACKAGE": "pkg",
}


def _rendered() -> dict:
    return render_declaration(contract()["broker_acl_declaration"], _ENV)


def _guard_patterns() -> list[str]:
    text = (
        resources.files("omnimarket.nodes.node_consumer_group_guard_effect")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    patterns = yaml.safe_load(text)["consumer_group_guard"]["reap"]["reapable_patterns"]
    return list(patterns)


def _delete_rows(declaration: dict) -> list[str]:
    return [
        row["name"]
        for row in declared_grants(declaration)
        if row["operation"] == "DELETE"
        and row["resource_type"] == "GROUP"
        and row["pattern"] == "PREFIXED"
    ]


def _covered(pattern: str, rows: Sequence[str]) -> bool:
    return any(re.fullmatch(pattern, row + tail) for row in rows for tail in _TAILS)


def test_every_reapable_family_has_a_declared_delete_row():
    patterns = _guard_patterns()
    assert len(patterns) == 10
    rows = _delete_rows(_rendered())
    uncovered = [p for p in patterns if not _covered(p, rows)]
    assert uncovered == []


def test_coverage_check_has_teeth():
    patterns = _guard_patterns()
    rows = _delete_rows(_rendered())
    for dropped in rows:
        remaining = [r for r in rows if r != dropped]
        assert [p for p in patterns if not _covered(p, remaining)], dropped
    assert not _covered(patterns[0], ["unrelated.prefix."])


def test_every_row_is_a_group_delete_for_the_guard_login():
    rows = declared_grants(_rendered())
    assert {r["principal"] for r in rows} == {"User:guard"}
    assert {(r["resource_type"], r["operation"], r["pattern"]) for r in rows} == {
        ("GROUP", "DELETE", "PREFIXED")
    }


_LISTING = (
    "lane=dev declared=10 live=4 missing=6 undeclared=0\n"
    "MISSING User:x GROUP PREFIXED receipt-mode-client DELETE\n"
)


def _runner(calls: list[list[str]], stdout: str, code: int):
    def run(argv: Sequence[str], timeout: float):
        calls.append(list(argv))
        declaration = yaml.safe_load(
            Path(argv[argv.index("--declaration") + 1]).read_text()
        )
        assert len(declared_grants(declaration)) == 10
        return subprocess.CompletedProcess(argv, code, stdout, "")

    return run


def test_dry_run_passes_dry_run_flag_and_reports_missing():
    calls: list[list[str]] = []
    handler = HandlerBrokerAclReconcile(_runner(calls, _LISTING, 1), _ENV)
    result = handler.handle(
        ModelBrokerAclReconcileRequest(
            mode="dry_run", reconcile_command=("reconcile-cmd", "--flag")
        )
    )
    assert calls[0][-2:] == ["apply", "--dry-run"]
    assert calls[0][calls[0].index("--ssh-target") + 1] == "broker"
    assert (result.exit_code, result.declared, result.missing) == (1, 10, 6)
    assert result.missing_rows == ("User:x GROUP PREFIXED receipt-mode-client DELETE",)


def test_apply_reads_back_zero_missing():
    calls: list[list[str]] = []
    out = _LISTING + "lane=dev declared=10 live=10 missing=0 undeclared=0\n"
    handler = HandlerBrokerAclReconcile(_runner(calls, out, 0), _ENV)
    result = handler.handle(
        ModelBrokerAclReconcileRequest(
            mode="apply", reconcile_command=("reconcile-cmd", "--flag")
        )
    )
    assert calls[0][-1] == "apply"
    assert (result.exit_code, result.live, result.missing, result.missing_rows) == (
        0,
        10,
        0,
        (),
    )


def test_unset_principal_is_refused_without_running():
    calls: list[list[str]] = []
    result = HandlerBrokerAclReconcile(
        _runner(calls, "", 0),
        {k: v for k, v in _ENV.items() if k != "ONEX_ACL_GUARD_PRINCIPAL"},
    ).handle(
        ModelBrokerAclReconcileRequest(
            mode="apply", reconcile_command=("reconcile-cmd", "--flag")
        )
    )
    assert calls == []
    assert result.exit_code == 2
    assert "ONEX_ACL_GUARD_PRINCIPAL" in result.error


def test_missing_ssh_target_is_refused_without_running():
    calls: list[list[str]] = []
    env = {k: v for k, v in _ENV.items() if k != "ONEX_LAB_BROKER_SSH_TARGET"}
    result = HandlerBrokerAclReconcile(_runner(calls, "", 0), env).handle(
        ModelBrokerAclReconcileRequest(
            mode="apply", reconcile_command=("reconcile-cmd", "--flag")
        )
    )
    assert calls == []
    assert "not set" in result.error


COMMAND = "onex.cmd.omnimarket.broker-acl-reconcile-effect-requested.v1"
TERMINAL = "onex.evt.omnimarket.broker-acl-reconcile-effect-completed.v1"


async def test_effect_command_to_completed_terminal(event_bus: EventBusInmemory):
    calls: list[list[str]] = []
    out = "lane=dev declared=10 live=10 missing=0 undeclared=0\n"
    handler = HandlerBrokerAclReconcile(_runner(calls, out, 0), _ENV)

    async def on_command(message):
        request = ModelBrokerAclReconcileRequest.model_validate_json(message.value)
        result = handler.handle(request)
        await event_bus.publish(
            TERMINAL, key=None, value=result.model_dump_json().encode()
        )

    await event_bus.start()
    try:
        await event_bus.subscribe(
            COMMAND, on_message=on_command, group_id="test-broker-acl-reconcile"
        )
        await event_bus.publish(
            COMMAND,
            key=None,
            value=ModelBrokerAclReconcileRequest(
                mode="apply", reconcile_command=("reconcile-cmd", "--flag")
            )
            .model_dump_json()
            .encode(),
        )
        history = await event_bus.get_event_history(topic=TERMINAL)
        assert len(history) == 1
        result = ModelBrokerAclReconcileResult.model_validate_json(history[0].value)
        assert (result.exit_code, result.missing) == (0, 0)
    finally:
        await event_bus.close()
