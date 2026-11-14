# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22881: the Mac unit of the central test harness carries the env its overlay's ledger emission names.

OMN-23007 gave the harness overlay an ``emission.ledger`` block that names two environment variables, the bus append
command and the host the rows name. The Mac unit (``ai.omninode.lab-test-harness``, declared in this node's contract)
declared neither, so every tick on the Mac built no appender and exited 1 (``ONEX_LEDGER_BUS_APPEND_COMMAND`` not set),
and the job-exit alarm counted it. The control builds the harness's own appender from the environment the rendered
unit really carries; with either variable absent the appender refuses, which is the exit the unit gave.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    HARNESS_ENTRY,
    HARNESS_UNIT,
    NODE_DIR,
    make_checkout,
    plist_of,
    real_run,
    request,
)

# The two names the deployed harness overlay's emission.ledger block carries.
ROUTE = ModelLedgerAppendRoute(
    command_env="ONEX_LEDGER_BUS_APPEND_COMMAND",
    host_env="ONEX_LEDGER_HOST_NAME",
    timeout_seconds=90,
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def rendered_environment(
    tmp_path: Path, units: dict[str, Any] | None = None, **kw: Any
) -> dict[str, str]:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=HARNESS_ENTRY)
    result = HandlerRegistryUnits(real_run, units=units).handle(
        request(
            tmp_path,
            unit=HARNESS_UNIT,
            project_path=str(branch),
            expected_revision=revision,
            **kw,
        )
    )
    assert result.status == "success", result.stderr
    environment: dict[str, str] = plist_of(result.rendered)["EnvironmentVariables"]
    return environment


def test_the_rendered_harness_unit_builds_the_ledger_appender_its_overlay_names(
    tmp_path: Path,
) -> None:
    environment = rendered_environment(tmp_path)
    appender = build_appender(ROUTE, "central-test-harness", environment)
    assert appender is not None
    root = str(tmp_path / "omni")
    command = shlex.split(environment[ROUTE.command_env])
    assert command[0] == f"{root}/omnimarket/.venv/bin/python"
    assert command[1:3] == ["-m", "omnimarket.work_ledger_bus.cli"]
    assert command[command.index("--omnibase-path") + 1] == root
    assert environment[ROUTE.host_env] == "h200"


def test_the_harness_unit_host_name_is_a_declared_override(tmp_path: Path) -> None:
    environment = rendered_environment(
        tmp_path, path_overrides={"ONEX_LEDGER_HOST_NAME": "h201"}
    )
    assert environment["ONEX_LEDGER_HOST_NAME"] == "h201"


@pytest.mark.parametrize("missing", [ROUTE.command_env, ROUTE.host_env])
def test_a_harness_unit_without_a_ledger_variable_builds_no_appender(
    tmp_path: Path, missing: str
) -> None:
    """The named break: the declaration as it stood before the fix, one variable removed, refuses as the Mac unit did."""
    units = load_units()
    declared = dict(units[HARNESS_UNIT])
    declared["environment"] = {
        k: v for k, v in declared["environment"].items() if k != missing
    }
    environment = rendered_environment(
        tmp_path, units={**units, HARNESS_UNIT: declared}
    )
    assert missing not in environment
    with pytest.raises(LedgerRouteError, match=missing):
        build_appender(ROUTE, "central-test-harness", environment)


def test_the_plist_template_comment_names_no_placeholder_the_renderer_would_fill() -> (
    None
):
    """The renderer replaces every occurrence of a placeholder, a comment's included. A value with two hyphens in a row
    (a command's ``--bus-lane``) put into the template's XML comment made the plist unparseable."""
    template = (
        NODE_DIR / "templates" / "ai.omninode.registry-module-tick.plist.tmpl"
    ).read_text(encoding="utf-8")
    comment = template[template.index("<!--") : template.index("-->")]
    assert re.findall(r"__[A-Z_]+__", comment) == []
