# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded detail capture: keep the head and the tail and a dropped-line count past a size bound.

An entrypoint writes every verb's stdout to one detail file. A verb that prints a whole ledger
(100,000 to 190,000 lines, about 113 MB) used to land in that file whole. ``bound_capture`` reads
the stream once, line by line, holding at most ``head_lines`` plus ``tail_lines`` lines in memory.
A capture within ``max_bytes`` is written unchanged; one past it ends as its first ``head_lines``,
its last ``tail_lines`` and one last line naming the dropped count. The three numbers are the
contract's ``detail_capture`` section, never literals here.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from pathlib import Path

import yaml

from omnimarket.nodes.node_work_ledger_reader_effect.models.model_detail_capture import (
    ModelDetailCaptureResult,
    ModelDetailCaptureSettings,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_detail_capture_settings(
    contract_path: Path = _CONTRACT_PATH,
) -> ModelDetailCaptureSettings:
    """The contract's ``detail_capture`` section; a missing or invalid section raises."""
    with contract_path.open(encoding="utf-8") as handle:
        section = (yaml.safe_load(handle) or {}).get("detail_capture")
    if not isinstance(section, dict):
        raise ValueError("contract declares no detail_capture section")
    return ModelDetailCaptureSettings.model_validate(section)


def _marker(total: int, dropped: int, settings: ModelDetailCaptureSettings) -> bytes:
    return (
        f"[detail capture bounded: {dropped} lines dropped of {total}; kept the first "
        f"{settings.head_lines} and the last {settings.tail_lines}]\n"
    ).encode()


def bound_capture(
    stream: Iterable[bytes],
    dest: Path,
    settings: ModelDetailCaptureSettings,
) -> ModelDetailCaptureResult:
    """Write ``stream`` (byte lines) to ``dest``, bounded by ``settings``."""
    head: list[bytes] = []
    tail: deque[bytes] = deque(maxlen=settings.tail_lines)
    spill = dest.with_name(dest.name + ".part")
    total = 0
    size = 0
    bounded = False
    try:
        with spill.open("wb") as out:
            for line in stream:
                total += 1
                if len(head) < settings.head_lines:
                    head.append(line)
                else:
                    tail.append(line)
                if bounded:
                    continue
                out.write(line)
                size += len(line)
                if size > settings.max_bytes:
                    bounded = True
                    out.seek(0)
                    out.truncate()  # rewritten from head and tail once the stream ends
            if not bounded:
                out.flush()
        if not bounded:
            spill.replace(dest)
            return ModelDetailCaptureResult(
                total_lines=total, dropped_lines=0, truncated=False
            )
        dropped = max(0, total - len(head) - len(tail))
        with spill.open("wb") as out:
            out.writelines(head)
            out.writelines(tail)
            out.write(_marker(total, dropped, settings))
        spill.replace(dest)
        return ModelDetailCaptureResult(
            total_lines=total, dropped_lines=dropped, truncated=True
        )
    finally:
        spill.unlink(missing_ok=True)


__all__ = ["bound_capture", "load_detail_capture_settings"]
