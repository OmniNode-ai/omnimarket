# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handoff check effect: assemble a session handoff and refuse it when a ruling is unaccounted for.

Three commands, one handler. ``assemble`` builds the handoff from the ledger, the PR watcher
reader and the live probes, and refuses (exit 1) when a ruling or consent in the window has no
status of done, in_flight, dropped or held; an unhealthy service is recorded and never refuses.
``resume`` reads the newest committed handoff and re-probes its stale lines. ``record-slice``
records a scrubbed ledger slice as a test fixture. Exit: 0 ok, 1 refused, 2 bad input, 3 no
handoff found. The policy is the ``handoff_check`` section of this node's contract.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_handoff_check_effect.handlers.assemble import assemble
from omnimarket.nodes.node_handoff_check_effect.handlers.record_slice import (
    record_slice,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.resume import resume
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelHandoffCheckRequest,
    ModelHandoffCheckResult,
    ModelHandoffPolicy,
)

POLICY_KEY = "handoff_check"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_policy(contract_path: Path = CONTRACT_PATH) -> ModelHandoffPolicy:
    """The declared handoff policy, read from the node contract."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get(POLICY_KEY), dict):
        raise ValueError(f"contract: missing {POLICY_KEY} section")
    return ModelHandoffPolicy.model_validate(raw[POLICY_KEY])


class HandlerHandoffCheck:
    """Assemble, resume and record-slice over the declared handoff contract."""

    def __init__(self, contract_path: Path | None = None) -> None:
        self._contract_path = contract_path or CONTRACT_PATH

    def handle(self, request: ModelHandoffCheckRequest) -> ModelHandoffCheckResult:
        ledger = request.ledger or os.environ.get("ONEX_LEDGER_PATH")
        if not ledger:
            return self._bad_input(
                request.command,
                "handoff_check: set ledger or ONEX_LEDGER_PATH (there is no default)",
            )
        try:
            policy = load_policy(self._contract_path)
            if request.command is EnumHandoffCommand.ASSEMBLE:
                return self._assemble(request, policy, ledger)
            if request.command is EnumHandoffCommand.RESUME:
                return resume(request, policy, ledger)
            return record_slice(request, policy, ledger)
        except (OSError, ValueError, KeyError, ValidationError, yaml.YAMLError) as exc:
            # json.JSONDecodeError is a ValueError.
            return self._bad_input(
                request.command, f"handoff_check: {type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _bad_input(
        command: EnumHandoffCommand, message: str
    ) -> ModelHandoffCheckResult:
        return ModelHandoffCheckResult(command=command, exit_code=2, errors=[message])

    @staticmethod
    def _assemble(
        request: ModelHandoffCheckRequest, policy: ModelHandoffPolicy, ledger: str
    ) -> ModelHandoffCheckResult:
        handoff = assemble(request, policy, ledger)
        dropped = handoff.dropped
        out = [dropped.summary_line()]
        out += [
            f"  dropped: {d.stamp} {d.row_type} ticket={d.ticket}"
            for d in dropped.dropped
        ]
        if handoff.refused:
            out.append(f"HANDOFF REFUSED: {len(handoff.refused)} item(s) unaccounted")
            out += [f"  UNACCOUNTED {item}" for item in handoff.refused]
            out.append(
                '  fix: add each to the statuses JSON as "<item>": {"status": "done|in_flight|dropped|held", "evidence": "<ledger row or PR>"}'
            )
            return ModelHandoffCheckResult(
                command=EnumHandoffCommand.ASSEMBLE,
                exit_code=1,
                output=out,
                refused=handoff.refused,
                dropped=dropped,
                stale_lines=handoff.stale_lines,
            )
        written: str | None = None
        if request.out_dir:
            target = Path(request.out_dir) / handoff.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(handoff.text, encoding="utf-8")
            written = str(target)
            out.append(
                f"HANDOFF OK: wrote {target} ({handoff.stale_lines} stale line(s))"
            )
        else:
            out.append(handoff.text)
            out.append(
                f"HANDOFF OK: not written (no out_dir); {handoff.stale_lines} stale line(s)"
            )
        return ModelHandoffCheckResult(
            command=EnumHandoffCommand.ASSEMBLE,
            exit_code=0,
            output=out,
            dropped=dropped,
            stale_lines=handoff.stale_lines,
            written_path=written,
        )


__all__ = ["HandlerHandoffCheck", "load_policy"]
