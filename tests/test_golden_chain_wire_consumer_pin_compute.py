# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-resolved consumer-pin read round trip over the in-memory bus (OMN-21901)."""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.handler_wire_consumer_pin import GateError
from omnimarket.models.model_wire_consumer_pin import (
    ModelConsumerPinFiles,
    ModelWireConsumerPinRequest,
    ModelWireConsumerPinResult,
)

pytestmark = pytest.mark.unit

GIT = "https://github.com/OmniNode-ai/omnimarket.git"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.email=g@omninode.ai", "-c", "user.name=g", *args],
        cwd=str(repo),
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=True,
    )
    return done.stdout.decode().strip()


def _consumer(
    tmp_path: Path, label: str, pyproject: str, lock: str
) -> ModelConsumerPinFiles:
    (tmp_path / f"{label}.pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / f"{label}.uv.lock").write_text(lock, encoding="utf-8")
    return ModelConsumerPinFiles(
        label=label,
        pyproject_path=str(tmp_path / f"{label}.pyproject.toml"),
        lock_path=str(tmp_path / f"{label}.uv.lock"),
    )


def _tag_files(
    tmp_path: Path, label: str, tag: str, commit: str
) -> ModelConsumerPinFiles:
    return _consumer(
        tmp_path,
        label,
        f'[tool.uv.sources]\nomnimarket = {{ git = "{GIT}", tag = "{tag}" }}\n',
        f'[[package]]\nname = "omnimarket"\nsource = {{ git = "{GIT}?tag={tag}#{commit}" }}\n',
    )


@pytest.mark.asyncio
async def test_golden_and_refusal_chains(tmp_path: Path) -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_wire_consumer_pin_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])()
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    assert request_topic == "onex.cmd.omnimarket.wire-consumer-pins-read-requested.v1"
    assert contract["event_bus"]["subscribe_topics"] == [request_topic]
    assert terminal_topic == "onex.evt.omnimarket.wire-consumer-pins-read.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal_topic]

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", f"dev-20261101000001-{old[:12]}")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    new = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", f"dev-20261101000002-{new[:12]}")

    bus = EventBusInmemory()
    received: asyncio.Future[ModelWireConsumerPinResult] = (
        asyncio.get_running_loop().create_future()
    )

    async def handle(message: Any) -> None:
        request = ModelWireConsumerPinRequest.model_validate_json(message.value)
        await bus.publish(
            terminal_topic,
            key=None,
            value=handler.handle(request).model_dump_json().encode(),
        )

    async def collect(message: Any) -> None:
        received.set_result(
            ModelWireConsumerPinResult.model_validate_json(message.value)
        )

    await bus.start()
    try:
        await bus.subscribe(request_topic, on_message=handle, group_id="wire-pin")
        await bus.subscribe(
            terminal_topic, on_message=collect, group_id="wire-pin-test"
        )
        request = ModelWireConsumerPinRequest(
            repo_root=str(repo),
            consumers=(
                _tag_files(tmp_path, "newer", f"dev-20261101000002-{new[:12]}", new),
                _tag_files(tmp_path, "older", f"dev-20261101000001-{old[:12]}", old),
            ),
            required=("newer", "older"),
        )
        await bus.publish(
            request_topic, key=None, value=request.model_dump_json().encode()
        )
        result = await asyncio.wait_for(received, timeout=5)
    finally:
        await bus.close()

    assert [(b.tag, b.consumers) for b in result.baselines] == [
        (f"dev-20261101000001-{old[:12]}", ("older",)),
        (f"dev-20261101000002-{new[:12]}", ("newer",)),
    ]

    sha_pin = _consumer(
        tmp_path,
        "sha",
        f'[tool.uv.sources]\nomnimarket = {{ git = "{GIT}", rev = "{new}" }}\n',
        f'[[package]]\nname = "omnimarket"\nsource = {{ git = "{GIT}?rev={new}#{new}" }}\n',
    )
    with pytest.raises(GateError) as refused:
        handler.handle(
            ModelWireConsumerPinRequest(repo_root=str(repo), consumers=(sha_pin,))
        )
    assert refused.value.outcome == "WIRE_COMPAT_PIN_REFUSED"
