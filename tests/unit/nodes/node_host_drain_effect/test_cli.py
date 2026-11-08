# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of node_host_drain_effect on the jsonl profile: the drain survives between two commands (OMN-22905)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_drain_effect import runtime_host_drain as cli
from omnimarket.nodes.node_host_drain_effect.overlay import ModelHostDrainOverlay


def _jsonl(
    overlay: ModelHostDrainOverlay, home: Path
) -> tuple[ModelHostDrainOverlay, dict[str, str]]:
    # Lane counts come from a command the test points at /bin/echo-like python; only the profile changes here.
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    document["lanes"]["count_command"] = ["python3", "-c", "print(0)", "{host}"]
    return (
        ModelHostDrainOverlay.model_validate(document),
        {overlay.emission.home_env: str(home)},
    )


def test_cli_drain_then_check_start_refuses_across_two_commands(
    overlay: ModelHostDrainOverlay, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded, environ = _jsonl(overlay, tmp_path)
    assert (
        cli.run(["drain", "--host", "example-host-a"], overlay=loaded, environ=environ)
        == 0
    )
    drained = json.loads(capsys.readouterr().out)
    assert drained["draining"] is True
    assert drained["drained"] is True
    assert (
        cli.run(
            ["check-start", "--host", "example-host-a"], overlay=loaded, environ=environ
        )
        == 1
    )
    refused = json.loads(capsys.readouterr().out)
    assert refused["outcome"] == "start_refused"
    assert cli.run(["draining"], overlay=loaded, environ=environ) == 0
    assert capsys.readouterr().out.split() == ["example-host-a"]
    journal = (tmp_path / loaded.emission.journal_relpath).read_text(encoding="utf-8")
    assert '"phase":"failure"' in journal.replace(" ", "")


def test_cli_without_the_emission_home_is_indeterminate(
    overlay: ModelHostDrainOverlay,
) -> None:
    loaded, _ = _jsonl(overlay, Path("/unused"))
    assert cli.run(["draining"], overlay=loaded, environ={}) == 3
