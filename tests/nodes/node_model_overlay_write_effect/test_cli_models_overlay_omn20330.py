# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models overlay``: the command onboarding calls (OMN-20330)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group

pytestmark = pytest.mark.unit


def test_native_overlay_is_written_through_the_command(tmp_path: Path) -> None:
    target = tmp_path / "bifrost_overrides.yaml"
    result = CliRunner().invoke(
        models_group,
        [
            "overlay",
            "--mode",
            "native",
            "--ollama-memory-gb",
            "16",
            "--target",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"written: {target}")
    assert (
        yaml.safe_load(target.read_text())["backends"][0]["backend_id"] == "local-coder"
    )


def test_the_lab_stack_mode_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "o.yaml"
    result = CliRunner().invoke(
        models_group,
        [
            "overlay",
            "--mode",
            "lab-stack",
            "--ollama-memory-gb",
            "16",
            "--target",
            str(target),
        ],
    )
    assert result.exit_code != 0
    assert "out of the MVP" in result.output
    assert not target.exists()


def test_a_url_without_its_model_is_a_usage_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        models_group,
        [
            "overlay",
            "--mode",
            "native",
            "--url",
            "http://h:1",
            "--target",
            str(tmp_path / "o.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "served_model_id" in result.output
