# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bare provider names on ``onex models`` suggest ``onex models add`` (OMN-23825).

Failure modes guarded against:
- a listed provider invoked bare does not point users to ``onex models add``;
- an unknown command loses Click's own ``No such command`` refusal;
- a capitalised provider name is not normalised to its lowercase form;
- ``onex models --help`` omits the ``onex models add`` hint.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.handlers import model_provider_labels as provider_labels

pytestmark = pytest.mark.unit


def test_bare_listed_provider_hints_at_add() -> None:
    runner = CliRunner()
    for provider in provider_labels.LISTED_PROVIDERS:
        result = runner.invoke(models_group, [provider])
        assert result.exit_code != 0
        assert f"onex models add {str(provider).lower()}" in result.output


def test_unknown_command_keeps_click_refusal() -> None:
    runner = CliRunner()
    result = runner.invoke(models_group, ["definitely-not-a-provider"])
    assert result.exit_code != 0
    assert "No such command" in result.output
    assert "onex models add" not in result.output


def test_capitalised_provider_name_hints_lowercase() -> None:
    runner = CliRunner()
    result = runner.invoke(models_group, ["Gemini"])
    assert result.exit_code != 0
    assert "onex models add gemini" in result.output


def test_models_help_mentions_add() -> None:
    runner = CliRunner()
    result = runner.invoke(models_group, ["--help"])
    assert result.exit_code == 0
    assert "onex models add" in result.output


def test_shell_completion_after_a_bare_provider_does_not_raise() -> None:
    """Completion parses resiliently: the hint is for a run, not for <TAB>."""
    from click.shell_completion import ShellComplete

    completer = ShellComplete(models_group, {}, "onex models", "_ONEX_COMPLETE")
    completer.get_completions(["gemini"], "")
