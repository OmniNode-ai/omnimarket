# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGeneratedExecutor — dynamic executor for generated node handlers.

Pre-wired at runtime startup. Responsibilities:

on_deploy_event(payload, input_data) — B1 (OMN-12826):
    Consumer entrypoint wired onto onex.cmd.omnimarket.node-deploy.v1. Deploys
    the generated source to the sandbox, invokes the handler via importlib, and
    returns/emits the terminal result in the SAME shape future runtime dispatch
    (NodeInvocationAdapter.dispatch) produces — explicitly labelled non-hot-load
    (_runtime_backend="sandbox", hot_load=False). This is sandbox importlib
    invoke, NOT runtime dynamic dispatch or image mutation.

deploy(payload):
    Receives a node-deploy event payload, writes contract.yaml + handler.py to
    the sandbox directory, and registers the node in the internal dispatch table.

execute(node_name, input_data):
    Loads the generated handler.py from sandbox at invocation time via importlib.
    Re-imports on each call so hot-written updates are picked up without restart.
    Called when an MCP tool (exposed by ServiceMCPToolSync) is invoked.

Full flow (no runtime restart):
  1. node_generation_consumer emits onex.cmd.omnimarket.node-deploy.v1
  2. on_deploy_event() deploys source to sandbox, invokes, emits the terminal
     result on onex.evt.omnimarket.generated-node-invoked.v1
  3. ServiceMCPToolSync receives the platform.node-registration.v1 event, hot-reloads tool metadata
  4. Next MCP call to that tool → execute() loads handler from disk → runs it
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_SANDBOX = Path(".onex_state/hackathon/generated")
_HANDLER_FILENAME = "handler.py"
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Sync (topic, bytes) -> None publisher injected by the runtime's Kafka adapter,
# identical to the contract HandlerGenerationConsumer uses.
EventPublisher = Callable[[str, bytes], None]


def _coerce_deploy_payload(payload: object) -> dict[str, Any]:
    """Normalize runtime deploy payload shapes to a mutable dict."""
    if isinstance(payload, (bytes, str)):
        loaded: object = json.loads(payload)
        if isinstance(loaded, dict):
            return cast(dict[str, Any], loaded)
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    if hasattr(payload, "model_dump"):
        dumped = payload.model_dump(mode="json")
        if isinstance(dumped, dict):
            return cast(dict[str, Any], dumped)
    raise TypeError(f"Unsupported deploy payload type: {type(payload).__name__}")


def _load_event_bus_topics(contract_path: Path | None = None) -> list[str]:
    """Read the node's subscribe + publish topics from the contract.

    Topics are NEVER hardcoded in the handler — they are resolved from
    contract.yaml so the wiring stays contract-driven (CLAUDE.md repo rule:
    "Keep event topics declared in contract.yaml; avoid hardcoded topic strings
    in handlers.").
    """
    p = contract_path or _CONTRACT_PATH
    with open(p) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    event_bus: dict[str, Any] = data.get("event_bus", {})
    topics: list[str] = []
    topics.extend(event_bus.get("subscribe_topics", []))
    topics.extend(event_bus.get("publish_topics", []))
    return topics


class HandlerGeneratedExecutor:
    """Receives deploy events, writes sandbox files, executes generated handlers.

    Pre-wired at startup — no runtime restart ever required.

    B1 (OMN-12826) — node-deploy consumer + terminal result:
        ``on_deploy_event`` is the consumer entrypoint for
        ``onex.cmd.omnimarket.node-deploy.v1``. It writes the generated source to
        the sandbox, invokes the handler via importlib, and returns the SAME
        terminal result shape future runtime dispatch
        (``NodeInvocationAdapter.dispatch``) produces — explicitly labelled
        non-hot-load (``_runtime_backend="sandbox"``, ``hot_load=False``). When a
        publisher is wired it also emits the terminal result on the
        contract-declared ``generated-node-invoked`` topic.
    """

    def __init__(
        self,
        sandbox_dir: Path | None = None,
        event_publisher: EventPublisher | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self.sandbox_dir = sandbox_dir or _DEFAULT_SANDBOX
        self._event_publisher: EventPublisher | None = event_publisher
        # node_name → handler_path, populated by deploy()
        self._registry: dict[str, Path] = {}

        topics = _load_event_bus_topics(contract_path)
        deploy_topic = next((t for t in topics if "node-deploy" in t), "")
        if not deploy_topic:
            raise ValueError(
                "contract.yaml event_bus must declare a node-deploy topic; "
                "the executor's command topic is contract-driven, not hardcoded"
            )
        terminal_topic = next((t for t in topics if "generated-node-invoked" in t), "")
        if not terminal_topic:
            raise ValueError(
                "contract.yaml event_bus.publish_topics must declare the "
                "generated-node-invoked terminal topic; it is contract-driven"
            )
        self._deploy_topic = deploy_topic
        self._terminal_topic = terminal_topic

    @property
    def deploy_topic(self) -> str:
        """Command topic this executor consumes (contract-resolved)."""
        return self._deploy_topic

    @property
    def terminal_topic(self) -> str:
        """Terminal result topic this executor emits (contract-resolved)."""
        return self._terminal_topic

    def handle(self, payload: object) -> dict[str, Any]:
        """Runtime dispatch entrypoint for the node-deploy command."""
        return self.on_deploy_event(payload)

    def deploy(self, payload: object) -> dict[str, Any]:
        """Receive a node-deploy event, write sandbox files, register for execution.

        Accepts the raw event payload as a dict, JSON bytes/string, or typed
        Pydantic deploy model.
        Returns {"status": "ok", "node_name": ...} on success or {"error": ...}.
        """
        try:
            data = _coerce_deploy_payload(payload)
        except ValueError as exc:
            return {"error": f"Invalid deploy payload JSON: {exc}"}
        except TypeError as exc:
            return {"error": str(exc)}

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
            (node_dir / _HANDLER_FILENAME).write_text(handler_source)
            if contract_yaml:
                (node_dir / "contract.yaml").write_text(contract_yaml)
        except OSError as exc:
            logger.warning(
                "[generated-executor] failed to write sandbox files for %s: %s",
                node_name,
                exc,
            )
            return {"error": f"Failed to write sandbox files: {exc}"}

        handler_path = node_dir / _HANDLER_FILENAME
        self._registry[node_name] = handler_path
        logger.info("[generated-executor] deployed %s → %s", node_name, handler_path)
        return {
            "status": "ok",
            "node_name": node_name,
            "handler_path": str(handler_path),
        }

    def execute(self, node_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        handler_path = self.sandbox_dir / node_name / _HANDLER_FILENAME

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

    # ------------------------------------------------------------------
    # B1 (OMN-12826): node-deploy consumer entrypoint + terminal result
    # ------------------------------------------------------------------

    def on_deploy_event(
        self,
        payload: object,
        input_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Consume a node-deploy command: sandbox-load, invoke, emit terminal.

        This is the entrypoint the runtime wires onto
        ``onex.cmd.omnimarket.node-deploy.v1``. It deploys the generated source
        to the sandbox, invokes the handler via importlib, and returns the
        terminal result in the SAME shape future runtime dispatch
        (``NodeInvocationAdapter.dispatch``) produces — so the demo loop does
        not bake in a demo-only result shape.

        The terminal result is explicitly labelled non-hot-load: this is a
        sandbox importlib invoke, NOT runtime dynamic dispatch or image
        mutation (``_runtime_backend="sandbox"``, ``hot_load=False``).

        When an ``event_publisher`` is wired, the terminal result is also
        emitted on the contract-declared ``generated-node-invoked`` topic.
        """
        try:
            data = _coerce_deploy_payload(payload)
        except (TypeError, ValueError) as exc:
            return self._terminal(
                status="failed",
                correlation_id="",
                node_name="",
                error=(
                    f"Invalid deploy payload JSON: {exc}"
                    if isinstance(exc, ValueError)
                    else str(exc)
                ),
            )

        correlation_id = str(data.get("correlation_id", ""))
        node_name = str(data.get("node_name", ""))

        deploy_result = self.deploy(data)
        if "error" in deploy_result:
            return self._terminal(
                status="failed",
                correlation_id=correlation_id,
                node_name=node_name,
                error=str(deploy_result["error"]),
            )

        invoke_input = input_data if input_data is not None else {}
        output = self.execute(node_name, invoke_input)
        if "error" in output:
            return self._terminal(
                status="failed",
                correlation_id=correlation_id,
                node_name=node_name,
                error=str(output["error"]),
            )

        return self._terminal(
            status="completed",
            correlation_id=correlation_id,
            node_name=node_name,
            output=output,
        )

    def _terminal(
        self,
        *,
        status: str,
        correlation_id: str,
        node_name: str,
        output: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Build (and emit) the terminal result in the future-dispatch shape.

        Mirrors the evidence keys ``NodeInvocationAdapter.dispatch`` returns
        (``_runtime_backend``, ``_event_bus_backend``, ``_state_store_backend``,
        ``_node_contract``, ``_command_topic``, ``status``) so a future runtime
        hot-load path can replace the sandbox path without changing the result
        contract. ``_runtime_backend="sandbox"`` + ``hot_load=False`` mark this
        as the non-hot-load today path.
        """
        result: dict[str, Any] = {
            "status": status,
            "correlation_id": correlation_id,
            "node_name": node_name,
            "hot_load": False,
            "_runtime_backend": "sandbox",
            "_event_bus_backend": "kafka" if self._event_publisher else "none",
            "_state_store_backend": "sandbox",
            "_node_contract": self._terminal_topic,
            "_command_topic": self._deploy_topic,
        }
        if output is not None:
            result["output"] = output
        if error:
            result["error"] = error

        if self._event_publisher is not None:
            try:
                self._event_publisher(self._terminal_topic, json.dumps(result).encode())
            except Exception as exc:
                logger.warning(
                    "[generated-executor] emit terminal to %s failed: %s",
                    self._terminal_topic,
                    exc,
                )
        return result


__all__: list[str] = ["HandlerGeneratedExecutor"]
