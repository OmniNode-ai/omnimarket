# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cache judging-path observations and ledger row indexes between controller invocations."""

from __future__ import annotations

import hashlib
import json
import marshal
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..models.model_landing_tick_speed import (
    ModelLandingTickSpeedRequest,
    ModelLandingTickSpeedResult,
)

JUDGE_CACHE_FILE = "judge-touch-cache.json"
JUDGE_KEPT = ("clean", "touched")
LEDGER_INDEX_DIR = "ledger-index"
LEDGER_INDEX_VERSION = 1
LEDGER_INDEX_KEEP_S = 9 * 86400


def _add(timing: dict[str, Any] | None, key: str, value: float) -> None:
    if timing is not None:
        timing[key] = round(float(timing.get(key, 0) or 0) + value, 2)


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Any) -> None:
    try:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Cache persistence is optional; the next tick can read the diff again.
        pass


def judge_touches(
    lf: Any,
    snap: Any,
    leases: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    prior: Mapping[str, Any] | None,
    now: str,
    read_touch: Callable[..., dict[str, Any]],
    state_dir: Path | None = None,
    timing: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Cache clean and touched head pairs; retry unreadable observations without changing landing decisions."""
    start = time.monotonic()
    path = Path(state_dir) / JUDGE_CACHE_FILE if state_dir is not None else None
    cache = _read_json(path)
    kept: dict[str, Any] = {}
    reads = served = 0
    touches = {str(k): dict(v) for k, v in (prior or {}).items() if isinstance(v, dict)}
    events: list[dict[str, Any]] = []
    for short, rec in sorted(touches.items()):
        lv = snap.live.get(short)
        if str(getattr(lv, "state", "") or "").upper() in (
            "MERGED",
            "CLOSED",
        ) and rec.get("friction"):
            touches.pop(short)
    by_id = {int(le["lease_id"]): le for le in leases}
    for result in results:
        le = by_id.get(int(result.get("lease_id") or 0))
        if le is None or lf.is_cause_key(str(le.get("pr") or "")):
            continue
        dispatch, head = (
            str(le.get("dispatch_head") or ""),
            str(result.get("head_sha") or ""),
        )
        if not (_sha40(dispatch) and _sha40(head)) or head == dispatch:
            continue
        short = lf.short_key(str(le["pr"]))
        old = touches.get(short) or {}
        if old.get("lease_id") == int(le["lease_id"]) and old.get("head") == head:
            continue
        repo = lf.repo_of(short)
        lv = snap.live.get(short)
        base = (
            str(getattr(lv, "base", "") or "")
            or snap.default_branches.get(repo)
            or lf.LANDING_BRANCH
        )
        key = f"{repo}:{base}:{dispatch}..{head}"
        touch = cache.get(key)
        if isinstance(touch, dict) and touch.get("status") in JUDGE_KEPT:
            served += 1
        else:
            reads += 1
            try:
                touch = read_touch(repo, dispatch, head, base, short.rpartition("#")[2])
            except Exception as exc:
                touch = {
                    "status": "unreadable",
                    "paths": [{"path": "(unreadable diff)", "kind": "unreadable"}],
                    "detail": f"{type(exc).__name__}: {exc}"[:300],
                }
        if touch["status"] in JUDGE_KEPT:
            kept[key] = {
                "status": touch["status"],
                "paths": list(touch.get("paths") or []),
                "detail": "",
            }
        if touch["status"] == "clean":
            continue
        touches[short] = {
            "head": head,
            "dispatch_head": dispatch,
            "lease_id": int(le["lease_id"]),
            "paths": touch["paths"],
            "status": touch["status"],
            "detail": touch["detail"],
            "at": now,
            "friction": False,
            "disarm_event": lf.retired_judge_disarm_of(old),
        }
        events.append(
            {
                "kind": "judge_touch",
                "subject": short,
                "outcome": touch["status"],
                "head_sha": head,
                "lease_id": int(le["lease_id"]),
                "paths": touch["paths"],
                "detail": lf.judge_paths_text(touch["paths"]),
                "read_error": touch["detail"],
            }
        )
    if path is not None and kept != cache:
        _write_json(path, kept)
    _add(timing, "judge_touches_s", time.monotonic() - start)
    _add(timing, "judge_touch_reads", reads)
    _add(timing, "judge_touch_cached", served)
    return (touches, events)


def _sha40(text: str) -> bool:
    return len(text) == 40 and all(c in "0123456789abcdef" for c in text)


def _index_path(directory: Path, key: Any) -> Path:
    return (
        directory
        / f"{hashlib.sha1(repr(key).encode('utf-8')).hexdigest()[:20]}.marshal"
    )


def load_ledger_index(
    rows_cache: dict[Any, Any], directory: Path, now: float | None = None
) -> dict[Any, tuple[Any, ...]]:
    """Fill ``rows_cache`` (``drain_map._ROWS_CACHE``) from the index files in ``directory``; returns each loaded key's
    stat. A file that does not load, or whose ledger file is gone or was last written past ``LEDGER_INDEX_KEEP_S``, is
    removed: drain_map then parses that ledger file whole, as without an index."""
    loaded: dict[Any, tuple[Any, ...]] = {}
    if not directory.is_dir():
        return loaded
    clock = time.time() if now is None else now
    held = {_index_path(directory, key).name for key in rows_cache}
    for path in sorted(directory.glob("*.marshal")):
        if path.name in held:
            continue
        try:
            body = marshal.loads(path.read_bytes())
            key, value = (body["key"], body["value"])
            fresh = (
                body["version"] == LEDGER_INDEX_VERSION
                and clock - Path(key[0]).stat().st_mtime < LEDGER_INDEX_KEEP_S
            )
            stat = tuple(value["stat"])
            if (
                fresh
                and isinstance(value["entries"], list)
                and isinstance(value["digest"], bytes)
                and (len(value["tail"]) == 3)
            ):
                rows_cache[key] = value
                loaded[key] = stat
                continue
        except (
            OSError,
            ValueError,
            EOFError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ):
            # Invalid indexes are discarded below so the ledger is parsed afresh.
            pass
        path.unlink(missing_ok=True)
    return loaded


def save_ledger_index(
    rows_cache: Mapping[Any, Any],
    directory: Path,
    loaded: Mapping[Any, tuple[Any, ...]],
) -> int:
    """Write the index of each ledger file whose parse changed since ``loaded``; returns how many were written."""
    written = 0
    for key, value in rows_cache.items():
        if loaded.get(key) == tuple(value.get("stat") or ()):
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = _index_path(directory, key)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(
                marshal.dumps(
                    {"version": LEDGER_INDEX_VERSION, "key": key, "value": dict(value)}
                )
            )
            os.replace(tmp, path)
            written += 1
        except (OSError, ValueError):
            continue
    return written


def ledger_indexed(
    rows_cache: dict[Any, Any] | None,
    state_dir: Path | None,
    read: Callable[[], Any],
    timing: dict[str, Any] | None = None,
) -> Any:
    """``read()`` (the controller's ledger read) with drain_map's row index loaded from ``state_dir`` before it and the
    changed files saved after it, timed in ``timing`` as ``ledger_index_load_s`` and ``ledger_index_save_s``. With no
    ``rows_cache`` (a drain_map without the in-process index) it is ``read()``."""
    if rows_cache is None:
        return read()
    directory = Path(state_dir) / LEDGER_INDEX_DIR if state_dir is not None else None
    start = time.monotonic()
    loaded = load_ledger_index(rows_cache, directory) if directory is not None else {}
    _add(timing, "ledger_index_load_s", time.monotonic() - start)
    _add(timing, "ledger_index_files", len(loaded))
    out = read()
    start = time.monotonic()
    if directory is not None:
        _add(
            timing,
            "ledger_index_written",
            save_ledger_index(rows_cache, directory, loaded),
        )
    _add(timing, "ledger_index_save_s", time.monotonic() - start)
    return out


class HandlerLandingTickSpeed:
    def handle(
        self, request: ModelLandingTickSpeedRequest
    ) -> ModelLandingTickSpeedResult:
        if request.operation == "judge_touches":
            if request.read_touch is None:
                raise ValueError("judge_touches needs read_touch")
            return ModelLandingTickSpeedResult(
                value=judge_touches(
                    request.lf,
                    request.snap,
                    request.leases,
                    request.results,
                    request.prior,
                    request.now,
                    request.read_touch,
                    request.state_dir,
                    request.timing,
                )
            )
        if request.operation == "ledger_indexed":
            if request.read is None:
                raise ValueError("ledger_indexed needs read")
            return ModelLandingTickSpeedResult(
                value=ledger_indexed(
                    request.rows_cache, request.state_dir, request.read, request.timing
                )
            )
        raise ValueError(
            f"unsupported landing tick speed operation: {request.operation}"
        )
