# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The harness path of a review vote: one ``onex delegate`` pinned to the rung's harness backend.

``HarnessBackendCaller`` runs ``onex delegate --backend-id <rung backend>`` from the
registry's ``omnibase_infra/scripts/onex`` on the rung's bus lane, so the delegation goes
to the harness handler of ``node_llm_delegation_call_effect``, which runs the harness on
the host where its subscription login is, judges the answer and leaves one cost record per
attempt. It reads the delegation's receipt back from a fresh run directory under
``$ONEX_STATE_DIR/agent_review/<rung run-directory leaf>`` and states it in the receipt
shape the vote check reads: the harness and login the rung config names when the accepted
attempt ran on the rung's backend, the model that attempt used, and the delegation's
correlation id as the delegation event id. Every value of the call comes from the rung
config. An absent registry root or state directory raises ``HarnessNotBoundError``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..models import ModelHarnessRun, ModelHarnessRungConfig

REGISTRY_ROOT_ENV = "OMNI_HOME"
STATE_DIR_ENV = "ONEX_STATE_DIR"
_STATE_SUBDIR = "agent_review"
_ONEX_RELATIVE = ("omnibase_infra", "scripts", "onex")
_ACCEPT = "accept"
_DONE = "done"
_FAILED = "run-failed"


class HarnessNotBoundError(RuntimeError):
    """The harness path's environment is absent on this host, or it left no receipt."""


def _tail(text: str, limit: int = 400) -> str:
    return text.strip()[-limit:]


def _document(stdout: str) -> dict[str, Any]:
    """The one JSON document ``onex delegate --json`` prints as its last stdout line."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        document = json.loads(lines[-1]) if lines else None
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise HarnessNotBoundError("onex delegate printed no JSON receipt")
    return document


def receipt_from_document(
    document: Mapping[str, Any], config: ModelHarnessRungConfig
) -> ModelHarnessRun:
    """The run a delegation's receipt supports, in the shape the vote check reads."""
    receipt = document.get("receipt", document)
    result = receipt.get("result", receipt) if isinstance(receipt, Mapping) else {}
    if not isinstance(result, Mapping):
        result = {}
    attempts = [a for a in result.get("attempts") or [] if isinstance(a, Mapping)]
    accepted = [a for a in attempts if a.get("acceptance_decision") == _ACCEPT]
    answer = str(result.get("response") or result.get("content") or "")
    if result.get("status") != "completed" or not accepted:
        detail = str(result.get("error_message") or result.get("status") or "")
        return ModelHarnessRun(
            receipt={
                "outcome": _FAILED,
                "error": {"detail": detail or "no attempt was accepted"},
            },
            answer=answer,
        )
    attempt = accepted[-1]
    on_rung = attempt.get("backend_id") == config.backend_id
    correlation_id = str(result.get("correlation_id") or "")
    return ModelHarnessRun(
        receipt={
            "harness": config.harness if on_rung else attempt.get("backend_id"),
            "login": config.login if on_rung else None,
            "model": attempt.get("model_used"),
            "outcome": _DONE,
            "event": {"published": bool(correlation_id), "event_id": correlation_id},
        },
        answer=answer,
    )


class HarnessBackendCaller:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def _paths(self, config: ModelHarnessRungConfig) -> tuple[Path, Path]:
        root = self._environ.get(REGISTRY_ROOT_ENV, "").strip()
        state = self._environ.get(STATE_DIR_ENV, "").strip()
        if not root:
            raise HarnessNotBoundError(
                f"{REGISTRY_ROOT_ENV} is not set; no onex is bound"
            )
        if not state:
            raise HarnessNotBoundError(
                f"{STATE_DIR_ENV} is not set; no run directory is bound"
            )
        onex = Path(root).joinpath(*_ONEX_RELATIVE)
        if not onex.is_file():
            raise HarnessNotBoundError(f"{onex} does not exist; no onex is bound")
        out_dir = Path(state) / _STATE_SUBDIR / config.run_dir_leaf / str(uuid.uuid4())
        return onex, out_dir

    async def run(
        self, prompt: str, model: str, config: ModelHarnessRungConfig
    ) -> ModelHarnessRun:
        onex, out_dir = self._paths(config)
        out_dir.mkdir(parents=True, exist_ok=False)
        prompt_file = out_dir / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            str(onex),
            "delegate",
            "--prompt-file",
            str(prompt_file),
            "--task-type",
            config.task_class,
            "--backend-id",
            config.backend_id,
            "--bus",
            "kafka",
            "--lane",
            config.delegate_lane,
            "--locus",
            "deployed-lane",
            "--timeout",
            str(math.ceil(config.call_budget_seconds)),
            "--state-root",
            str(out_dir),
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(self._environ),
        )
        out, err = await process.communicate()
        try:
            document = _document(out.decode("utf-8", errors="replace"))
        except HarnessNotBoundError as exc:
            raise HarnessNotBoundError(
                f"{exc} (exit {process.returncode}): "
                f"{_tail(err.decode('utf-8', errors='replace'))}"
            ) from None
        return receipt_from_document(document, config)


__all__ = ["HarnessBackendCaller", "HarnessNotBoundError", "receipt_from_document"]
