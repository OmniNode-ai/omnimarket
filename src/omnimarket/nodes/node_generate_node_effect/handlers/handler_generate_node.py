"""HandlerGenerateNode — scaffold native Onex node packages."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_generate_node_effect.models.model_generate_node_command import (
    EnumNodeType,
    ModelGenerateNodeCommand,
)
from omnimarket.nodes.node_generate_node_effect.models.model_generate_node_result import (
    ModelGenerateNodeResult,
)


class HandlerGenerateNode:
    """Effect handler that scaffolds a new Onex node from templates."""

    def handle(self, command: ModelGenerateNodeCommand) -> ModelGenerateNodeResult:
        """Create the node file manifest and write files unless dry-run."""
        output_dir = Path(command.output_dir)
        files = _render_files(command)

        if not command.dry_run and output_dir.exists() and any(output_dir.iterdir()):
            raise FileExistsError(
                f"output_dir already exists and is not empty: {output_dir}"
            )

        if not command.dry_run:
            for relative_path, content in files:
                path = output_dir / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

        return ModelGenerateNodeResult(
            correlation_id=command.correlation_id,
            node_name=command.node_name,
            created_files=tuple(relative_path for relative_path, _ in files),
            output_dir=str(output_dir),
            dry_run=command.dry_run,
        )


def _render_files(command: ModelGenerateNodeCommand) -> list[tuple[str, str]]:
    stem = command.node_name.removeprefix("node_")
    pascal = _pascal(stem)
    handler_class = f"Handler{pascal}"
    request_model = f"Model{pascal}Request"
    result_model = f"Model{pascal}Result"
    return [
        (
            "contract.yaml",
            _contract_yaml(command, handler_class, request_model, result_model),
        ),
        ("metadata.yaml", _metadata_yaml(command)),
        ("__init__.py", _node_init(command.node_name, handler_class, stem)),
        ("handlers/__init__.py", '"""Handlers for generated node."""\n'),
        (
            f"handlers/handler_{stem}.py",
            _handler_py(stem, handler_class, request_model, result_model),
        ),
        ("models/__init__.py", '"""Models for generated node."""\n'),
        (f"models/model_{stem}_request.py", _request_model_py(request_model)),
        (f"models/model_{stem}_result.py", _result_model_py(result_model)),
        ("tests/__init__.py", ""),
        (
            "tests/test_golden_chain.py",
            _test_py(command.node_name, handler_class, request_model),
        ),
    ]


def _pascal(stem: str) -> str:
    return "".join(part.capitalize() for part in stem.split("_"))


def _kebab(value: str) -> str:
    return value.replace("_", "-")


def _purity(node_type: EnumNodeType) -> str:
    if node_type in (EnumNodeType.COMPUTE, EnumNodeType.REDUCER):
        return "pure"
    if node_type is EnumNodeType.EFFECT:
        return "effectful"
    return "impure"


def _contract_yaml(
    command: ModelGenerateNodeCommand,
    handler_class: str,
    request_model: str,
    result_model: str,
) -> str:
    stem = command.node_name.removeprefix("node_")
    topic = _kebab(stem)
    node_type = command.node_type.value
    return f"""---
name: {command.node_name}
contract_version: {{major: 1, minor: 0, patch: 0}}
node_type: {node_type}
node_version: {{major: 1, minor: 0, patch: 0}}
node_not_implemented: false

description: >
  Generated {node_type} node. Replace this description with the contract-owned
  behavior before enabling production traffic.

handler:
  module: omnimarket.nodes.{command.node_name}.handlers.handler_{stem}
  class: {handler_class}
  input_model: omnimarket.nodes.{command.node_name}.models.model_{stem}_request.{request_model}

descriptor:
  node_archetype: {node_type}
  purity: {_purity(command.node_type)}
  idempotent: true
  timeout_ms: 60000

event_bus:
  subscribe_topics:
    - onex.cmd.omnimarket.{topic}-start.v1
  publish_topics:
    - onex.evt.omnimarket.{topic}-completed.v1

metadata:
  transport_type: kafka
  generated_by: node_generate_node_effect
"""


def _metadata_yaml(command: ModelGenerateNodeCommand) -> str:
    return f"""name: {command.node_name}
version: "1.0.0"
description: "Generated {command.node_type.value} node"
entry_points:
  onex.nodes:
    {command.node_name}: "omnimarket.nodes.{command.node_name}"
capabilities:
  standalone: true
  full_runtime: false
  requires_network: false
dependencies: []
authors: ["OmniNode Platform Team"]
license: "MIT"
tags: ["generated", "{command.node_type.value}"]
node_role: "{command.node_type.value}"
"""


def _node_init(node_name: str, handler_class: str, stem: str) -> str:
    return f'''"""Generated {node_name} package."""

from omnimarket.nodes.{node_name}.handlers.handler_{stem} import {handler_class}

__all__ = ["{handler_class}"]
'''


def _handler_py(
    stem: str, handler_class: str, request_model: str, result_model: str
) -> str:
    return f'''"""Generated handler for {stem}."""

from __future__ import annotations

from omnimarket.nodes.node_{stem}.models.model_{stem}_request import {request_model}
from omnimarket.nodes.node_{stem}.models.model_{stem}_result import {result_model}


class {handler_class}:
    """Generated handler skeleton."""

    def handle(self, request: {request_model}) -> {result_model}:
        return {result_model}(status="ok")
'''


def _request_model_py(request_model: str) -> str:
    return f'''"""Generated request model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class {request_model}(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
'''


def _result_model_py(result_model: str) -> str:
    return f'''"""Generated result model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class {result_model}(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="ok", description="Generated handler status")
'''


def _test_py(node_name: str, handler_class: str, request_model: str) -> str:
    stem = node_name.removeprefix("node_")
    return f'''"""Golden-chain smoke test for generated {node_name}."""

from omnimarket.nodes.{node_name}.handlers.handler_{stem} import {handler_class}
from omnimarket.nodes.{node_name}.models.model_{stem}_request import {request_model}


def test_generated_handler_returns_ok() -> None:
    result = {handler_class}().handle({request_model}())

    assert result.status == "ok"
'''


__all__: list[str] = ["HandlerGenerateNode"]
