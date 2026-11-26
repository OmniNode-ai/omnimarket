# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``onex models`` group: its help, and a hint for a bare provider name.

``onex models gemini`` is not a command; the working form is
``onex models add gemini``. A listed provider name given bare exits non-zero
with a one-line hint naming that form; any other unknown name keeps Click's own
refusal unchanged.
"""

from __future__ import annotations

import click

from omnimarket.handlers import model_provider_labels as provider_labels

MODELS_HELP = (
    "Set up the models delegation can use: Google Gemini, OpenRouter, OpenAI, "
    "Reflection AI, Anthropic, Z.AI, or a Local Model: any model server "
    "you run, by its endpoint URL.\n\nSet up any "
    "combination; delegation chooses among them for each task.\n\n"
    "Add a provider after setup with: onex models add <provider>."
)


class ModelsGroup(click.Group):
    """``onex models`` group with bare-provider hints."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not ctx.resilient_parsing:
            name = args[0].lower()
            if name in provider_labels.TYPED_PROVIDERS and name not in self.commands:
                raise click.UsageError(
                    f"No such command '{args[0]}'. "
                    f"Did you mean 'onex models add {name}'?",
                    ctx=ctx,
                )
        return super().resolve_command(ctx, args)


__all__ = ["MODELS_HELP", "ModelsGroup"]
