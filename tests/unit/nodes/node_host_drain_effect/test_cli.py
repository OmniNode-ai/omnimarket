# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of node_host_drain_effect (OMN-22905). The drain across two commands is proved on the bus in test_seam_verb_to_host_advertisement."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Literal, NoReturn

import pytest

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
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


def _kafka_overlay(overlay: ModelHostDrainOverlay) -> ModelHostDrainOverlay:
    document = overlay.model_dump(mode="json")
    document["profile"] = "bus"
    document["bus"] = {
        "kind": "kafka",
        "lane": "dev",
        "window_seconds": 1,
        "cache_seconds": 0,
        "timeout_seconds": 5,
    }
    return ModelHostDrainOverlay.model_validate(document)


def test_cli_bus_open_refusal_is_indeterminate(
    overlay: ModelHostDrainOverlay,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The lane declaration is found under a workspace root the environment names: a bus open that is refused is a
    # deployment problem, not a drain answer.

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise LabRunBusError("no workspace root is set")

    monkeypatch.setattr(cli, "open_sync_bus", refuse)
    environ = {overlay.emission.home_env: str(tmp_path)}
    assert cli.run(["draining"], overlay=_kafka_overlay(overlay), environ=environ) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


class _RefusedOpen:
    """An async context manager whose enter raises: SyncBus's thread re-raises it from ``ready.result``."""

    async def __aenter__(self) -> object:
        raise LabRunBusError("stop")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


def test_cli_bus_open_gets_the_workspace_root_from_the_environment(
    overlay: ModelHostDrainOverlay,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The opener runs lazily on SyncBus's own thread and its refusal re-raises from ``ready.result``, so the fake
    # records the root and raises on __aenter__, inside that thread.
    recorded: list[Path | None] = []

    def fake_open_lane_bus(
        *,
        bus: Literal["kafka", "inmemory"],
        lane: str | None,
        kafka_bootstrap: str | None,
        root: Path | None,
    ) -> _RefusedOpen:
        recorded.append(root)
        return _RefusedOpen()

    monkeypatch.setattr(cli, "open_lane_bus", fake_open_lane_bus)
    with pytest.raises(LabRunBusError):
        cli.open_sync_bus(_kafka_overlay(overlay), {"OMNIBASE_PATH": str(tmp_path)})
    assert recorded == [tmp_path]
