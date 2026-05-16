# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGeneratedExecutor — dynamic executor for generated node handlers.

Pre-wired at runtime startup. Owns two responsibilities:

deploy(payload):
    Receives a node-deploy event payload, writes contract.yaml + handler.py to
    the sandbox directory, and registers the node in the internal dispatch table.
    Called by the Kafka consumer when onex.cmd.omnimarket.node-deploy.v1 arrives.

execute(node_name, input_data):
    Loads the generated handler.py from sandbox at invocation time via importlib.
    Re-imports on each call so hot-written updates are picked up without restart.
    Called when an MCP tool (exposed by ServiceMCPToolSync) is invoked.

Full flow (no runtime restart):
  1. node_generation_consumer emits onex.cmd.omnimarket.node-deploy.v1
  2. deploy() writes source to sandbox + registers node
  3. ServiceMCPToolSync receives node-registered event, hot-reloads tool metadata
  4. Next MCP call to that tool → execute() loads handler from disk → runs it
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_SANDBOX = Path(".onex_state/hackathon/generated")


class HandlerGeneratedExecutor:
    """Receives deploy events, writes sandbox files, executes generated handlers.

    Pre-wired at startup — no runtime restart ever required.
    """

    def __init__(self, sandbox_dir: Path | None = None) -> None:
        self.sandbox_dir = sandbox_dir or _DEFAULT_SANDBOX
        # node_name → handler_path, populated by deploy()
        self._registry: dict[str, Path] = {}

    def deploy(self, payload: dict[str, Any] | bytes | str) -> dict[str, Any]:
        """Receive a node-deploy event, write sandbox files, register for execution.

        Accepts the raw event payload as a dict, JSON bytes, or JSON string.
        Returns {"status": "ok", "node_name": ...} on success or {"error": ...}.
        """
        if isinstance(payload, (bytes, str)):
            try:
                data: dict[str, Any] = json.loads(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                return {"error": f"Invalid deploy payload JSON: {exc}"}
        else:
            data = payload

        node_name = data.get("node_name", "")
        contract_yaml = data.get("contract_yaml", "")
        handler_source = data.get("handler_source", "")

        if not node_name:
            return {"error": "deploy payload missing node_name"}
        if not handler_source:
            return {"error": f"deploy payload missing handler_source for {node_name}"}

        # Reject path traversal: no "..", no absolute paths, no path separators.
        if (
            ".." in node_name
            or node_name.startswith("/")
            or "/" in node_name
            or "\\" in node_name
        ):
            return {"error": f"deploy payload node_name is unsafe: {node_name!r}"}

        node_dir = self.sandbox_dir / node_name
        try:
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / "handler.py").write_text(handler_source)
            if contract_yaml:
                (node_dir / "contract.yaml").write_text(contract_yaml)
        except OSError as exc:
            logger.warning(
                "[generated-executor] failed to write sandbox files for %s: %s",
                node_name,
                exc,
            )
            return {"error": f"Failed to write sandbox files: {exc}"}

        handler_path = node_dir / "handler.py"
        self._registry[node_name] = handler_path
        logger.info("[generated-executor] deployed %s → %s", node_name, handler_path)
        return {
            "status": "ok",
            "node_name": node_name,
            "handler_path": str(handler_path),
        }

    def execute(self, node_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        handler_path = self.sandbox_dir / node_name / "handler.py"

        if not handler_path.exists():
            logger.warning("[generated-executor] handler not found: %s", handler_path)
            return {"error": f"Handler not found: {handler_path}"}

        try:
            spec = importlib.util.spec_from_file_location(
                f"generated.{node_name}", handler_path
            )
            if spec is None or spec.loader is None:
                return {"error": f"Could not create module spec for {handler_path}"}

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning(
                "[generated-executor] failed to load %s: %s", handler_path, exc
            )
            return {"error": f"Failed to load generated handler: {exc}"}

        if not hasattr(module, "handle"):
            return {"error": "Generated handler missing handle() function"}

        try:
            result = module.handle(input_data)
        except Exception as exc:
            logger.warning(
                "[generated-executor] handle() raised for %s: %s", node_name, exc
            )
            return {"error": f"Generated handler raised: {exc}"}

        if not isinstance(result, dict):
            return {"result": result}
        return result


__all__: list[str] = ["HandlerGeneratedExecutor"]
