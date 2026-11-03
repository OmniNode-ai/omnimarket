# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The harness-delegate path, run on the host where the rung's login is.

``HarnessDelegateCaller`` runs ``harness_delegate.py run --harness <rung harness>`` from
the omni plugin at ``$OMNI_PLUGIN_ROOT``, into a fresh run directory under
``$ONEX_STATE_DIR/agent_review/<rung run-directory leaf>``, and reads back the one receipt
and answer it wrote. Every value of the call comes from the rung config. The harness
refuses a login that is not a subscription one and never falls back to an API key; it
gives the harness CLI an allowlisted environment with no key, token or secret name. An
absent plugin root or state directory raises ``HarnessNotBoundError``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

from ..models import ModelHarnessRun, ModelHarnessRungConfig

PLUGIN_ROOT_ENV = "OMNI_PLUGIN_ROOT"
STATE_DIR_ENV = "ONEX_STATE_DIR"
_STATE_SUBDIR = "agent_review"


class HarnessNotBoundError(RuntimeError):
    """The harness path's environment is absent on this host."""


class HarnessDelegateCaller:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def _paths(self, config: ModelHarnessRungConfig) -> tuple[Path, Path]:
        root = self._environ.get(PLUGIN_ROOT_ENV, "").strip()
        state = self._environ.get(STATE_DIR_ENV, "").strip()
        if not root:
            raise HarnessNotBoundError(
                f"{PLUGIN_ROOT_ENV} is not set; no harness is bound"
            )
        if not state:
            raise HarnessNotBoundError(
                f"{STATE_DIR_ENV} is not set; no run directory is bound"
            )
        script = Path(root) / config.harness_script
        if not script.is_file():
            raise HarnessNotBoundError(f"{script} does not exist; no harness is bound")
        out_dir = Path(state) / _STATE_SUBDIR / config.run_dir_leaf / str(uuid.uuid4())
        return script, out_dir

    async def run(
        self, prompt: str, model: str, config: ModelHarnessRungConfig
    ) -> ModelHarnessRun:
        script, out_dir = self._paths(config)
        out_dir.mkdir(parents=True, exist_ok=False)
        prompt_file = out_dir / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            "run",
            "--harness",
            config.harness,
            "--model",
            model,
            "--task-class",
            config.task_class,
            "--effort",
            config.effort,
            "--call-budget-s",
            str(config.call_budget_seconds),
            "--prompt-file",
            str(prompt_file),
            "--check",
            'test -s "$HARNESS_ANSWER_FILE"',
            "--out-dir",
            str(out_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(self._environ),
        )
        await process.communicate()
        receipts = sorted(out_dir.glob("*/receipt.json"))
        if len(receipts) != 1:
            raise HarnessNotBoundError(
                f"the harness left {len(receipts)} receipts under {out_dir} "
                f"(exit {process.returncode})"
            )
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        answer_file = receipts[0].parent / "answer.md"
        answer = (
            answer_file.read_text(encoding="utf-8") if answer_file.is_file() else ""
        )
        return ModelHarnessRun(receipt=receipt, answer=answer)


__all__ = ["HarnessDelegateCaller", "HarnessNotBoundError"]
