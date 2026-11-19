# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose installed model commands through the existing CLI extension loader."""

from importlib.metadata import entry_points

from omnibase_core.cli.cli_commands import load_cli_extensions

from omnimarket.cli.cli_secret import models_group

load_cli_extensions(models_group, entry_points(group="onex.models"))

__all__ = ["models_group"]
