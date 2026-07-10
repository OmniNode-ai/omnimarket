# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT handler: write generated node files to disk (tier-4a).

An EFFECT — it owns the filesystem I/O. It writes each generated file under the
command's ``target_root`` (refusing any path that escapes the root), then echoes
the accumulating ``ModelCodegenPipelineState`` back with the written paths, so
the orchestrator threads state through this hop for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from omnimarket.codegen.models import ModelFileWriteCommand, ModelFileWriteResult


class HandlerCodegenFileWriter:
    """EFFECT: write the generated node's files under the target root."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def handle(self, command: ModelFileWriteCommand) -> ModelFileWriteResult:
        root = Path(command.target_root).resolve()
        written: list[str] = []
        for generated in command.files:
            target = (root / generated.relative_path).resolve()
            if root != target and root not in target.parents:
                raise ValueError(
                    "refusing to write outside target_root: "
                    f"{generated.relative_path!r}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generated.content, encoding="utf-8")
            written.append(str(target))
        return ModelFileWriteResult(state=command.state, written_paths=tuple(written))


__all__ = ["HandlerCodegenFileWriter"]
