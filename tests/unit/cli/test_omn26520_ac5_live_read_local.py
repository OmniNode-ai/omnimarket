# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-26520 AC5 live-read item runs on the host that holds the dev runtime.

The item once read ``ssh omni-201 docker inspect ...``. ``omni-201`` does not
resolve from any lab host, so the item exited 255 on every host it was run
from and the done gate could never pass. The item now reads the container
where it runs and fails unless the health status is ``healthy``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-26520.yaml"
ITEM_ID = "dod-omn26520-ac5-live-read"


def _live_read_command() -> str:
    contract = yaml.safe_load(CONTRACT.read_text())
    items = [i for i in contract["dod_evidence"] if i["id"] == ITEM_ID]
    assert len(items) == 1, f"expected one {ITEM_ID} item, found {len(items)}"
    checks = items[0]["checks"]
    assert len(checks) == 1
    return str(checks[0]["check_value"])


def test_the_live_read_names_no_host_a_lab_machine_cannot_resolve() -> None:
    words = shlex.split(_live_read_command())
    assert words[0] != "ssh", "the live read starts with ssh to a host name"
    assert "omni-201" not in words


def test_the_live_read_reads_the_runtime_container_health() -> None:
    command = _live_read_command()
    assert "docker inspect" in command
    assert "{{.State.Health.Status}}" in command
    assert "omninode-runtime" in command


def test_the_live_read_fails_unless_the_container_reads_healthy() -> None:
    command = _live_read_command()
    assert "healthy" in command.split("omninode-runtime", 1)[1], (
        "the command prints the status but never compares it, so an unhealthy "
        "runtime would exit 0"
    )
