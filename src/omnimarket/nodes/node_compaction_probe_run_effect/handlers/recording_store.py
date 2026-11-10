# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Append-only recordings of model calls, keyed as the measurement plan's section 3.5 keys them.

One file per key, one writer per file, no lease and no terminal of its own. A retry
appends an attempt line; nothing is rewritten or deleted. The scorer reads the latest
valid attempt per key and an unavailable one never counts as a valid recording.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = ["ModelAttempt", "RecordingKey", "RecordingStore", "wilson_lower"]

_Z95 = 1.96


def wilson_lower(successes: int, n: int, z: float = _Z95) -> float:
    """Wilson score lower bound of a proportion; 0.0 for no trials."""
    if n <= 0:
        return 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


class RecordingKey(BaseModel):
    """Manifest digest, served model id, prompt, context, decoding settings, repeat index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_digest: str
    served_model_id: str
    prompt_hash: str
    context_hash: str
    decoding: str
    repeat_index: int

    def digest(self) -> str:
        body = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ModelAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt: int
    status: str
    output: str | None


class RecordingStore:
    """Recordings under ``<root>/<manifest digest>/<key digest>.jsonl``."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def path(self, key: RecordingKey) -> Path:
        return self._root / key.manifest_digest / f"{key.digest()}.jsonl"

    def attempts(self, key: RecordingKey) -> list[ModelAttempt]:
        path = self.path(key)
        if not path.is_file():
            return []
        found: list[ModelAttempt] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                found.append(
                    ModelAttempt(
                        attempt=row["attempt"],
                        status=row["status"],
                        output=row["output"],
                    )
                )
            except (ValueError, KeyError):
                continue
        return found

    def latest_valid(self, key: RecordingKey) -> ModelAttempt | None:
        valid = [
            a for a in self.attempts(key) if a.status == "OK" and a.output is not None
        ]
        return valid[-1] if valid else None

    def append(
        self, key: RecordingKey, *, ok: bool, output: str | None
    ) -> ModelAttempt:
        """Append one attempt; earlier attempts are kept byte for byte."""
        previous = self.attempts(key)
        attempt = ModelAttempt(
            attempt=max((a.attempt for a in previous), default=0) + 1,
            status="OK" if ok and output is not None else "UNAVAILABLE",
            output=output,
        )
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "attempt": attempt.attempt,
            "key": key.model_dump(),
            "output": attempt.output,
            "status": attempt.status,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return attempt
