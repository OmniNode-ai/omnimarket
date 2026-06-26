# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGeneratedExecutor — dynamic executor for generated node handlers.

Pre-wired at runtime startup. Responsibilities:

on_deploy_event(payload, input_data) — B1 (OMN-12826) + Phase 0.1 (OMN-13605):
    Consumer entrypoint wired onto onex.cmd.omnimarket.node-deploy.v1. Performs
    BOTH halves of the generation spine:
      (a) hot-load: deploys the generated handler to the sandbox, invokes it via
          importlib, and emits the terminal result in the SAME shape future
          runtime dispatch (NodeInvocationAdapter.dispatch) produces — explicitly
          labelled non-hot-load today (_runtime_backend="sandbox",
          hot_load=False). This is sandbox importlib invoke, NOT runtime dynamic
          dispatch or image mutation.
      (b) full-package scaffold: invokes the node_generate_node_effect scaffolder
          to write the full 10-file canonical node package into a worktree
          staging directory, so the generated node has a real, PR-able canonical
          source tree (consumed downstream by the Phase 0.2 publish effect).

deploy(payload):
    Receives a node-deploy event payload, writes contract.yaml + handler.py to
    the sandbox directory, and registers the node in the internal dispatch table.

execute(node_name, input_data):
    Loads the generated handler.py from sandbox at invocation time via importlib.
    Re-imports on each call so hot-written updates are picked up without restart.
    Called when an MCP tool (exposed by ServiceMCPToolSync) is invoked.

scaffold_package(node_name, contract_yaml, correlation_id):
    Invokes the node_generate_node_effect scaffolder through its contract-declared
    public handler to materialize the full 10-file canonical package under the
    worktree staging root. Returns the staging directory and the created-file
    manifest.

Full flow (no runtime restart):
  1. node_generation_consumer emits onex.cmd.omnimarket.node-deploy.v1
  2. on_deploy_event() deploys source to sandbox, invokes it, scaffolds the full
     canonical package into the staging dir, and emits the terminal result on
     onex.evt.omnimarket.generated-node-invoked.v1
  3. ServiceMCPToolSync receives the platform.node-registration.v1 event, hot-reloads tool metadata
  4. Next MCP call to that tool → execute() loads handler from disk → runs it
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml

logger = logging.getLogger(__name__)

_HANDLER_FILENAME = "handler.py"
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# OMN-12854: the generated-node sandbox MUST live under the runtime's writable
# state root, NOT a hardcoded relative path. A relative ".onex_state" resolves
# against the runtime's effective CWD, which is not writable in the deployed
# container (Errno 13 Permission denied) — the generated source was never
# materialized and sandbox-invoke had no artifact to import. The state root is
# resolved from the runtime-provided env (ONEX_STATE_DIR preferred, then
# ONEX_STATE_ROOT) and FAILS FAST when unset — no silent relative fallback
# (CLAUDE.md Rule 6/8: no hardcoded paths, no silent defaults).
#
# OMN-13605 (Phase 0.1): the prior sandbox subdir was the hackathon-tagged
# ("hackathon", "generated"). The node is now a permanent canonical artifact, so
# the sandbox lives under a node-scoped, hackathon-free subdir. The full
# canonical package is scaffolded separately under the staging root below.
_SANDBOX_SUBDIR = ("node_generation_consumer", "sandbox")
_STATE_ROOT_ENV_KEYS = ("ONEX_STATE_DIR", "ONEX_STATE_ROOT")
_REPLAY_STATE_SUBDIR = ("node_generation_consumer", "generated_executor_replay")

# OMN-13605 (Phase 0.1): worktree staging root for the full 10-file canonical
# package. Resolved fail-fast from a dedicated env var (preferred) or the runtime
# state root — never a hardcoded path. The full package is written here so the
# Phase 0.2 publish effect has a real canonical source tree to commit + PR.
_STAGING_DIR_ENV_KEYS = ("ONEX_GENERATED_STAGING_DIR",)
_STAGING_SUBDIR = ("node_generation_consumer", "staging")

# A scaffold-able node name must satisfy the scaffolder's own pattern
# (^node_[a-z][a-z0-9_]*$). Generated contracts occasionally carry a name that
# does not (e.g. "unknown"); we detect that here and skip scaffolding rather than
# raising a ValidationError mid-invoke.
_SCAFFOLD_NODE_NAME_RE = re.compile(r"^node_[a-z][a-z0-9_]*$")


def _resolve_sandbox_dir() -> Path:
    """Resolve the generated-node sandbox under the runtime writable state root.

    Reads ONEX_STATE_DIR (preferred) or ONEX_STATE_ROOT. Raises a fail-fast
    error when neither is set so a misconfigured runtime surfaces immediately
    instead of silently writing (or failing to write) a relative path.
    """
    for key in _STATE_ROOT_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value).joinpath(*_SANDBOX_SUBDIR)
    raise RuntimeError(
        "HandlerGeneratedExecutor requires a writable runtime state root: set "
        f"one of {_STATE_ROOT_ENV_KEYS} (no hardcoded relative sandbox fallback)."
    )


def _resolve_staging_dir() -> Path:
    """Resolve the worktree staging root for the full canonical package.

    Prefers the dedicated ONEX_GENERATED_STAGING_DIR. Falls back to the runtime
    state root + staging subdir. FAILS FAST when neither a dedicated staging dir
    nor a state root is configured — no hardcoded relative fallback
    (CLAUDE.md Rule 6/8).
    """
    for key in _STAGING_DIR_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value)
    state_root = _resolve_state_root()
    if state_root is not None:
        return state_root.joinpath(*_STAGING_SUBDIR)
    raise RuntimeError(
        "HandlerGeneratedExecutor requires a writable staging root: set "
        f"one of {_STAGING_DIR_ENV_KEYS} or one of {_STATE_ROOT_ENV_KEYS} "
        "(no hardcoded relative staging fallback)."
    )


def _resolve_state_root() -> Path | None:
    for key in _STATE_ROOT_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value)
    return None


def _replay_state_path(correlation_id: str) -> Path | None:
    state_root = _resolve_state_root()
    if state_root is None:
        return None
    digest = hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()
    return state_root.joinpath(*_REPLAY_STATE_SUBDIR) / f"{digest}.json"


def _coerce_correlation_uuid(correlation_id: str) -> UUID:
    """Coerce a correlation_id string into a UUID for the scaffolder command.

    The deploy payload's correlation_id is a free-form string (e.g. "corr-1"),
    but the scaffolder command requires a UUID. A non-UUID string is mapped
    deterministically via uuid5 so the same correlation_id always yields the
    same scaffold correlation — preserving replay determinism.
    """
    try:
        return UUID(correlation_id)
    except (ValueError, AttributeError, TypeError):
        return uuid5(NAMESPACE_URL, correlation_id or "")


def _node_type_from_contract(contract_yaml: str) -> str:
    """Extract the declared node_type from a generated contract.yaml.

    Returns the lower-cased node_type value, defaulting to "compute" — the
    generation consumer's stated purpose is generating COMPUTE nodes — when the
    contract is empty/unparseable or omits node_type.
    """
    if not contract_yaml.strip():
        return "compute"
    try:
        data = yaml.safe_load(contract_yaml)
    except yaml.YAMLError:
        return "compute"
    if not isinstance(data, dict):
        return "compute"
    node_type = data.get("node_type", "")
    return str(node_type).strip().lower() or "compute"


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
        staging_dir: Path | None = None,
    ) -> None:
        # OMN-12854: an explicit sandbox_dir (tests) wins; otherwise resolve from
        # the runtime writable state root, fail-fast if unset — never a relative
        # default.
        self.sandbox_dir = (
            sandbox_dir if sandbox_dir is not None else _resolve_sandbox_dir()
        )
        # OMN-13605 (Phase 0.1): worktree staging root for the full 10-file
        # canonical package. An explicit staging_dir (tests) wins; otherwise it is
        # resolved lazily on first scaffold so construction does not require a
        # staging root to be configured for the sandbox-only paths.
        self._staging_dir_override = staging_dir
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

    @property
    def staging_dir(self) -> Path:
        """Worktree staging root for the full canonical package (resolved lazily).

        OMN-13605 (Phase 0.1): an explicit override (tests) wins; otherwise it is
        resolved fail-fast from the dedicated staging env or the runtime state
        root. Never a hardcoded relative path.
        """
        if self._staging_dir_override is not None:
            return self._staging_dir_override
        return _resolve_staging_dir()

    def handle(self, payload: object) -> dict[str, Any]:
        """Runtime dispatch entrypoint for the node-deploy command."""
        return self.on_deploy_event(payload)

    def scaffold_package(
        self,
        node_name: str,
        contract_yaml: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Scaffold the full 10-file canonical package into the staging dir.

        OMN-13605 (Phase 0.1): invokes the ``node_generate_node_effect``
        scaffolder through its contract-declared public handler
        (``HandlerGenerateNode``) to materialize the full canonical node package
        — contract.yaml, metadata.yaml, package + handler + model + test modules
        — under ``{staging_dir}/{node_name}``. This is the canonical, PR-able
        source tree the Phase 0.2 publish effect commits and opens a PR for.

        The node archetype is taken from the generated contract's ``node_type``
        (default ``compute`` — the generation consumer generates COMPUTE nodes).
        Returns ``{"status": "ok", "staging_dir": ..., "created_files": [...]}``
        on success, or ``{"error": ...}`` when the package cannot be scaffolded.
        The scaffold step is best-effort relative to the hot-load path: a scaffold
        failure does not fail the in-session invocation, it is surfaced as an
        ``error`` field in the terminal result's ``scaffold`` block.
        """
        # node_name must satisfy the scaffolder's own pattern; an "unknown" or
        # malformed name (e.g. an unparseable generated contract) is skipped
        # rather than raising mid-invoke.
        if not _SCAFFOLD_NODE_NAME_RE.match(node_name):
            return {
                "error": (
                    f"node_name {node_name!r} does not match the scaffolder "
                    "pattern ^node_[a-z][a-z0-9_]*$; skipping full-package scaffold"
                )
            }

        # Invoke the scaffolder through its contract-declared public handler. The
        # command/result models live in the shared omnimarket.models package (not
        # the scaffolder's node-local models), so importing them here is NOT a
        # cross-node reach-in (test_no_cross_node_reach_in.py / OMN-9263). Imported
        # lazily so the sandbox-only paths never pay the import cost.
        try:
            from omnimarket.models.model_generate_node import (
                EnumNodeType,
                ModelGenerateNodeCommand,
            )
            from omnimarket.nodes.node_generate_node_effect.handlers.handler_generate_node import (
                HandlerGenerateNode,
            )
        except ImportError as exc:
            return {"error": f"node_generate_node_effect scaffolder unavailable: {exc}"}

        node_type_value = _node_type_from_contract(contract_yaml)
        try:
            node_type = EnumNodeType(node_type_value)
        except ValueError:
            # An unrecognised node_type (e.g. EFFECT_GENERIC) falls back to the
            # generation consumer's default archetype, COMPUTE.
            node_type = EnumNodeType.COMPUTE

        try:
            staging_root = self.staging_dir
        except RuntimeError as exc:
            return {"error": f"staging root unresolved: {exc}"}

        output_dir = staging_root / node_name
        try:
            command = ModelGenerateNodeCommand(
                correlation_id=_coerce_correlation_uuid(correlation_id),
                node_name=node_name,
                node_type=node_type,
                output_dir=str(output_dir),
                dry_run=False,
            )
            result = HandlerGenerateNode().handle(command)
        except Exception as exc:
            logger.warning(
                "[generated-executor] scaffold of %s into %s failed: %s",
                node_name,
                output_dir,
                exc,
            )
            return {"error": f"scaffold failed: {exc}"}

        logger.info(
            "[generated-executor] scaffolded %d-file canonical package for %s → %s",
            len(result.created_files),
            node_name,
            output_dir,
        )
        return {
            "status": "ok",
            "staging_dir": result.output_dir,
            "created_files": list(result.created_files),
        }

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

        replayed = self._load_replay_terminal(correlation_id)
        if replayed is not None:
            logger.info(
                "[generated-executor] replay correlation_id=%s; "
                "returning stored terminal without deploy/invoke emit",
                correlation_id,
            )
            return replayed

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

        # OMN-13605 (Phase 0.1): after the hot-load invoke proves the generated
        # node is invokable in-session, scaffold the full 10-file canonical
        # package into the worktree staging dir. The scaffold is reported in the
        # terminal's ``scaffold`` block but does NOT fail the in-session
        # invocation — the hot-load and full-package halves are independent.
        contract_yaml = str(data.get("contract_yaml", ""))
        scaffold = self.scaffold_package(node_name, contract_yaml, correlation_id)

        return self._terminal(
            status="completed",
            correlation_id=correlation_id,
            node_name=node_name,
            output=output,
            scaffold=scaffold,
        )

    def _load_replay_terminal(self, correlation_id: str) -> dict[str, Any] | None:
        if not correlation_id:
            return None
        path = _replay_state_path(correlation_id)
        if path is None or not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[generated-executor] ignoring unreadable replay marker for %s: %s",
                correlation_id,
                exc,
            )
            return None
        return loaded if isinstance(loaded, dict) else None

    def _record_replay_terminal(self, result: dict[str, Any]) -> None:
        correlation_id = str(result.get("correlation_id", ""))
        if not correlation_id:
            return
        path = _replay_state_path(correlation_id)
        if path is None:
            logger.debug(
                "[generated-executor] no ONEX state root configured; "
                "replay guard disabled for correlation_id=%s",
                correlation_id,
            )
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:
            logger.warning(
                "[generated-executor] failed to persist replay marker for %s: %s",
                correlation_id,
                exc,
            )

    def _terminal(
        self,
        *,
        status: str,
        correlation_id: str,
        node_name: str,
        output: dict[str, Any] | None = None,
        error: str = "",
        scaffold: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build (and emit) the terminal result in the future-dispatch shape.

        Mirrors the evidence keys ``NodeInvocationAdapter.dispatch`` returns
        (``_runtime_backend``, ``_event_bus_backend``, ``_state_store_backend``,
        ``_node_contract``, ``_command_topic``, ``status``) so a future runtime
        hot-load path can replace the sandbox path without changing the result
        contract. ``_runtime_backend="sandbox"`` + ``hot_load=False`` mark this
        as the non-hot-load today path.

        OMN-13605 (Phase 0.1): when a full-package scaffold ran, its outcome
        (``staging_dir`` + ``created_files`` on success, or ``error``) is carried
        in the ``scaffold`` block so a consumer can locate the canonical package
        the Phase 0.2 publish effect commits and PRs.
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
        if scaffold is not None:
            result["scaffold"] = scaffold
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
        self._record_replay_terminal(result)
        return result


__all__: list[str] = ["HandlerGeneratedExecutor"]
