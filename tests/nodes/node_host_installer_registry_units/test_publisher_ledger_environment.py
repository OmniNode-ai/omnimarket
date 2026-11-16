# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publisher unit carries the ledger environment its J5 step needs (OMN-23086).

The publish cycle's join J5 step appends one ledger row over the bus and reads it back; it names the host that writes and the two
bus commands through environment variables its overlay names. The unit installed without them ran that step to INDETERMINATE
(exit 3) on every tick.

Golden chain: the rendered unit holds the host name the request gave, the append command and the read command under the registry
root, and still parses as a plist although the commands hold long options.
Error chain (the failing controls): an install that names no ledger host, in the request or the shell, is refused naming the
variable; the previous contract's unit held none of the three variables.

OMN-23606: the read command is also a request input. The unit installed through this node always held the contract's read
command, and on the launching Mac the floor's window read through it took 23.5 s against 15.9 s through the internal ledger
source, which put the 30 s factory_floor step over its limit every cycle; no request could name the faster command. The previous
contract's unit ignored the request's command (the failing control).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    UNIT,
    make_checkout,
    plist_of,
    real_run,
    request,
)

pytestmark = pytest.mark.unit

LEDGER_VARIABLES = (
    "ONEX_LEDGER_HOST_NAME",
    "ONEX_LEDGER_BUS_APPEND_COMMAND",
    "ONEX_LEDGER_BUS_READ_COMMAND",
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME", raising=False)
    monkeypatch.delenv("ONEX_LEDGER_BUS_READ_COMMAND", raising=False)


def environment_of(tmp_path: Path, **kw: str) -> dict[str, str]:
    make_checkout(tmp_path / "omni" / "omnimarket")
    result = HandlerRegistryUnits(real_run).handle(request(tmp_path, **kw))
    assert result.status == "success", result.stderr
    env: dict[str, str] = plist_of(result.rendered)["EnvironmentVariables"]
    return env


def test_golden_the_rendered_unit_holds_the_three_ledger_variables(
    tmp_path: Path,
) -> None:
    env = environment_of(tmp_path, ledger_host_name="host-under-test")
    root = str(tmp_path / "omni")
    assert env["ONEX_LEDGER_HOST_NAME"] == "host-under-test"
    assert env["ONEX_LEDGER_BUS_APPEND_COMMAND"] == (
        f"{root}/omnimarket/.venv/bin/python -m omnimarket.work_ledger_bus.cli append "
        f"--bus-lane dev --omnibase-path {root}"
    )
    assert env["ONEX_LEDGER_BUS_READ_COMMAND"] == (
        f"{root}/omnibase_infra/.venv/bin/onex ledger query --stdin"
    )


def test_golden_the_host_name_comes_from_the_installing_shell_when_the_request_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "shell-host")
    assert (
        environment_of(tmp_path, ledger_host_name="")["ONEX_LEDGER_HOST_NAME"]
        == "shell-host"
    )


def test_error_an_install_that_names_no_ledger_host_is_refused_naming_the_variable(
    tmp_path: Path,
) -> None:
    make_checkout(tmp_path / "omni" / "omnimarket")
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, mode="install", ledger_host_name="")
    )
    assert result.status == "failure"
    assert "ONEX_LEDGER_HOST_NAME" in result.stderr


def test_error_the_unit_declares_the_host_as_required_and_both_commands_as_its_own() -> (
    None
):
    unit = load_units()[UNIT]
    assert "ONEX_LEDGER_HOST_NAME" in unit["required_environment_inputs"]
    assert {v for v in LEDGER_VARIABLES if v != "ONEX_LEDGER_HOST_NAME"} <= set(
        unit["environment"]
    )


def test_error_a_host_name_with_a_space_is_refused_by_the_request_model() -> None:
    with pytest.raises(ValueError, match="ledger_host_name"):
        request(Path("/tmp/x"), ledger_host_name="two words")


READ_COMMAND = "/opt/venv/bin/python -m ledger_reader.source --stdin"


def test_golden_the_request_names_the_ledger_read_command(tmp_path: Path) -> None:
    env = environment_of(
        tmp_path, ledger_host_name="host-under-test", ledger_read_command=READ_COMMAND
    )
    assert env["ONEX_LEDGER_BUS_READ_COMMAND"] == READ_COMMAND


def test_golden_the_read_command_comes_from_the_installing_shell_when_the_request_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_BUS_READ_COMMAND", READ_COMMAND)
    env = environment_of(tmp_path, ledger_host_name="host-under-test")
    assert env["ONEX_LEDGER_BUS_READ_COMMAND"] == READ_COMMAND


def test_error_the_unit_declares_the_read_command_as_a_request_input() -> None:
    unit = load_units()[UNIT]
    assert (
        unit["environment_inputs"]["ONEX_LEDGER_BUS_READ_COMMAND"]
        == "ledger_read_command"
    )


@pytest.mark.parametrize(
    "command",
    ["relative/onex ledger query", "/bin/sh -c x;y", "/bin/x $(id)", "/bin/x `id`"],
)
def test_error_a_read_command_with_a_shell_metacharacter_or_relative_path_is_refused(
    command: str,
) -> None:
    with pytest.raises(ValueError, match="ledger_read_command"):
        request(Path("/tmp/x"), ledger_read_command=command)
