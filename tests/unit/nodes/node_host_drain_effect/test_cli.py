# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of node_host_drain_effect (OMN-22905). The drain across two commands is proved on the bus in test_seam_verb_to_host_advertisement."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_host_drain_effect import runtime_host_drain as cli
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import FixedLaneSource
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay


def _in_memory(
    overlay: ModelHostDrainOverlay, home: Path
) -> tuple[ModelHostDrainOverlay, dict[str, str], FixedLaneSource]:
    # Lane counts come from a fixed advertisement; only the profile changes here.
    document = overlay.model_dump(mode="json")
    document["profile"] = "in_memory"
    for read in document["host_reads"]:
        read["command"] = ["python3", "-c", "print(0)", "{host}", read["kind"]]
    return (
        ModelHostDrainOverlay.model_validate(document),
        {overlay.emission.home_env: str(home)},
        FixedLaneSource({"example-host-a": 2, "example-host-b": 0}),
    )


def test_cli_without_the_emission_home_is_indeterminate(
    overlay: ModelHostDrainOverlay,
) -> None:
    # The bus profile writes its chain journal under the emission home: a missing home is a missing deployment fact, not a bus read.
    document = overlay.model_dump(mode="json")
    document["profile"] = "bus"
    document["bus"] = {
        "kind": "inmemory",
        "window_seconds": 1,
        "cache_seconds": 0,
        "timeout_seconds": 5,
    }
    loaded = ModelHostDrainOverlay.model_validate(document)
    assert cli.run(["draining"], overlay=loaded, environ={}) == 3
