# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One read of each lab host answers every placement for a short window, made outside the placement lock (OMN-17427).

On 2026-10-21 the remote-lane runner and the landing worker each took the placement lock and then read every host live
(``landing_placement.read_pool``: one ssh probe and one 50-ping link read per host, and the launching host's own probe):
12.5 s and 13.2 s on the launching host with the lock free, up to the 60 s local probe bound under load. With 154 lanes in
status placing the lock was held at every sample and 14 placements completed in 300 s, so the lab ran 6 lanes beside about
18 free Codex slots. A placement now reads the pool before it takes the lock, through ``shared``: a probe captured by one
placement answers every placement for ``POOL_READ_TTL_S``, captured under that probe's own lock (placements that need the
same host wait for one read of it rather than each making one) and written atomically. Under the placement lock only the
slot markers are counted again (``recount``), the holdback and the pick are made and the marker is written, so two
placements still cannot take one host past its cap. The window stays under the link history's ``LINK_READ_MIN_GAP_S``, so
one shared ping is never recorded as two link reads.
"""

import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_pool_read_share_effect.models.model_pool_read_share import (
    ModelPoolReadShareRequest,
    ModelPoolReadShareResult,
)

POOL_READ_DIR = (
    ".pool-read"  # a dot name: placed_counts never reads it as a slot marker
)
POOL_READ_TTL_S = 30.0
PROBE_TIMEOUT_S = 20.0


def probe_key(argv: Sequence[str]) -> str:
    """The probe an argv runs, whoever asks: the ``# lane=`` line ``ssh_argv`` writes for the reader is not part of it."""
    parts = [str(a) for a in argv]
    if parts and parts[-1].startswith("# lane="):
        parts[-1] = parts[-1].partition("\n")[2]
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()[:32]


def capture_live(
    argv: list[str], timeout: float = PROBE_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """The probe run live when the caller passes no capture: a timeout or a launch failure is exit 255."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 255, "", f"timed out after {timeout:.0f}s"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 255, "", str(exc))


class HandlerPoolReadShare:
    """``capture`` (``landing_placement._capture``'s shape: argv and a timeout, a CompletedProcess back) runs a probe live."""

    def __init__(
        self,
        capture: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.capture, self.clock = capture or capture_live, clock or time.time

    def _fresh(self, path: Path, ttl_s: float) -> ModelPoolReadShareResult | None:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if not 0 <= self.clock() - float(body["at"]) < ttl_s:
                return None
            return ModelPoolReadShareResult(
                returncode=int(body["returncode"]),
                stdout=str(body["stdout"]),
                stderr=str(body["stderr"]),
                shared=True,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def handle(self, request: ModelPoolReadShareRequest) -> ModelPoolReadShareResult:
        directory, key = Path(request.directory), probe_key(request.argv)
        path = directory / f"{key}.json"
        hit = self._fresh(path, request.ttl_s)
        if hit is not None:
            return hit
        try:
            directory.mkdir(parents=True, exist_ok=True)
            lock = (directory / f".{key}.lock").open("a+", encoding="utf-8")
        except OSError:
            return self._live(request, None)
        with lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._fresh(path, request.ttl_s) or self._live(request, path)

    def _live(
        self, request: ModelPoolReadShareRequest, path: Path | None
    ) -> ModelPoolReadShareResult:
        done = self.capture(list(request.argv), timeout=request.timeout_s)
        result = ModelPoolReadShareResult(
            returncode=done.returncode,
            stdout=done.stdout or "",
            stderr=done.stderr or "",
            shared=False,
        )
        if path is not None:
            body = {
                "at": self.clock(),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            try:
                tmp = path.with_name(
                    f".{path.name}.{os.getpid()}.{threading.get_ident()}"
                )
                tmp.write_text(json.dumps(body), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        return result


def shared(
    placement_dir: Path,
    capture: Callable[..., Any],
    *,
    ttl_s: float = POOL_READ_TTL_S,
    clock: Callable[[], float] | None = None,
) -> Callable[..., "subprocess.CompletedProcess[str]"]:
    """A ``read_pool`` capture whose answers every placement on ``placement_dir`` shares for ``ttl_s`` seconds."""
    handler = HandlerPoolReadShare(capture, clock)
    directory = str(Path(placement_dir) / POOL_READ_DIR)

    def run(
        argv: Sequence[str], timeout: float = PROBE_TIMEOUT_S
    ) -> "subprocess.CompletedProcess[str]":
        r = handler.handle(
            ModelPoolReadShareRequest(
                directory=directory,
                argv=tuple(str(a) for a in argv),
                timeout_s=float(timeout),
                ttl_s=ttl_s,
            )
        )
        return subprocess.CompletedProcess(list(argv), r.returncode, r.stdout, r.stderr)

    return run


def recount(
    readings: Sequence[Any],
    placed: Mapping[str, int],
    fresh: Mapping[str, int] | None = None,
) -> list[Any]:
    """The readings with the slot markers as they stand now: ``placed`` (``placed_counts``) and, when given, ``fresh``
    (``fresh_counts``), read under the placement lock, replace the counts the pool read was made with."""
    out = []
    for r in readings:
        count = int(placed.get(r.host.name, 0))
        out.append(
            replace(
                r,
                placed=count,
                **(
                    {"fresh": min(count, int(fresh.get(r.host.name, 0)))}
                    if fresh is not None
                    else {}
                ),
            )
        )
    return out
