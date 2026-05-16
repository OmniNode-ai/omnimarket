# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGeneratedExecutor — dynamic executor for generated node handlers.

Pre-wired at runtime startup. When a generated MCP tool is invoked:
  1. ServiceMCPToolSync has already hot-reloaded the tool metadata (no restart needed)
  2. The MCP call routes here
  3. This executor loads the generated handler.py from the sandbox at invocation time
  4. Executes handle(input_data) and returns the result

No runtime restart required — the tool appears via Kafka hot-reload and executes via
dynamic import from the sandbox directory.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_SANDBOX = Path(".onex_state/hackathon/generated")


class HandlerGeneratedExecutor:
    """Executes dynamically generated node handlers without runtime restart.

    Pre-wired at startup — loads generated code from sandbox at invocation time.
    Each execute() call re-imports the module so updated handlers are picked up
    without restarting the executor.
    """

    def __init__(self, sandbox_dir: Path | None = None) -> None:
        self.sandbox_dir = sandbox_dir or _DEFAULT_SANDBOX

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
            spec.loader.exec_module(module)  # type: ignore[union-attr]
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
