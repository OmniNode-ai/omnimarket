# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe each check runs writes its own state under the declared writable state root, never under the registry root.

The build history was moved under the state root, but the probe routes still named ``{registry_root}/.onex_state/...`` as the
probe's ``--state-dir``. In the runtime the registry root (OMNI_HOME=/app) is read-only, so the probe's own evidence write raised
PermissionError and exited 1, which the executor reads as a failed check: a false regression on every build.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.models.overlay_documents.model_build_delegation_checks_overlay import (
    DEFAULT_CHECK_ROUTES,
    ModelBuildDelegationChecksOverlay,
)
from omnimarket.nodes.node_build_delegation_checks.adapters.command_check_executor import (
    CommandCheckExecutor,
)
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
)

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_build_delegation_checks/overlay.example.yaml"
)


def test_no_default_route_derives_a_write_path_from_the_registry_root() -> None:
    for check, argv in DEFAULT_CHECK_ROUTES.items():
        state_dir = argv[argv.index("--state-dir") + 1]
        assert "{registry_root}" not in state_dir, (check, state_dir)
        assert state_dir.startswith("{state_root}/"), (check, state_dir)


def test_the_shipped_example_overlay_routes_carry_no_registry_root_state_dir() -> None:
    routes = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["check_routes"]
    assert routes, "the example overlay declares routes"
    for check, argv in routes.items():
        state_dir = argv[argv.index("--state-dir") + 1]
        assert "{registry_root}" not in state_dir, (check, state_dir)
        assert state_dir.startswith("{state_root}/"), (check, state_dir)
    # the example is the tested default: it must load as an overlay unchanged
    ModelBuildDelegationChecksOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def test_the_executor_fills_state_root_from_its_declared_root(tmp_path: Path) -> None:
    state = tmp_path / "data" / ".onex_state"
    home = tmp_path / "app"
    marker = "probe/marker.txt"
    code = (
        "import pathlib, sys; p = pathlib.Path(sys.argv[1], 'probe'); p.mkdir(parents=True); "
        "(p / 'marker.txt').write_text('x')"
    )
    executor = CommandCheckExecutor(
        {"C11": (sys.executable, "-c", code, "{state_root}")},
        registry_root=home,
        state_root=state,
    )

    outcome = executor.run("C11", "a" * 40, 30)

    assert outcome.reading is EnumCheckReading.PASS
    assert (state / marker).read_text() == "x"
    assert not home.exists()


def test_a_route_naming_state_root_without_a_declared_root_is_unavailable_not_filled(
    tmp_path: Path,
) -> None:
    executor = CommandCheckExecutor(
        {"C11": (sys.executable, "-c", "pass", "{state_root}")},
        registry_root=tmp_path,
    )

    outcome = executor.run("C11", "a" * 40, 30)

    assert outcome.reading is EnumCheckReading.UNAVAILABLE
    assert "state root" in outcome.detail


@pytest.mark.parametrize("check", ["C11", "C30"])
def test_default_routes_resolve_to_a_directory_under_the_state_root(
    check: str, tmp_path: Path
) -> None:
    state = tmp_path / ".onex_state"
    argv = DEFAULT_CHECK_ROUTES[check]
    resolved = argv[argv.index("--state-dir") + 1].replace("{state_root}", str(state))
    assert Path(resolved).is_relative_to(state)
