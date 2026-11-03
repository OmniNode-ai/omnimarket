"""A bus request cannot choose the program the broker ACL reconcile runs.

The argv comes only from the operator's environment command; a request that carries
``reconcile_command`` is refused by the model, before the handler or any subprocess.
"""

import json
import subprocess
from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from omnimarket.models.broker_acl_reconcile import ModelBrokerAclReconcileRequest
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.handler_broker_acl_reconcile import (
    HandlerBrokerAclReconcile,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.runtime_broker_acl_reconcile import (
    main,
)

pytestmark = pytest.mark.unit

_ENV = {
    "ONEX_LAB_BROKER_SSH_TARGET": "broker",
    "ONEX_ACL_RECONCILE_COMMAND": "reconcile-cmd --flag",
    "ONEX_ACL_GUARD_PRINCIPAL": "User:guard",
    "ONEX_ACL_FLEET_PACKAGE": "pkg",
}
_HOSTILE = ["sh", "-c", "touch /tmp/owned"]


def _recording(calls: list[list[str]]):
    def run(argv: Sequence[str], timeout: float):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_model_fields_do_not_declare_reconcile_command():
    assert set(ModelBrokerAclReconcileRequest.model_fields) == {"mode"}


def test_model_fields_forbid_extra_cells():
    assert ModelBrokerAclReconcileRequest.model_config["extra"] == "forbid"


def _deliver(payload: str, handler: HandlerBrokerAclReconcile) -> None:
    """What the runtime does with a command message: validate, then handle."""
    handler.handle(ModelBrokerAclReconcileRequest.model_validate_json(payload))


@pytest.mark.parametrize(
    "cells",
    [
        {"mode": "apply", "reconcile_command": _HOSTILE},
        {"mode": "dry_run", "reconcile_command": []},
    ],
)
def test_request_command_on_the_bus_payload_is_refused_before_any_subprocess(cells):
    calls: list[list[str]] = []
    handler = HandlerBrokerAclReconcile(_recording(calls), _ENV)
    with pytest.raises(ValidationError):
        _deliver(json.dumps(cells), handler)
    assert calls == []


def test_request_command_argv_comes_only_from_the_configured_environment_command():
    calls: list[list[str]] = []
    HandlerBrokerAclReconcile(_recording(calls), _ENV).handle(
        ModelBrokerAclReconcileRequest(mode="dry_run")
    )
    assert calls[0][:2] == ["reconcile-cmd", "--flag"]
    assert calls[0][-2:] == ["apply", "--dry-run"]
    assert "touch" not in calls[0]


def test_request_command_unset_environment_command_is_refused_without_running():
    calls: list[list[str]] = []
    env = {k: v for k, v in _ENV.items() if k != "ONEX_ACL_RECONCILE_COMMAND"}
    result = HandlerBrokerAclReconcile(_recording(calls), env).handle(
        ModelBrokerAclReconcileRequest(mode="apply")
    )
    assert calls == []
    assert result.exit_code == 2
    assert "ONEX_ACL_RECONCILE_COMMAND" in result.error


def test_request_command_operator_entry_point_has_no_command_override(capsys):
    with pytest.raises(SystemExit) as refused:
        main(["--mode", "apply", "--reconcile-command", "sh -c true"])
    assert refused.value.code == 2
    assert "--reconcile-command" in capsys.readouterr().err
