# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-host link read history for the link gate (OMN-17427).

A host is benched on two consecutive lossy reads, so the reads are kept in one JSON file in the placement directory (a name no
slot marker can take). Reads closer together than ``LINK_READ_MIN_GAP_S`` are one observation (five lanes reading inside one
tick are not five reads), and a read older than ``LINK_HISTORY_MAX_AGE_S`` says nothing about the link now. A missing or damaged
file reads as no history, and one that cannot be written leaves the judgement on what was read.
"""

import json
import os
from pathlib import Path

from ..models.model_link_history import ModelLinkHistoryRequest, ModelLinkHistoryResult

LINK_HISTORY_FILE = "link-loss.json"
LINK_HISTORY_READS = 5
LINK_READ_MIN_GAP_S = 60.0
LINK_HISTORY_MAX_AGE_S = 30 * 60.0


def _entries(raw: object) -> list[tuple[float, float]]:
    """(epoch seconds, loss percent) pairs of one host's stored list, newest first; a malformed item is dropped."""
    nums = (int, float)
    pairs = (
        [
            (float(i[0]), float(i[1]))
            for i in raw
            if isinstance(i, list)
            and len(i) == 2
            and all(isinstance(v, nums) and not isinstance(v, bool) for v in i)
        ]
        if isinstance(raw, list)
        else []
    )
    return sorted(pairs, reverse=True)


def read_history(path: Path) -> dict[str, list[tuple[float, float]]]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return (
        {str(name): _entries(raw) for name, raw in body.items()}
        if isinstance(body, dict)
        else {}
    )


class HandlerLinkHistory:
    def handle(self, request: ModelLinkHistoryRequest) -> ModelLinkHistoryResult:
        """Earlier reads of each host (stored, under the max age, at least the min gap older than now); record this read.

        A host with no loss reading (the launching host, a ping with no summary) is neither judged nor stored.
        """
        path, now = Path(request.path), request.now
        history = read_history(path)
        priors: dict[str, tuple[float, ...]] = {}
        for name, loss in request.losses.items():
            stored = [
                (at, old)
                for at, old in history.get(name, [])
                if now - at < LINK_HISTORY_MAX_AGE_S
            ]
            if loss is not None:
                priors[name] = tuple(
                    old for at, old in stored if now - at >= LINK_READ_MIN_GAP_S
                )
                if not stored or now - stored[0][0] >= LINK_READ_MIN_GAP_S:
                    stored.insert(0, (now, loss))
            history[name] = stored[:LINK_HISTORY_READS]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(
                    {n: [list(e) for e in es] for n, es in history.items() if es}
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass
        return ModelLinkHistoryResult(priors=priors)
