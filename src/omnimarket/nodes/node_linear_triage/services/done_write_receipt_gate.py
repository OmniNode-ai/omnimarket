# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Done-write receipt gate for the triage node's Done flips (OMN-20368).

Operator ruling (2026-10-02): a ticket reaches Done only on a PASS dod_verify
whose checks bind every acceptance criterion; a merged PR or ticked boxes is
never enough. The rule itself is
``omnibase_core.handlers.handler_done_write_receipt_gate`` -- the evidence-autoclose
closer's rule, lifted into the layer both repos import. This module is the
triage half: it runs the verifier node, reads the verdict it prints, and hands
verdict plus the ticket's current description to that rule.

It sits BEHIND :mod:`close_evidence_gate`, never in place of it: the evidence
gate says what kind of delivery proof the close carries (a merged PR, a
roll-up, a receipt); this gate says whether the ticket's own acceptance
criteria are proven. The first is necessary and the second decides.

Fail-closed at every step. A verifier that cannot be launched, times out,
prints no JSON, or reaches no verdict is a refusal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Protocol, runtime_checkable

from omnibase_core.handlers.handler_done_write_receipt_gate import (
    evaluate_done_write_receipt,
    extract_dod_verify_verdict,
)
from omnibase_core.models.ticket.model_done_write_decision import (
    ModelDoneWriteDecision,
)

#: A verifier run executes the contract's checks, so it is slow by design.
DOD_VERIFY_TIMEOUT_SECONDS = 300.0


@runtime_checkable
class DodVerdictProbe(Protocol):
    """Source of the dod_verify verdict for one ticket. Injectable for tests."""

    def verdict_for(self, *, ticket_id: str) -> tuple[dict[str, object] | None, str]:
        """``(verdict, "")`` when one was reached, else ``(None, reason)``."""
        ...


def dod_verify_argv(ticket_id: str) -> list[str]:
    """Argv that runs the verifier node from THIS process's own environment.

    The node's own entry point under the running interpreter, not ``onex skill``
    and not ``uv run`` from a cwd: the verifier's environment is a property of
    how this process was composed, and the node CLI carries ``--execution-audience``
    itself, so no omnibase_infra release has to be new enough to map it. Run
    against a ``.201`` lab checkout (2026-10-02) the infra skill CLI at 0.38.59
    refused that flag outright; the node entry point answered.
    """
    return [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_dod_verify",
        "--ticket-id",
        ticket_id,
        "--execution-audience",
        "hosted",
    ]


def _verdict_from_stdout(stdout: str) -> tuple[dict[str, object] | None, str]:
    """The verdict off the verifier's stdout, whichever shape it printed.

    The node entry point prints the verdict itself (``ModelDodVerifyState`` as
    JSON, flat); the ``onex skill`` receipt wraps it and names its arm in
    ``result_model``. Both are read; an object that is neither is no verdict.
    """
    try:
        printed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"printed no JSON verdict: {exc}"
    if not isinstance(printed, dict):
        return None, "output was not a JSON object"
    if "result_model" in printed:
        return extract_dod_verify_verdict(printed)
    if "total_checks" in printed:
        return printed, ""
    return None, "the output carries no `total_checks`, so no verdict was reached"


class DodVerifySubprocessProbe:
    """Default :class:`DodVerdictProbe`: the dod_verify node, run as a subprocess.

    stdout is parsed REGARDLESS of exit code. The node exits non-zero on every
    genuine evidence gap while still printing the complete verdict; discarding
    it would report "the ticket is not proven" as "the verifier crashed".
    """

    def __init__(self, timeout_seconds: float = DOD_VERIFY_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def verdict_for(self, *, ticket_id: str) -> tuple[dict[str, object] | None, str]:
        argv = dod_verify_argv(ticket_id)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, f"Timeout running dod_verify for {ticket_id}"
        except OSError as exc:
            return None, f"OS error launching dod_verify for {ticket_id}: {exc}"
        verdict, why = _verdict_from_stdout(proc.stdout)
        if verdict is None:
            detail = proc.stderr.strip() if proc.returncode != 0 else ""
            return None, (
                f"dod_verify exit_code={proc.returncode}: {why}"
                + (f" ({detail[-300:]})" if detail else "")
            )
        return verdict, ""


class DoneWriteReceiptRefusedError(Exception):
    """Raised when the Done-write receipt gate refuses a close.

    Attributes:
        ticket_id: The Linear identifier the close was attempted for.
        decision: The structured refusal verdict (``allowed=False`` + reason).
    """

    def __init__(self, ticket_id: str, decision: ModelDoneWriteDecision) -> None:
        self.ticket_id = ticket_id
        self.decision = decision
        super().__init__(f"Refusing Done flip for {ticket_id}: {decision.reason}")


def enforce_done_write_receipt(
    *, ticket_id: str, description: str | None, probe: DodVerdictProbe
) -> ModelDoneWriteDecision:
    """Run the verifier and the core rule; raise on a refusal.

    ``description`` is the ticket's CURRENT description; ``None`` (it could not
    be read) refuses, because criteria that cannot be read cannot be judged.
    """
    if description is None:
        decision = ModelDoneWriteDecision(
            allowed=False,
            reason=(
                f"the description of {ticket_id} could not be read, so its "
                "acceptance criteria cannot be judged (OMN-20368)."
            ),
        )
        raise DoneWriteReceiptRefusedError(ticket_id, decision)
    verdict, reason = probe.verdict_for(ticket_id=ticket_id)
    if verdict is None:
        decision = ModelDoneWriteDecision(
            allowed=False,
            reason=f"no dod_verify verdict for {ticket_id}: {reason}",
        )
        raise DoneWriteReceiptRefusedError(ticket_id, decision)
    decision = evaluate_done_write_receipt(
        ticket_id=ticket_id, description=description, verdict=verdict
    )
    if not decision.allowed:
        raise DoneWriteReceiptRefusedError(ticket_id, decision)
    return decision


__all__ = [
    "DOD_VERIFY_TIMEOUT_SECONDS",
    "DodVerdictProbe",
    "DodVerifySubprocessProbe",
    "DoneWriteReceiptRefusedError",
    "dod_verify_argv",
    "enforce_done_write_receipt",
]
