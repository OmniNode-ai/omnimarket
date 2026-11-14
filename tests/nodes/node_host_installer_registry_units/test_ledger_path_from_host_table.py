# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: an install that names no ledger path reads the one the host table's row for the host declares.

An install of a unit that takes the ledger path as a request input (the ONEX_LEDGER_PATH
input of host-drift-owner-dispatch-systemd), with no ledger path in the request or the
shell, reads the ledger the lab host table's row for the host declares (OMN-24406, ported
from the frozen change the frozen internal installer change, OMN-23598): a declared path under $OMNI_HOME/
fills against the request's registry root, and an absolute declared path is used as is.
The request's ledger path and the shell's ONEX_LEDGER_PATH still win over the row; a row
that declares no ledger path, a host the table does not list and a table that cannot be
read all refuse the install, naming the missing input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.overlay_keys import (
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsResult,
)
from tests.nodes.node_host_installer_registry_units.test_host_drift_owner_dispatch_unit import (
    checkout,
    request,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    HANDLER,
    LINUX_PYTHON,
    Recorder,
)

pytestmark = pytest.mark.unit

LEDGER_HOST = "h-example"
UNIT = "host-drift-owner-dispatch-systemd"
# The shell variable the unit's contract fills from the request's ledger_path field: read from the contract.
LEDGER_ENV = next(
    name
    for name, field in load_units()[UNIT]["environment_inputs"].items()
    if field == "ledger_path"
)
DECLARED = "$OMNI_HOME/docs/tracking/example-rows.md"


def table(*rows: ModelLabHostRow) -> ModelLabHostsTable:
    """The smallest valid lab host table: the rows given, and nothing else."""
    return ModelLabHostsTable(hosts=rows)


def row(name: str, **fields: object) -> ModelLabHostRow:
    """One host row: its name, a lab target and the fields it declares (a ledger path among them)."""
    return ModelLabHostRow(name=name, target=f"{name}.lab", **fields)


def handler(rows: tuple[ModelLabHostRow, ...]) -> HandlerRegistryUnits:
    """The installer whose host table is the rows: the loader never reads the overlay store."""
    return HandlerRegistryUnits(Recorder(), hosts=lambda: table(*rows))


def install(
    tmp_path: Path,
    rows: tuple[ModelLabHostRow, ...],
    **fields: object,
) -> ModelRegistryUnitsResult:
    """The install of the unit from a clean branch checkout, the host table given."""
    path = tmp_path / "branch"
    revision = checkout(path)
    return handler(rows).handle(
        request(tmp_path, path, revision, mode="install", **fields)
    )


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_golden_host_table_ledger_path_a_declared_omni_home_path_fills_the_rendered_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install that names no ledger path reads the one the host table's row for the host declares (OMN-24406)."""
    monkeypatch.delenv(LEDGER_ENV, raising=False)
    result = install(
        tmp_path, (row(LEDGER_HOST, ledger_path=DECLARED),), ledger_path=""
    )
    assert result.status == "success", result.stderr
    expected = "=" + str(tmp_path / "omni" / "docs/tracking/example-rows.md")
    assert expected in result.rendered


def test_golden_host_table_ledger_path_an_absolute_declared_path_is_used_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_ENV, raising=False)
    declared = f"{tmp_path}/ledger/rows.md"
    result = install(
        tmp_path, (row(LEDGER_HOST, ledger_path=declared),), ledger_path=""
    )
    assert result.status == "success", result.stderr
    assert "=" + declared in result.rendered


def test_host_table_ledger_path_a_request_ledger_path_wins_over_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_ENV, raising=False)
    supplied = f"{tmp_path}/ledger/supplied.md"
    result = install(
        tmp_path, (row(LEDGER_HOST, ledger_path=DECLARED),), ledger_path=supplied
    )
    assert result.status == "success", result.stderr
    assert "=" + supplied in result.rendered
    assert "docs/tracking/example-rows.md" not in result.rendered


def test_host_table_ledger_path_the_shell_variable_wins_over_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplied = f"{tmp_path}/ledger/shell.md"
    monkeypatch.setenv(LEDGER_ENV, supplied)
    result = install(
        tmp_path, (row(LEDGER_HOST, ledger_path=DECLARED),), ledger_path=""
    )
    assert result.status == "success", result.stderr
    assert "=" + supplied in result.rendered
    assert "docs/tracking/example-rows.md" not in result.rendered


def test_host_table_ledger_path_a_row_with_no_ledger_path_and_an_unlisted_host_still_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_ENV, raising=False)
    path = tmp_path / "branch"
    revision = checkout(path)
    for rows in (
        (row(LEDGER_HOST),),
        (row("h999", ledger_path=DECLARED),),
    ):
        result = handler(rows).handle(
            request(tmp_path, path, revision, mode="install", ledger_path="")
        )
        assert result.status == "failure"
        assert result.exit_code == 1
        assert LEDGER_ENV in result.stderr


def test_host_table_ledger_path_an_unreadable_table_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_ENV, raising=False)

    def hosts() -> ModelLabHostsTable:
        raise NodeOverlayError("unreadable")

    path = tmp_path / "branch"
    revision = checkout(path)
    result = HandlerRegistryUnits(Recorder(), hosts=hosts).handle(
        request(tmp_path, path, revision, mode="install", ledger_path="")
    )
    assert result.status == "failure"
    assert result.exit_code == 1
    assert "lab host table cannot be read" in result.stderr


def test_failing_control_host_table_ledger_row_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The break row_ignored: the base code refused the install without ever consulting the
    row its ledger path comes from, and the fix refuses it still when the row declares none."""
    monkeypatch.delenv(LEDGER_ENV, raising=False)
    path = tmp_path / "branch"
    revision = checkout(path)
    result = handler((row(LEDGER_HOST),)).handle(
        request(tmp_path, path, revision, mode="install", ledger_path="")
    )
    assert result.status == "failure"
    assert LEDGER_ENV in result.stderr
