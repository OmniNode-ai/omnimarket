# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the lane process effect tests: stub runner scripts, specs and a wait for a file or a pid to end."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from omnibase_spi.contracts.services import ModelLaneStartSpec
from omnibase_spi.protocols.lane import ProtocolLaneProcessRunner

from omnimarket.nodes.node_lane_process_effect.handlers.handler_lane_process import (
    HandlerLaneProcess,
)

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

LANE = "lane-test"
WAIT_SECONDS = 15.0


class FakeContainer:
    """The part of the container the handler uses: it answers the one runner it was given."""

    def __init__(self, runner: ProtocolLaneProcessRunner) -> None:
        self.runner = runner
        self.asked: list[object] = []

    def get_service(
        self, protocol_type: object, service_name: str | None = None
    ) -> object:
        self.asked.append(protocol_type)
        if protocol_type is not ProtocolLaneProcessRunner:
            raise LookupError(f"no service for {protocol_type}")
        return self.runner


def handler_with(runner: ProtocolLaneProcessRunner) -> HandlerLaneProcess:
    """The handler as the runtime builds it (from the container), over a container that holds this runner."""
    return HandlerLaneProcess(cast("ModelONEXContainer", FakeContainer(runner)))


def write_script(directory: Path, name: str, body: str) -> Path:
    """Write a python script the tests run as the stub leader or the forwarded command."""
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def spec_for(
    argv: tuple[str, ...],
    working_directory: Path,
    *,
    environment: tuple[tuple[str, str], ...] = (),
    lane: str = LANE,
) -> ModelLaneStartSpec:
    return ModelLaneStartSpec(
        argv=argv,
        environment=environment,
        working_directory=str(working_directory),
        lane=lane,
    )


def python_spec(
    script: Path,
    working_directory: Path,
    *args: str,
    environment: tuple[tuple[str, str], ...] = (),
    lane: str = LANE,
) -> ModelLaneStartSpec:
    return spec_for(
        (sys.executable, str(script), *args),
        working_directory,
        environment=environment,
        lane=lane,
    )


def wait_for(condition: object, what: str) -> None:
    """Poll a zero-argument callable until it is true, for at most WAIT_SECONDS."""
    assert callable(condition)
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
