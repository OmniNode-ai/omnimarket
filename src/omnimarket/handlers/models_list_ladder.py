# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models list --ladder``: the order models are tried in, and their cost (OMN-23167).

``ladder_option`` decorates the ``list`` command: with ``--ladder`` it prints one
line per routable model (position, provider, model, cost class) and stops;
without it the command runs as it did. The cost class is ``free``, ``paid`` or
``unknown``; a model with no price in the catalogue is ``unknown``, never free.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import click

from omnimarket.handlers.handler_model_keys import MODEL_LABELS, model_handler
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
)

model_json = click.option(
    "--json", "as_json", is_flag=True, help="One JSON object per line."
)

_UNKNOWN_NOTE = "  (price not in the catalogue, not treated as free)"


def show_ladder(as_json: bool) -> None:
    """Print every routable model in the order it is tried, with its cost class."""
    result = model_handler().handle(ModelModelSetupRequest(operation="ladder"))
    if not result.ladder:
        if not as_json:
            click.echo("no model is set up. Add one with: onex models add <provider>")
        return
    for rung in result.ladder:
        if as_json:
            click.echo(rung.model_dump_json())
            continue
        note = _UNKNOWN_NOTE if rung.cost_class == "unknown" else ""
        click.echo(
            f"{rung.position}. {MODEL_LABELS[rung.provider]:<11} {rung.model}  "
            f"{rung.cost_class}{note}"
        )


def ladder_option(command: Callable[..., None]) -> Callable[..., None]:
    """Add ``--ladder`` to ``command``; when given, show the ladder instead of running it."""

    @click.option(
        "--ladder",
        is_flag=True,
        help=(
            "List each model in the order delegation tries it, with its provider "
            "and cost class: free, paid or unknown."
        ),
    )
    @functools.wraps(command)
    def wrapper(*args: Any, ladder: bool = False, **kwargs: Any) -> None:
        if ladder:
            show_ladder(bool(kwargs.get("as_json")))
            return
        command(*args, **kwargs)

    return wrapper
