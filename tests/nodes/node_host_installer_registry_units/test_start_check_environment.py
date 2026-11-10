# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the start check runs in the rendered unit's environment.

The installer's start proof used to run in the installing shell's environment: a request override
such as ONEX_SKILL_OVERLAY_ROOTS was never visible to it, so it checked a different environment
than the unit that gets loaded. The proof now runs through `env NAME=value ...` with exactly the
variables the rendered unit carries, so what is checked is what will run; a proof that is missing
the unit's overlay roots is refused.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    make_checkout,
    plist_of,
    real_run,
    request,
)

pytestmark = pytest.mark.unit

LINUX_PYTHON = "/usr/bin/python3.12"
TICK_ENTRY = "src/omnimarket/nodes/node_pr_landing_tick_orchestrator/__main__.py"
STORE_ENTRY = (
    "src/omnimarket/nodes/node_overlay_store_read_effect/runtime_overlay_store_tick.py"
)
CUTOVER_ENTRY = "src/omnimarket/nodes/node_pr_landing_snapshot_effect/__main__.py"
ENV_WORD = re.compile(r"^[A-Z_][A-Z0-9_]*=")


def env_words(argv: list[str]) -> list[str]:
    """The leading NAME=value words of an `env` command, up to the first word that is not one."""
    words: list[str] = []
    for word in argv[1:]:
        if ENV_WORD.match(word) is None:
            break
        words.append(word)
    return words


class OverlayProofRunner:
    """Records every argv. Real git and id; the `env` start proof is refused unless it carries
    ONEX_SKILL_OVERLAY_ROOTS (a falsifier: a proof in the installing shell's environment, or one
    that drops the variable, ends the install refused); everything else succeeds."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "env":
            if "ONEX_SKILL_OVERLAY_ROOTS" not in [
                w.split("=", 1)[0] for w in env_words(argv)
            ]:
                return subprocess.CompletedProcess(argv, 1, "", "overlay_refused")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] in ("git", "id"):
            return real_run(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def proof(self) -> list[str]:
        return next(c for c in self.calls if c[0] == "env" and "/usr/bin/uv" in c)


@pytest.fixture(autouse=True)
def interpreters_and_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both host kinds under test exist on this host: the brew python (launchd units) and the
    Linux pool python (systemd units); uv is on PATH."""
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path in (BREW_PYTHON, LINUX_PYTHON),
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_start_check_runs_with_the_environment_the_unit_will_run(
    tmp_path: Path,
) -> None:
    """An install succeeds only because the proof carries the unit's override; a proof without
    ONEX_SKILL_OVERLAY_ROOTS is refused and the install fails."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=TICK_ENTRY)
    run = OverlayProofRunner()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            unit="landing-tick-orchestrator",
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
        )
    )
    assert result.status == "success", result.stderr
    proof = run.proof()
    assert proof[0] == "env"
    assert proof[-1] == "--help"
    assert proof.index("/usr/bin/uv") > 1
    words = env_words(proof)
    assert f"ONEX_SKILL_OVERLAY_ROOTS={tmp_path / 'overlays'}" in words


def test_the_start_check_carries_exactly_the_environment_the_plist_carries(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=TICK_ENTRY)
    run = OverlayProofRunner()
    handler = HandlerRegistryUnits(run)
    req = request(
        tmp_path,
        unit="landing-tick-orchestrator",
        mode="install",
        project_path=str(branch),
        expected_revision=revision,
        path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
    )
    result = handler.handle(req)
    assert result.status == "success", result.stderr
    unit = load_units()["landing-tick-orchestrator"]
    environment = handler.environment(req, unit)
    for name, value in environment.items():
        assert plist_of(result.rendered)["EnvironmentVariables"][name] == value
    assert set(env_words(run.proof())) == {
        f"{name}={value}" for name, value in environment.items()
    }


def test_the_systemd_start_check_runs_with_the_environment_the_service_carries(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=STORE_ENTRY)
    run = OverlayProofRunner()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            unit="overlay-store-refresh-systemd",
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
                "LAB_WORK_HOST_NAME": "h051",
            },
        )
    )
    assert result.status == "success", result.stderr
    words = env_words(run.proof())
    assert f"ONEX_SKILL_OVERLAY_ROOTS={tmp_path / 'overlays'}" in words
    assert "LAB_WORK_HOST_NAME=h051" in words


def test_the_cutover_overlay_file_resolves_from_the_first_root_of_the_override(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=CUTOVER_ENTRY)
    run = OverlayProofRunner()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            unit="landing-snapshot-cutover",
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": f"{tmp_path}/clone/overlays:{tmp_path}/other",
            },
        )
    )
    assert result.status == "success", result.stderr
    env = plist_of(result.rendered)["EnvironmentVariables"]
    assert env["ONEX_LANDING_SNAPSHOT_OVERLAY_FILE"] == (
        f"{tmp_path}/clone/overlays/node_pr_landing_snapshot_effect_cutover/overlay.yaml"
    )


def test_a_unit_that_declares_no_overlay_roots_refuses_an_unfilled_overlay_root(
    tmp_path: Path,
) -> None:
    unit = copy.deepcopy(load_units()["landing-snapshot-cutover"])
    del unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"]
    unit["environment_path_overrides"] = [
        name
        for name in unit["environment_path_overrides"]
        if name != "ONEX_SKILL_OVERLAY_ROOTS"
    ]
    handler = HandlerRegistryUnits(real_run, units={"landing-snapshot-cutover": unit})
    with pytest.raises(ValueError, match="overlay_root"):
        handler.environment(request(tmp_path, unit="landing-snapshot-cutover"), unit)
