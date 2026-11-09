# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real backends of the J10 join: the lane-health projection for receipts, an HTTP read for the runtime (OMN-22957).

Every address, key name and timeout comes from ``ModelDevDeployJoinOverlay``. The receipts reader asks the same
projection row the merged-and-running count reads (``receipt_sha``, ``receipt_result``, ``receipt_observed_at``). The
runtime probe sends the asked correlation id in the overlay's header and reads the sha, the echoed correlation id and
the functional flag from the JSON body under the overlay's key names. A refused connection, a timeout, a non-2xx
status or a body that is not a JSON object is an unreachable runtime with the reason in ``detail``, never a raise.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from omnimarket.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
)
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_join_j10 import (
    ModelDevDeployJoinOverlay,
    ModelReceiptRead,
    ModelRuntimeAnswer,
    ModelRuntimeAsk,
)


def _http_get(url: str, headers: dict[str, str], timeout_seconds: int) -> str:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


class LaneHealthLabPassReceipts:
    """Reads the dev lane's lab pass receipts from the lane-health projection document."""

    def __init__(
        self,
        overlay: ModelDevDeployJoinOverlay,
        fetch: Callable[[str, dict[str, str], int], str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._fetch = fetch or _http_get

    def read_receipts(self, sha: str) -> ModelReceiptRead:
        overlay = self._overlay
        try:
            document: dict[str, Any] = json.loads(
                self._fetch(overlay.receipts_url, {}, overlay.timeout_seconds)
            )
            rows: list[dict[str, Any]] = document["rows"]
            found = tuple(
                ModelLabPassReceiptFact(
                    sha=str(row["receipt_sha"]),
                    lane=str(row["lane"]),
                    result="PASS"
                    if row.get("receipt_result") == overlay.receipt_pass_word
                    else "FAIL",
                    finished_at=datetime.fromisoformat(str(row["receipt_observed_at"])),
                )
                for row in rows
                if row.get("receipt_sha") == sha
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ModelReceiptRead(
                receipts=(),
                unread_reason=f"lab pass receipt projection unreadable: {exc}"[:200],
            )
        return ModelReceiptRead(receipts=found, unread_reason=None)


class HttpRuntimeProbe:
    """One correlated functional request to the running runtime over HTTP."""

    def __init__(
        self,
        overlay: ModelDevDeployJoinOverlay,
        fetch: Callable[[str, dict[str, str], int], str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._fetch = fetch or _http_get

    def ask(self, ask: ModelRuntimeAsk) -> ModelRuntimeAnswer:
        overlay = self._overlay
        down = ModelRuntimeAnswer(
            reachable=False,
            observed_sha=None,
            echoed_correlation_id=None,
            functional_ok=False,
            detail="",
        )
        try:
            body = json.loads(
                self._fetch(
                    overlay.runtime_url,
                    {overlay.runtime_correlation_header: ask.correlation_id},
                    overlay.timeout_seconds,
                )
            )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return down.model_copy(
                update={"detail": f"runtime unreachable: {exc}"[:200]}
            )
        if not isinstance(body, dict):
            return down.model_copy(
                update={"detail": "runtime answer is not a JSON object"}
            )
        sha = body.get(overlay.runtime_sha_key)
        echoed = body.get(overlay.runtime_correlation_key)
        return ModelRuntimeAnswer(
            reachable=True,
            observed_sha=sha.lower() if isinstance(sha, str) else None,
            echoed_correlation_id=echoed if isinstance(echoed, str) else None,
            functional_ok=body.get(overlay.runtime_ok_key) is True,
            detail="runtime answered",
        )


__all__: list[str] = ["HttpRuntimeProbe", "LaneHealthLabPassReceipts"]
