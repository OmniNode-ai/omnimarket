# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models overlay``: write the developer model overlay (OMN-20330)."""

from __future__ import annotations

from pathlib import Path

import click

from omnimarket.nodes.node_model_overlay_write_effect.handlers.handler_model_overlay_write import (
    HandlerModelOverlayWrite,
)
from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_request import (
    ModelModelOverlayWriteRequest,
)
from omnimarket.nodes.node_model_overlay_write_effect.protocols.protocol_overlay_file_store_backends import (
    FilesystemOverlayFileStore,
)


@click.command("overlay")
@click.option(
    "--mode",
    type=click.Choice(["native", "local-stack", "lab-stack"], case_sensitive=False),
    required=True,
    help="Which setup the overlay is for.",
)
@click.option("--ollama-memory-gb", type=click.IntRange(min=0), default=None)
@click.option("--url", default=None, help="Base URL of a model server you run.")
@click.option(
    "--model", "served_model_id", default=None, help="Model id the server serves."
)
@click.option("--parameter-count", default=None, help="Model size label, such as 27B.")
@click.option("--context-window", type=click.IntRange(min=1), default=None)
@click.option("--max-tokens", type=click.IntRange(min=1), default=None)
@click.option(
    "--target",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write here instead of the mode's usual file.",
)
@click.option(
    "--marker",
    default=None,
    help="The first line the caller recognises its own files by (starts with #).",
)
def write_model_overlay(
    mode: str,
    ollama_memory_gb: int | None,
    url: str | None,
    served_model_id: str | None,
    parameter_count: str | None,
    context_window: int | None,
    max_tokens: int | None,
    target: Path | None,
    marker: str | None,
) -> None:
    """Write the model overlay for MODE so your local model is used (OMN-20330).

    Give --ollama-memory-gb to use the Ollama default for this machine, or --url,
    --model, --parameter-count and --context-window for a server you run. No key
    is read or written. A file you wrote yourself is left as it is.
    """
    try:
        request = ModelModelOverlayWriteRequest(
            mode=mode.lower().replace("-", "_"),
            source="url" if url else "ollama",
            memory_gb=ollama_memory_gb,
            url=url,
            served_model_id=served_model_id,
            parameter_count=parameter_count,
            context_window=context_window,
            max_tokens=max_tokens,
            target_path=target,
            marker_line=marker,
        )
    except ValueError as error:
        raise click.UsageError(str(error)) from None
    result = HandlerModelOverlayWrite(FilesystemOverlayFileStore()).handle(request)
    if result.outcome == "refused":
        raise click.ClickException(result.reason or "the overlay was not written")
    click.echo(f"{result.outcome}: {result.path} ({result.served_model_id})")
