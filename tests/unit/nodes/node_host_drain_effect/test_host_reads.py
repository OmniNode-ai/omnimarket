# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay's host reads and the adapter behind them (OMN-24489).

SAFE to power down is a reading of the host itself, so the overlay must declare a host read for the one kind
(model_servers) exactly once, and each read's command must name ``{host}``. The adapter runs that argv with the
host substituted, no shell, and refuses a stdout that is not a non-negative integer: an unreadable count is never a
measured zero. A timeout of the read propagates as a SubprocessError.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_drain_effect.adapters.host_reads import CommandHostRead
from omnimarket.nodes.node_host_drain_effect.models.model_host_drain import (
    EnumHostReadKind,
)
from omnimarket.nodes.node_host_drain_effect.overlay import (
    ModelHostDrainOverlay,
    ModelHostReadSettings,
    load_example_overlay,
)

pytestmark = pytest.mark.unit


def _document() -> dict[str, Any]:
    """The packaged example overlay as a mutable document; each refusal test cuts one thing out of it."""
    return load_example_overlay().model_dump(mode="json")


def _refused(document: dict[str, Any]) -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        ModelHostDrainOverlay.model_validate(document)
    return exc_info.value


def test_an_overlay_without_host_reads_is_refused() -> None:
    document = _document()
    del document["host_reads"]
    assert "host_reads" in str(_refused(document))


def test_an_overlay_missing_the_model_servers_kind_is_refused() -> None:
    document = _document()
    document["host_reads"] = [
        read for read in document["host_reads"] if read["kind"] != "model_servers"
    ]
    message = str(_refused(document))
    assert "at least 1 item" in message
    assert "at least 1 item" in message


def test_an_overlay_that_declares_a_kind_twice_is_refused() -> None:
    document = _document()
    document["host_reads"] = [*document["host_reads"], document["host_reads"][0]]
    message = str(_refused(document))
    assert "twice" in message
    assert "model_servers" in message


def test_a_command_that_names_no_host_is_refused() -> None:
    document = _document()
    document["host_reads"][0]["command"] = ["example-host-read", "model_servers"]
    assert "command must name {host}" in str(_refused(document))


class _FakeRun:
    """A ``run`` that records one call and prints a fixed stdout."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr="")


def _settings(
    command: tuple[str, ...] = ("example-host-read", "{host}", "model_servers"),
) -> ModelHostReadSettings:
    return ModelHostReadSettings(
        name="model_servers",
        kind=EnumHostReadKind.MODEL_SERVERS,
        command=command,
        timeout_seconds=30,
    )


def test_a_read_returns_the_printed_integer_and_names_the_host_in_the_argv() -> None:
    run = _FakeRun("3\n")
    read = CommandHostRead(_settings(), run=run)
    assert read.name == "model_servers"
    assert read.kind is EnumHostReadKind.MODEL_SERVERS
    assert read.read("example-host-a") == 3
    assert len(run.calls) == 1
    argv, kwargs = run.calls[0]
    assert argv == ["example-host-read", "example-host-a", "model_servers"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30
    assert kwargs["check"] is True
    assert "shell" not in kwargs


def test_output_that_is_not_an_integer_is_refused_naming_the_read() -> None:
    read = CommandHostRead(_settings(), run=_FakeRun("not-a-count\n"))
    with pytest.raises(ValueError, match="the host read model_servers"):
        read.read("example-host-a")


def test_a_negative_count_is_refused_naming_the_read() -> None:
    read = CommandHostRead(_settings(), run=_FakeRun("-2\n"))
    with pytest.raises(ValueError, match="the host read model_servers"):
        read.read("example-host-a")


def test_a_timeout_of_a_read_propagates() -> None:
    def timed_out(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["example-host-read"], timeout=30)

    read = CommandHostRead(_settings(), run=timed_out)
    with pytest.raises(subprocess.TimeoutExpired):
        read.read("example-host-a")
