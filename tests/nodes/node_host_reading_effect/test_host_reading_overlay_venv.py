# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host reading units take their interpreter from the overlay venv alone (OMN-22548)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_reading_effect.handlers.schedule import (
    HostReadingScheduler,
    load_schedule,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelHostReadingOverlay
from tests.nodes.node_host_reading_effect.host_reading_support import (
    EXAMPLE,
    LEDGER_HOST,
    WRITING_HOST,
    overlay,
)

pytestmark = pytest.mark.unit


def _document() -> dict[str, object]:
    loaded = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("venv", [None, ""], ids=["absent", "empty"])
def test_a_host_declared_without_a_venv_is_refused_by_the_overlay(
    venv: str | None,
) -> None:
    document = _document()
    hosts = document["hosts"]
    assert isinstance(hosts, list)
    assert hosts[0]["venv"]
    if venv is None:
        del hosts[0]["venv"]
    else:
        hosts[0]["venv"] = venv
    with pytest.raises(ValidationError, match="venv"):
        ModelHostReadingOverlay.model_validate(document)


@pytest.mark.parametrize("host", [WRITING_HOST, LEDGER_HOST])
def test_an_interpreter_absent_from_the_overlay_venv_refuses_and_writes_nothing(
    tmp_path: Path, host: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "home"
    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")

    declared = next(row for row in overlay().hosts if row.name == host)
    result = HostReadingScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=run, uid=501
    ).install(host, omni_root=root, overlay_root=tmp_path / "overlays")
    assert result.exit_code == 1
    assert (
        f"no venv interpreter at {root / declared.venv / 'bin/python'}" in result.stderr
    )
    assert calls == []
    assert not home.exists()
