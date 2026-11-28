# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A row goes to the ledger host through the append node's command, with no ledger file named."""

from __future__ import annotations

import subprocess
import uuid
from unittest.mock import Mock

import pytest

from omnimarket.handlers import ledger_append_via_node as module

NAMESPACE = uuid.UUID("5f0c1d2e-7a4b-4c8e-9b3a-2d6e8f1a0c47")


def test_request_id_is_stable_per_key() -> None:
    assert module.request_id_for(NAMESPACE, "a") == module.request_id_for(
        NAMESPACE, "a"
    )
    assert module.request_id_for(NAMESPACE, "a") != module.request_id_for(
        NAMESPACE, "b"
    )


def test_row_is_sent_on_standard_input_over_the_work_ledger_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.socket, "gethostname", lambda: "h053.example")
    request_id = module.request_id_for(NAMESPACE, "k")
    assert module.append_row_via_bus(
        "row",
        lane="a-lane",
        request_id=request_id,
        command_env="UNSET_COMMAND_ENV",
        environ={},
    ) == (True, "ledger append exit 0: ")
    argv = run.call_args.args[0]
    assert argv == [
        "onex",
        "work-ledger",
        "append",
        "--lane",
        "a-lane",
        "--host",
        "h053",
        "--request-id",
        str(request_id),
    ]
    assert run.call_args.kwargs["input"] == "row"


def test_command_env_overrides_and_an_empty_command_refuses() -> None:
    request_id = module.request_id_for(NAMESPACE, "k")
    assert module.append_row_via_bus(
        "row",
        lane="a-lane",
        request_id=request_id,
        command_env="X",
        environ={"X": "  "},
    ) == (False, "ledger command is empty")


@pytest.mark.parametrize(
    "failure", [OSError("missing"), subprocess.TimeoutExpired("onex", 360)]
)
def test_launch_failure_is_not_ok(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=failure))
    ok, detail = module.append_row_via_bus(
        "row",
        lane="a-lane",
        request_id=module.request_id_for(NAMESPACE, "k"),
        command_env="X",
    )
    assert ok is False
    assert str(failure) in detail


def test_host_bus_append_command_is_the_whole_command_when_the_caller_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unit that declares ONEX_LEDGER_BUS_APPEND_COMMAND appends through it, bus lane and root bound (OMN-24562).

    The default ``onex work-ledger`` binds no bus lane, so a unit with no workspace root exits 69 on every append.
    """
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.socket, "gethostname", lambda: "h053.example")
    request_id = module.request_id_for(NAMESPACE, "k")
    environ = {
        "ONEX_LEDGER_BUS_APPEND_COMMAND": "/py -m a.cli append --bus-lane dev --omnibase-path /root"
    }
    assert module.append_row_via_bus(
        "row",
        lane="a-lane",
        request_id=request_id,
        command_env="UNSET_COMMAND_ENV",
        environ=environ,
    ) == (True, "ledger append exit 0: ")
    assert run.call_args.args[0] == [
        "/py", "-m", "a.cli", "append", "--bus-lane", "dev", "--omnibase-path", "/root",
        "--lane", "a-lane", "--host", "h053", "--request-id", str(request_id),
    ]  # fmt: skip


def test_caller_command_env_wins_over_the_host_bus_append_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(module.subprocess, "run", run)
    module.append_row_via_bus(
        "row",
        lane="a-lane",
        request_id=module.request_id_for(NAMESPACE, "k"),
        command_env="X",
        environ={"X": "mine", "ONEX_LEDGER_BUS_APPEND_COMMAND": "/py append"},
    )
    assert run.call_args.args[0][:2] == ["mine", "append"]
