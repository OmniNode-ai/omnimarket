# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the Claude token pool rotation tick on every host the pool overlay lists (OMN-23887).

The pool's rotation tick had its own installer (``schedule install``, a hand-built venv per host), so it was not
installable the declared way: no project path, no expected revision, and an environment of its own making. It is now a
registry unit per platform. The pool overlay's host rows name each host's scheduler, and every row must resolve to a
declared unit that renders with that host's name as the tick's host, so a listed host with no declaration is red.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_claude_token_pool_effect.handlers.schedule import (
    CONTRACT_PATH,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationOverlay,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    Recorder,
    make_checkout,
    request,
)

pytestmark = pytest.mark.unit

LINUX_PYTHON = "/usr/bin/python3.12"
BREW_PYTHON = "/opt/homebrew/bin/python3.13"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
UNIT_FOR_SCHEDULER = {
    "systemd-user": "claude-pool-rotation-tick-systemd",
    "launchd": "claude-pool-rotation-tick",
}
CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|PRIVATE_KEY", re.I
)


def _overlay() -> ModelRotationOverlay:
    return ModelRotationOverlay.model_validate(
        yaml.safe_load(
            (CONTRACT_PATH.parent / "overlay.example.yaml").read_text(encoding="utf-8")
        )
    )


def unit_for_host(overlay: ModelRotationOverlay, host_name: str) -> str:
    """The declared registry unit that installs the tick on ``host_name``; refuses a host with none."""
    row = overlay.host(host_name)
    if row is None:
        raise LookupError(f"{host_name} is not a host of the pool overlay")
    declared = UNIT_FOR_SCHEDULER.get(row.scheduler)
    if declared is None or declared not in load_units():
        raise LookupError(
            f"{host_name}: no registry unit declares the tick for scheduler {row.scheduler}"
        )
    return declared


@pytest.fixture(autouse=True)
def python_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present",
        lambda path: path in {LINUX_PYTHON, BREW_PYTHON},
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _host_name_in(rendered: str) -> str | None:
    """The value the rendered unit gives ONEX_HOST_NAME, from a systemd Environment line or a plist key pair."""
    for line in rendered.splitlines():
        match = re.search(r'ONEX_HOST_NAME=([^"\s]+)', line)
        if match:
            return match.group(1)
    match = re.search(r"<key>ONEX_HOST_NAME</key>\s*<string>([^<]+)</string>", rendered)
    return match.group(1) if match else None


def test_golden_every_listed_host_resolves_to_a_declared_unit_that_renders_with_its_own_name(
    tmp_path: Path,
) -> None:
    overlay = _overlay()
    assert len(overlay.hosts) >= 2
    seen_platforms = set()
    for row in overlay.hosts:
        name = unit_for_host(overlay, row.name)
        unit = load_units()[name]
        seen_platforms.add(unit["platform"])
        branch = tmp_path / row.name / "branch"
        revision = make_checkout(branch, entry=unit["entry_file"])
        result = HandlerRegistryUnits(Recorder()).handle(
            request(
                tmp_path / row.name,
                unit=name,
                mode="dry-run",
                project_path=str(branch),
                expected_revision=revision,
                ledger_host_name=row.name,
            )
        )
        assert result.status == "success", f"{row.name}: {result.stderr}"
        assert result.revision == revision
        assert not result.project_is_canonical
        assert _host_name_in(result.rendered) == row.name
        assert "ONEX_SKILL_OVERLAY_ROOTS" in result.rendered
        assert unit["module"] in result.rendered
        assert not CREDENTIAL_NAME.search(
            "\n".join(
                line
                for line in result.rendered.splitlines()
                if line.startswith("Environment=")
            )
        )
    assert seen_platforms == {"systemd", "launchd"}


def test_error_a_listed_host_with_no_declared_unit_is_refused() -> None:
    overlay = _overlay()
    odd = overlay.model_copy(
        update={
            "hosts": (
                *overlay.hosts,
                overlay.hosts[0].model_copy(
                    update={"name": "host-z", "scheduler": "cron"}
                ),
            )
        }
    )
    with pytest.raises(LookupError, match="host-z: no registry unit declares"):
        unit_for_host(odd, "host-z")
    assert [unit_for_host(odd, row.name) for row in odd.hosts[:-1]]
    with pytest.raises(LookupError, match="not a host of the pool overlay"):
        unit_for_host(overlay, "host-unlisted")


@pytest.mark.parametrize("name", sorted(UNIT_FOR_SCHEDULER.values()))
def test_error_an_install_without_the_host_name_is_refused(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_HOST_NAME", raising=False)
    monkeypatch.delenv("ONEX_HOST_NAME", raising=False)
    unit = load_units()[name]
    assert unit["required_environment_inputs"] == ["ONEX_HOST_NAME"]
    handler = HandlerRegistryUnits(Recorder())
    with pytest.raises(ValueError, match="ONEX_HOST_NAME is unset"):
        handler.environment(
            request(tmp_path, unit=name, mode="install", ledger_host_name=""), unit
        )
    named = handler.environment(
        request(tmp_path, unit=name, mode="install", ledger_host_name="host-a"), unit
    )
    assert named["ONEX_HOST_NAME"] == "host-a"


@pytest.mark.parametrize("name", sorted(UNIT_FOR_SCHEDULER.values()))
def test_error_a_checkout_at_another_revision_is_refused(
    tmp_path: Path, name: str
) -> None:
    unit = load_units()[name]
    branch = tmp_path / "branch"
    make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=name,
            mode="dry-run",
            project_path=str(branch),
            expected_revision="0" * 40,
            ledger_host_name="host-a",
        )
    )
    assert result.status != "success"


def test_golden_the_unit_module_answers_its_probe_without_an_overlay_or_a_write(
    tmp_path: Path,
) -> None:
    for name in UNIT_FOR_SCHEDULER.values():
        unit = load_units()[name]
        done = subprocess.run(
            [sys.executable, "-m", unit["module"], *unit["probe_argv"]],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {"status": "probe_ok"}
        assert list(tmp_path.iterdir()) == []
