# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sender the outbox settle runs with: the host's ledger bus publisher, run as a subprocess.

``PublisherSender(argv_prefix)`` is a ``Sender`` (``sender(payload, request_id) -> SendResult``). It runs the declared
publisher with the rows on stdin and reads the last JSON line of its stdout as the receipt. A publisher that cannot be
started, or that does not return in time, answers exit 75 (pending) so the settle stops and keeps the request.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

from omnimarket.nodes.node_ledger_outbox_settle_effect.handlers.handler_outbox_settle import (
    SendResult,
)

EXIT_PENDING = 75
EXIT_FOREIGN_RECEIPT = 70


def _last_receipt(stdout: str) -> dict[str, object] | None:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
    return None


class PublisherSender:
    """Sends one request through the publisher command and answers its exit code, receipt and stderr."""

    def __init__(
        self,
        argv_prefix: Sequence[str],
        *,
        lane: str,
        host: str,
        timeout_s: float = 60.0,
    ) -> None:
        if not argv_prefix:
            raise ValueError("the publisher argv prefix is empty")
        self._prefix = list(argv_prefix)
        self._lane = lane
        self._host = host
        self._timeout_s = timeout_s

    def __call__(self, payload: str, request_id: str) -> SendResult:
        argv = [
            *self._prefix,
            "--lane", self._lane,
            "--host", self._host,
            "--request-id", request_id,
            "--rows-file", "-",
            "--timeout-s", f"{self._timeout_s:g}",
        ]  # fmt: skip
        try:
            done = subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self._timeout_s + 60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SendResult(
                EXIT_PENDING, request_id, None, f"{type(exc).__name__}: {exc}"
            )
        receipt = _last_receipt(done.stdout)
        if receipt is not None and receipt.get("request_id") not in (None, request_id):
            return SendResult(
                EXIT_FOREIGN_RECEIPT,
                request_id,
                None,
                f"the publisher answered for request {receipt.get('request_id')!r}, not {request_id}",
            )
        return SendResult(done.returncode, request_id, receipt, done.stderr)
