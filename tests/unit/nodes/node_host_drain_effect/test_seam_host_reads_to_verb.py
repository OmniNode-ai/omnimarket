# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the overlay's host reads to the drain verb's status line, through a real argv (OMN-24489).

The seam is the overlay's ``host_reads`` (an argv that prints one integer for ``{host}``) on one side and the verb's
SAFE reading on the other. Each test runs the verb's own command line on the durable profile with real child
processes, so a change to the argv contract (the placeholder, the integer, the exit code) or to the SAFE rule makes one
of them fail. No bus topic is crossed: both sides are in this process and its child commands, so there is no broker run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_drain_effect import runtime_host_drain as cli
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay

HOST = "example-host-a"


def _overlay(
    overlay: ModelHostDrainOverlay, outputs: dict[str, str]
) -> ModelHostDrainOverlay:
    """The example overlay on the durable profile; each host read prints the text ``outputs`` gives for its kind."""
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    document["lanes"].pop("count_command")
    document["lanes"]["receipts_dir"] = "receipts"
    for read in document["host_reads"]:
        read["command"] = [
            sys.executable,
            "-c",
            f"print({outputs[read['kind']]!r})",
            "{host}",
        ]
    return ModelHostDrainOverlay.model_validate(document)


def _status_line(
    overlay: ModelHostDrainOverlay,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> str:
    environ = {overlay.emission.home_env: str(tmp_path)}
    (tmp_path / "receipts").mkdir(exist_ok=True)
    loaded = overlay.model_copy(
        update={
            "lanes": overlay.lanes.model_copy(
                update={"receipts_dir": str(tmp_path / "receipts")}
            )
        }
    )
    assert (
        cli.run(["drain", "--host", HOST, "--line"], overlay=loaded, environ=environ)
        == 0
    )
    return capsys.readouterr().out.strip()


ZERO = {"lanes": "0", "units": "0", "model_servers": "0"}


def test_every_host_read_zero_prints_safe(
    overlay: ModelHostDrainOverlay, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = _status_line(_overlay(overlay, ZERO), tmp_path, capsys)
    assert "host read lanes=0 units=0 model_servers=0" in line
    assert line.endswith(": SAFE to power down")
    assert "NOT SAFE" not in line


@pytest.mark.parametrize("kind", ["lanes", "units", "model_servers"])
def test_one_busy_host_read_prints_not_safe(
    overlay: ModelHostDrainOverlay,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    line = _status_line(_overlay(overlay, {**ZERO, kind: "2"}), tmp_path, capsys)
    assert f"{kind}=2" in line
    assert line.endswith(": NOT SAFE to power down")


def test_a_host_read_that_prints_no_integer_prints_not_safe(
    overlay: ModelHostDrainOverlay, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = _status_line(
        _overlay(overlay, {**ZERO, "units": "unreachable"}), tmp_path, capsys
    )
    assert "units=unreadable" in line
    assert line.endswith(": NOT SAFE to power down")
