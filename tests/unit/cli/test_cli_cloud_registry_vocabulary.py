# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex cloud delegate --task-type`` reads its vocabulary from the authority (OMN-19407).

The command used to carry a tuple "transcribed from" the gateway's contract.
These tests assert a PROPERTY, not a list: whatever the authority's public
projection is, the command offers exactly that, and when the authority cannot
be read the command refuses by naming it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli import cli_cloud
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit


def _stand_in_authority(tmp_path: Path) -> ModelTaskClassAuthority:
    """An authority whose class names exist nowhere in production."""
    path = tmp_path / "task_class_contracts.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "task_classes": {
                    "alpha_probe": {
                        "gateway_exposure": "public",
                        "selection": {"priority": 1, "phrases": []},
                    },
                    "beta_probe": {
                        "gateway_exposure": "public",
                        "selection": {"priority": 1, "phrases": []},
                    },
                    "gamma_internal_probe": {
                        "gateway_exposure": "internal",
                        "selection": {"priority": 0, "phrases": []},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return load_task_class_authority(path)


def _help() -> str:
    result = CliRunner().invoke(cli_cloud.cloud_group, ["delegate", "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_help_lists_exactly_the_live_public_projection() -> None:
    output = " ".join(_help().split())
    live = load_task_class_authority()
    for name in live.public_task_classes:
        assert name in output
    for name in live.internal_task_classes:
        assert name not in output


def test_the_choices_follow_the_authority_not_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = _stand_in_authority(tmp_path)
    monkeypatch.setattr(cli_cloud, "load_task_class_authority", lambda: stand_in)
    task_type = next(
        param
        for param in cli_cloud.cloud_group.commands["delegate"].params
        if param.name == "task_type"
    )

    assert tuple(task_type.type.choices) == ("alpha_probe", "beta_probe")  # type: ignore[attr-defined]


def test_an_unreadable_authority_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreadable() -> ModelTaskClassAuthority:
        raise FileNotFoundError("task-class authority not found at /nowhere")

    monkeypatch.setattr(cli_cloud, "load_task_class_authority", _unreadable)

    result = CliRunner().invoke(
        cli_cloud.cloud_group, ["delegate", "a prompt", "--task-type", "document"]
    )

    assert result.exit_code != 0
    assert "task-class authority" in result.output
    assert "could not be read" in result.output
