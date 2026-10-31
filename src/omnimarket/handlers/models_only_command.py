# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models only``: limit delegation to local or free models (OMN-21267).

The ``onex models`` group takes its added commands from ``MODEL_COMMANDS``, and
``onex models list`` reads its rows through ``listed``, which says the
rule in force first.
"""

from __future__ import annotations

from typing import Literal, cast

import click

from omnimarket.handlers.delegation_model_scope import (
    MODEL_SCOPE_RULES,
    model_scope_text,
)
from omnimarket.handlers.handler_model_keys import model_handler
from omnimarket.handlers.models_first_command import first_model, models_text
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_result import (
    ModelModelStatus,
)

_CLEAR_HOW = "onex models only --clear removes it"


@click.command("only")
@click.argument(
    "rule", required=False, type=click.Choice(MODEL_SCOPE_RULES, case_sensitive=False)
)
@click.option(
    "--clear",
    is_flag=True,
    help="Remove the rule; delegation may use any model you set up again.",
)
def only_models(rule: str | None, clear: bool) -> None:
    """Limit delegation to local models only, or to free models only.

    With RULE ``local``, every delegation on this machine skips a model that is
    not one you run (a model added by URL, or Ollama). With ``free``, it skips a
    model the provider catalogue does not declare free; local models count as
    free. Each skip is recorded with its reason and no attempt is made on it. When
    the rule leaves no model, the delegation is refused before any provider call.
    """
    handler = model_handler()
    if clear:
        if rule is not None:
            raise click.ClickException("--clear takes no rule.")
        handler.handle(ModelModelSetupRequest(operation="clear_only"))
        click.echo("No model rule is set; delegation may use any model you set up.")
        return
    if rule is None:
        raise click.ClickException(
            "name the rule: onex models only local, or onex models only free. "
            f"{_CLEAR_HOW}."
        )
    chosen = cast(Literal["local", "free"], rule.lower())
    handler.handle(ModelModelSetupRequest(operation="set_only", only=chosen))
    click.echo(
        f"Delegation now uses {model_scope_text(chosen)}; any other model is "
        f"skipped. {_CLEAR_HOW}."
    )


def listed(as_json: bool) -> tuple[ModelModelStatus, ...]:
    """The ``onex models list`` rows; in text, the rule in force is said first.

    The JSON rows keep their shape, one provider per line.
    """
    result = model_handler().handle(ModelModelSetupRequest(operation="status"))
    if result.only is not None and not as_json:
        click.echo(f"Rule in force: {model_scope_text(result.only)} ({_CLEAR_HOW})")
    return result.providers


#: The commands ``onex models`` adds beside its own.
MODEL_COMMANDS: tuple[click.Command, ...] = (first_model, only_models)

__all__: list[str] = [
    "MODEL_COMMANDS",
    "listed",
    "models_text",
    "only_models",
]
