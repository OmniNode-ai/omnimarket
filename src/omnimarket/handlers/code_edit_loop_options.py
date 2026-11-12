# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The loop-limit options of ``onex code-edit run`` (OMN-24006).

The round limit, the check time limit and the wall budget each fall back to the
node's overlay, then to the request model's own default (the wall budget: no limit).
"""

from __future__ import annotations

from collections.abc import Callable

import click


def loop_limit_options[F: Callable[..., object]](command: F) -> F:
    """Declare --max-turns, --check-timeout-s and --wall-budget-s on a click command."""
    for option in (
        click.option(
            "--wall-budget-s",
            type=click.IntRange(1, 7200),
            default=None,
            help="The wall budget in seconds; omitted, the node's overlay decides, else no limit.",
        ),
        click.option(
            "--check-timeout-s",
            type=click.IntRange(1, 1800),
            default=None,
            help="The check time limit in seconds; omitted, the node's overlay decides, else the request model's default.",
        ),
        click.option(
            "--max-turns",
            type=click.IntRange(1, 40),
            default=None,
            help="The round limit; omitted, the node's overlay decides, else the request model's default.",
        ),
    ):
        command = option(command)
    return command
