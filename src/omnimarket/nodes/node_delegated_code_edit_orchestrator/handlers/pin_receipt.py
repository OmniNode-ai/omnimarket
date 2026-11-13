"""Delegate receipt reading for the backend pin of a delegated code edit run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from omnimarket.nodes.node_delegated_code_edit_orchestrator import ModelDelegatePin


def pin_violation(receipt_path: Path, pin: ModelDelegatePin) -> str:
    """Why a finished turn was not served by ``pin``'s backend and host, or "".

    Reads the delegate receipt: the run's resolved backend, its own pin
    verdict, and the backend and host of every attempt. A turn whose receipt
    cannot be read, or names no backend, is unverifiable and counts as a
    violation, because the pin is only worth what the receipt can prove.
    """
    try:
        raw = json.loads(receipt_path.read_text())
    except (OSError, ValueError, UnicodeError, RecursionError):
        return f"the delegate receipt {receipt_path} is unreadable"
    if not isinstance(raw, dict):
        return f"the delegate receipt {receipt_path} is not an object"
    receipt = cast("dict[str, object]", raw)
    problems: list[str] = []
    if receipt.get("backend_pin_honoured") is False:
        problems.append("the delegate reported its backend pin not honoured")
    backend_id = receipt.get("backend_id")
    if not backend_id and _served_then_vetoed(receipt, pin):
        # A pinned run whose answer a gate refused names no backend of its own
        # (no accepted attempt authored it), yet every attempt names the pinned
        # backend: the pin was served and the model's reply was the failure.
        pass
    elif not backend_id:
        problems.append("the receipt names no backend")
    elif backend_id != pin.backend_id:
        problems.append(f"backend {backend_id!r}")
    for attempt in _attempts(receipt):
        attempt_backend = attempt.get("backend_id")
        attempt_host = attempt.get("host")
        if attempt_backend and attempt_backend != pin.backend_id:
            problems.append(f"an attempt on backend {attempt_backend!r}")
        if attempt_host and attempt_host != pin.host:
            problems.append(f"an attempt on host {attempt_host}")
    return "; ".join(dict.fromkeys(problems))


def _served_then_vetoed(receipt: dict[str, object], pin: ModelDelegatePin) -> bool:
    """True when the pinned backend answered and a quality gate refused the answer."""
    if receipt.get("terminal_failure_reason") != "quality_veto":
        return False
    attempts = _attempts(receipt) or _receipt_attempts(receipt)
    return bool(attempts) and all(
        attempt.get("backend_id") == pin.backend_id for attempt in attempts
    )


def _receipt_attempts(receipt: dict[str, object]) -> list[dict[str, object]]:
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list):
        return []
    return [a for a in attempts if isinstance(a, dict)]


def _attempts(receipt: dict[str, object]) -> list[dict[str, object]]:
    inner = receipt.get("receipt")
    result = inner.get("result") if isinstance(inner, dict) else None
    terminal = result.get("terminal_payload") if isinstance(result, dict) else None
    payload = terminal.get("payload") if isinstance(terminal, dict) else None
    for holder in (payload, result):
        attempts = holder.get("attempts") if isinstance(holder, dict) else None
        if isinstance(attempts, list):
            return [a for a in attempts if isinstance(a, dict)]
    return []
