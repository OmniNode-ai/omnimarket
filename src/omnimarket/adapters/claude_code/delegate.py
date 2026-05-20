# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude Code delegation adapter — transport bridge over node_delegate_skill_orchestrator.

This adapter is the only place that knows how to reach the delegation runtime from
Claude Code. It resolves message routing from named contract fields
(``runtime_dispatch.command_topic`` and ``runtime_dispatch.terminal_events.{success,failure}``)
rather than positional list indices, builds a typed command envelope, and waits for
the correlated success or failure terminal event.

The underlying Pattern B runtime client is wrapped internally; its Codex-specific
naming is never surfaced on this adapter's interface or in user-facing output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict

_ALLOWED_TASK_TYPES = (
    "test",
    "document",
    "research",
    "code_generation",
    "refactor",
    "reasoning",
    "complex_reasoning",
    "planning",
    "review",
    "summarization",
    "agent_delegation",
    "escalation",
)
_ALLOWED_SOURCES = ("claude-code", "codex")

# Contract lives in the installed package, not the repo working directory.
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)


class DelegationTopics(BaseModel):
    """Resolved routing for the delegation runtime, read from contract named fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_topic: str
    success_topic: str
    failure_topic: str
    default_timeout_ms: int
    max_timeout_ms: int


def resolve_topics_from_contract(
    contract_path: Path | None = None,
) -> DelegationTopics:
    """Read routing from the contract's ``runtime_dispatch`` block.

    Fails loudly with ``KeyError`` if ``runtime_dispatch`` or any of its required
    named fields are missing — never falls back to positional list lookups.
    """
    path = contract_path or _CONTRACT_PATH
    contract: dict[str, Any] = yaml.safe_load(path.read_text())
    rd = contract["runtime_dispatch"]
    terminal = rd["terminal_events"]
    return DelegationTopics(
        command_topic=rd["command_topic"],
        success_topic=terminal["success"],
        failure_topic=terminal["failure"],
        default_timeout_ms=int(rd["default_timeout_ms"]),
        max_timeout_ms=int(rd["max_timeout_ms"]),
    )


def _coerce_correlation_id(correlation_id: str | UUID | None) -> UUID:
    if correlation_id is None:
        return uuid4()
    if isinstance(correlation_id, UUID):
        return correlation_id
    try:
        return UUID(str(correlation_id))
    except ValueError as exc:
        raise ValueError(
            f"correlation_id must be a UUID string, got {correlation_id!r}"
        ) from exc


def _validate_task_type(task_type: str) -> str:
    if task_type not in _ALLOWED_TASK_TYPES:
        raise ValueError(
            f"task_type must be one of {_ALLOWED_TASK_TYPES}, got {task_type!r}"
        )
    return task_type


def _validate_source(source: str) -> str:
    if source not in _ALLOWED_SOURCES:
        raise ValueError(f"source must be one of {_ALLOWED_SOURCES}, got {source!r}")
    return source


def build_delegation_payload(
    *,
    prompt: str,
    task_type: str,
    source: str,
    cwd: str | None = None,
    wait: bool = True,
    max_tokens: int = 2048,
    correlation_id: str | UUID | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the payload object carried inside the delegation command envelope."""
    if not prompt:
        raise ValueError("prompt must be a non-empty string")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
    ):
        raise ValueError("max_tokens must be a positive integer")
    cid = _coerce_correlation_id(correlation_id)
    payload: dict[str, Any] = {
        "prompt": prompt,
        "task_type": _validate_task_type(task_type),
        "source": _validate_source(source),
        "wait": wait,
        "max_tokens": max_tokens,
        "correlation_id": str(cid),
        "metadata": dict(metadata or {}),
    }
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


class DelegationDispatchAdapter:
    """Adapter that compiles and (optionally) dispatches delegation commands."""

    def __init__(self, contract_path: Path | None = None) -> None:
        self._topics = resolve_topics_from_contract(contract_path)

    @property
    def topics(self) -> DelegationTopics:
        return self._topics

    def _envelope(
        self,
        *,
        prompt: str,
        task_type: str,
        source: str,
        cwd: str | None,
        wait: bool,
        max_tokens: int,
        correlation_id: str | UUID | None,
        metadata: dict[str, str] | None,
    ) -> dict[str, Any]:
        payload = build_delegation_payload(
            prompt=prompt,
            task_type=task_type,
            source=source,
            cwd=cwd,
            wait=wait,
            max_tokens=max_tokens,
            correlation_id=correlation_id,
            metadata=metadata,
        )
        return {
            "command_topic": self._topics.command_topic,
            "terminal_events": {
                "success": self._topics.success_topic,
                "failure": self._topics.failure_topic,
            },
            "correlation_id": payload["correlation_id"],
            "payload": payload,
            "timeout_ms": self._topics.default_timeout_ms,
        }

    def compile_request(
        self,
        *,
        prompt: str,
        task_type: str,
        source: str,
        cwd: str | None = None,
        wait: bool = True,
        max_tokens: int = 2048,
        correlation_id: str | UUID | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the exact command envelope without touching the message bus."""
        return self._envelope(
            prompt=prompt,
            task_type=task_type,
            source=source,
            cwd=cwd,
            wait=wait,
            max_tokens=max_tokens,
            correlation_id=correlation_id,
            metadata=metadata,
        )

    def dispatch_sync(
        self,
        *,
        prompt: str,
        task_type: str,
        source: str,
        cwd: str | None = None,
        wait: bool = True,
        max_tokens: int = 2048,
        correlation_id: str | UUID | None = None,
        metadata: dict[str, str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Publish the delegation command and wait for the correlated terminal event.

        Returns ``{"ok": bool, "correlation_id": str, ...}``. A failure terminal event
        returns ``ok: false`` with a typed error; a timeout returns
        ``status="timeout"`` matching the contract's timeout behavior.
        """
        # Imported lazily so that compile-only / payload-building paths and unit
        # tests do not require the runtime transport stack.
        from omnimarket.adapters.codex.runtime_client import (
            CodexRuntimeRequestAdapter,
        )

        envelope = self._envelope(
            prompt=prompt,
            task_type=task_type,
            source=source,
            cwd=cwd,
            wait=wait,
            max_tokens=max_tokens,
            correlation_id=correlation_id,
            metadata=metadata,
        )
        effective_timeout = min(
            timeout_ms or self._topics.default_timeout_ms,
            self._topics.max_timeout_ms,
        )
        runtime_adapter = CodexRuntimeRequestAdapter(
            requester=source,
            command_topic=self._topics.command_topic,
        )
        response = runtime_adapter.dispatch_sync(
            command_name="delegate_skill.orchestrate",
            payload=cast(dict[str, object], envelope["payload"]),
            correlation_id=envelope["correlation_id"],
            timeout_ms=effective_timeout,
            response_topic=self._topics.success_topic,
            additional_response_topics=(self._topics.failure_topic,),
        )
        result: dict[str, Any] = response.model_dump(mode="json", exclude_none=True)
        result["command_topic"] = self._topics.command_topic
        result["terminal_events"] = {
            "success": self._topics.success_topic,
            "failure": self._topics.failure_topic,
        }
        return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, help="User prompt to delegate.")
    parser.add_argument(
        "--task-type",
        required=True,
        help=f"Task classification, one of {_ALLOWED_TASK_TYPES}.",
    )
    parser.add_argument(
        "--source",
        default="claude-code",
        help=f"Registered adapter source, one of {_ALLOWED_SOURCES}.",
    )
    parser.add_argument("--cwd", default=None, help="Working directory context.")
    parser.add_argument(
        "--max-tokens", type=int, default=2048, help="Max output tokens."
    )
    parser.add_argument(
        "--correlation-id", default=None, help="Optional correlation UUID."
    )
    parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Do not wait for a synchronous result.",
    )
    parser.add_argument(
        "--compile-only",
        action="store_true",
        help="Print the command envelope without publishing to the message bus.",
    )
    parser.set_defaults(wait=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``onex-delegate``."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        task_type = _validate_task_type(args.task_type)
        source = _validate_source(args.source)
        correlation_id = _coerce_correlation_id(args.correlation_id)
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, indent=2) + "\n")
        return 2

    adapter = DelegationDispatchAdapter()

    if args.compile_only:
        envelope = adapter.compile_request(
            prompt=args.prompt,
            task_type=task_type,
            source=source,
            cwd=args.cwd,
            wait=args.wait,
            max_tokens=args.max_tokens,
            correlation_id=correlation_id,
        )
        result = {"ok": True, **envelope}
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return 0

    try:
        result = adapter.dispatch_sync(
            prompt=args.prompt,
            task_type=task_type,
            source=source,
            cwd=args.cwd,
            wait=args.wait,
            max_tokens=args.max_tokens,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "correlation_id": str(correlation_id), "error": str(exc)},
                indent=2,
            )
            + "\n"
        )
        return 1

    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
