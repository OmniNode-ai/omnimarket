# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_codegen_file_writer_effect — real filesystem writes.

The EFFECT genuinely writes to disk (into a pytest tmp dir), so these assert the
files land with the right content, nested paths create their parents, the state
is echoed back, and a path that escapes the target root is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSpec,
    ModelFileWriteCommand,
    ModelGeneratedFile,
)
from omnimarket.nodes.node_codegen_file_writer_effect.handlers.handler_codegen_file_writer import (
    HandlerCodegenFileWriter,
)


def _command(
    target_root: Path, files: tuple[ModelGeneratedFile, ...]
) -> ModelFileWriteCommand:
    spec = ModelCodegenSpec(
        node_name="NodeGreeterCompute", namespace="ns", archetype="compute"
    )
    return ModelFileWriteCommand(
        state=ModelCodegenPipelineState(spec=spec),
        target_root=str(target_root),
        files=files,
    )


class TestFileWriter:
    def test_writes_files_with_content(self, tmp_path: Path) -> None:
        files = (
            ModelGeneratedFile(relative_path="handler.py", content="x = 1\n"),
            ModelGeneratedFile(relative_path="contract.yaml", content="name: x\n"),
        )
        result = HandlerCodegenFileWriter().handle(_command(tmp_path, files))
        assert (tmp_path / "handler.py").read_text() == "x = 1\n"
        assert (tmp_path / "contract.yaml").read_text() == "name: x\n"
        assert len(result.written_paths) == 2

    def test_nested_path_creates_parent(self, tmp_path: Path) -> None:
        files = (
            ModelGeneratedFile(relative_path="handlers/handler.py", content="y = 2\n"),
        )
        HandlerCodegenFileWriter().handle(_command(tmp_path, files))
        assert (tmp_path / "handlers" / "handler.py").read_text() == "y = 2\n"

    def test_state_is_echoed_back(self, tmp_path: Path) -> None:
        files = (ModelGeneratedFile(relative_path="a.py", content="a = 1\n"),)
        command = _command(tmp_path, files)
        result = HandlerCodegenFileWriter().handle(command)
        assert result.state == command.state

    def test_path_escaping_root_is_refused(self, tmp_path: Path) -> None:
        files = (ModelGeneratedFile(relative_path="../escape.py", content="danger\n"),)
        with pytest.raises(ValueError, match="outside target_root"):
            HandlerCodegenFileWriter().handle(_command(tmp_path, files))
        # nothing was written outside the root.
        assert not (tmp_path.parent / "escape.py").exists()
