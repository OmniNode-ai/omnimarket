# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the day-one listing: load the overlay, read the sources from its backend, call the handler.

``inmemory`` takes the sources it is given; ``directory`` reads every file under the staged copy's root
(the directory named by the overlay's ``copy_root_env``) whose suffix the overlay includes.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.nodes.node_landing_private_reads_effect.handlers.handler_landing_private_reads import (
    HandlerLandingPrivateReads,
    ProtocolPrivateReadsEventSink,
)
from omnimarket.nodes.node_landing_private_reads_effect.models.model_landing_private_reads import (
    ModelLandingPrivateReadsOverlay,
    ModelLandingPrivateReadsRequest,
    ModelLandingPrivateReadsResult,
    ModelSourceFile,
)
from omnimarket.nodes.node_landing_private_reads_effect.overlay import load_overlay


class PrivateReadsSourceError(RuntimeError):
    """The staged copy cannot be read: the variable is unset or names no directory."""


def read_directory_sources(
    overlay: ModelLandingPrivateReadsOverlay, environ: Mapping[str, str]
) -> list[ModelSourceFile]:
    root_value = environ.get(overlay.copy_root_env, "").strip()
    if not root_value:
        raise PrivateReadsSourceError(f"{overlay.copy_root_env} is unset")
    root = Path(root_value)
    if not root.is_dir():
        raise PrivateReadsSourceError(f"{overlay.copy_root_env} names no directory")
    sources: list[ModelSourceFile] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in overlay.exclude_prefixes):
            continue
        if path.is_file() and path.suffix in overlay.include_suffixes:
            sources.append(
                ModelSourceFile(
                    path=relative,
                    text=path.read_text(encoding="utf-8", errors="replace"),
                )
            )
    return sources


def run(
    environ: Mapping[str, str] | None = None,
    *,
    sources: Sequence[ModelSourceFile] = (),
    sink: ProtocolPrivateReadsEventSink | None = None,
    correlation_id: str | None = None,
) -> ModelLandingPrivateReadsResult:
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    if overlay.source_backend == "directory":
        files = read_directory_sources(overlay, env)
    else:
        files = list(sources)
    request = ModelLandingPrivateReadsRequest(
        correlation_id=correlation_id or str(uuid.uuid4()),
        overlay=overlay,
        sources=files,
    )
    return HandlerLandingPrivateReads(sink).handle(request)


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        result = run()
    except PrivateReadsSourceError as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "verdict": "NOT_WORKING",
                    "failure_code": "source_unreadable",
                    "reason": str(exc),
                }
            )
            + "\n"
        )
        return 2
    sys.stdout.write(
        json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    return 0 if result.verdict == "WORKING" else 1


if __name__ == "__main__":
    sys.exit(main())
