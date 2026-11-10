# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reading units take their interpreter from the overlay venv alone (OMN-22548).

The reading writer and the host reading node once read their interpreter from a contract key that
named the lab dispatch venv; when that venv's bus publisher broke, the hourly readings stopped
writing rows. The venv is now a value of each overlay host, so these tests hold three things:
no contract of either node names a venv, an overlay host without a venv is refused, and an
interpreter that is absent from the declared venv is a refusal that writes nothing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    ReadingScheduler,
    load_schedule,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from tests.nodes.node_reading_writer_effect.reading_support import (
    overlay,
    overlay_document,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACTS = (
    NODES / "node_reading_writer_effect/contract.yaml",
    NODES / "node_host_reading_effect/contract.yaml",
)


def venv_mentions(value: Any, path: str = "") -> Iterator[str]:
    """Every key and every string value of a loaded contract that names a venv, with its path."""
    if isinstance(value, dict):
        for key, child in value.items():
            where = f"{path}.{key}" if path else str(key)
            if "venv" in str(key).lower():
                yield where
            yield from venv_mentions(child, where)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from venv_mentions(child, f"{path}[{index}]")
    elif isinstance(value, str) and "venv" in value.lower():
        yield f"{path} = {value!r}"


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda path: path.parent.name)
def test_no_contract_key_names_a_venv(contract: Path) -> None:
    loaded = yaml.safe_load(contract.read_text(encoding="utf-8"))
    assert list(venv_mentions(loaded)) == []


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda path: path.parent.name)
def test_the_guard_flags_a_contract_that_names_a_dispatch_venv(contract: Path) -> None:
    """Positive control: the same walk reads a planted key and a planted value as mentions."""
    loaded = yaml.safe_load(contract.read_text(encoding="utf-8"))
    loaded["schedule"]["dispatch_venv"] = "venvs/lab-dispatch"
    loaded["schedule"]["interpreter"] = "venvs/lab-dispatch/bin/python"
    assert list(venv_mentions(loaded)) == [
        "schedule.dispatch_venv",
        "schedule.dispatch_venv = 'venvs/lab-dispatch'",
        "schedule.interpreter = 'venvs/lab-dispatch/bin/python'",
    ]


def test_a_writer_host_without_a_venv_is_refused_by_the_overlay() -> None:
    document = overlay_document()
    document["schedule_hosts"] = [{"name": "h-test", "scheduler": "systemd-user"}]
    with pytest.raises(ValidationError, match="venv"):
        ModelReadingOverlay.model_validate(document)


def test_a_writer_host_with_an_empty_venv_is_refused_by_the_overlay() -> None:
    document = overlay_document()
    document["schedule_hosts"] = [
        {"name": "h-test", "scheduler": "systemd-user", "venv": ""}
    ]
    with pytest.raises(ValidationError, match="venv"):
        ModelReadingOverlay.model_validate(document)


def test_an_interpreter_absent_from_the_overlay_venv_refuses_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    commands: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")

    result = ReadingScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=run
    ).install("h-test", omni_root=root, overlay_root=tmp_path / "overlays")
    assert result.exit_code == 1
    assert f"no venv interpreter at {root}/venv-readings/bin/python" in result.stderr
    assert commands == []
    assert not (home / ".config").exists()
