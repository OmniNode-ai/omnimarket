# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of a host's overlay store loader, the unit the host installer renders (OMN-23353).

The unit passes ``--omni-home`` and ``--state-dir``. The overlay names the clone of the overlay home (relative to
the registry root), the branch that clone must sit at the head of, and the overlays to load. A clone that is not
at the head of that branch loads nothing and the tick exits non-zero: the store is never filled from a stale or
side branch, which is what the hand-started transient timer this unit replaces did.

The tick publishes when the overlay home's branch has moved past the revision of the last load, and otherwise only to
assert the overlay once ``REASSERT_AFTER_S`` has passed (a reader's staleness is measured against that time). That is what
lets the unit run every few minutes: a merged change reaches the store within one tick of the clone reaching it, and an
unchanged home adds one record an hour per overlay, not one a tick (OMN-26432).

Each overlay is judged on its own: one the loader refuses is named, the ids after it still publish, and the tick exits
non-zero when any id was refused. The loader keeps one assertion per overlay (id, digest, time) in its own state file, apart
from anything a set writes, and an id whose newest assertion is older than the overlay's bound (two hours) raises one alarm
naming the id and its owner, on the topic the contract declares and on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.overlay_store import (
    ModelOverlayAssertion,
    ModelOverlayAssertionCheck,
    ModelOverlayAssertionStaleAlarm,
    ModelOverlayStoreLoadOverlay,
)
from omnimarket.nodes.node_overlay_assertion_stale_compute.handlers import (
    HandlerOverlayAssertionStale,
)
from omnimarket.nodes.node_overlay_store_load_effect.runtime_overlay_store_load import (
    NODE,
    _alarm,
    _run,
)

EXIT_REFUSED = 65
EXIT_STALE_CLONE = 66
EXIT_BUS = 69
_GIT_TIMEOUT_S = 30
# An unchanged overlay is asserted again after this long, so readers' ages keep resetting while the loader is alive.
REASSERT_AFTER_S = 3600
_LAST_LOAD_FILE = "last-load.json"


def _git(clone: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(clone), *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
        env=scrub_git_location_env(os.environ),
    )


def clone_state(clone: Path, branch: str) -> dict[str, str]:
    """What the clone holds against its branch: its head, the branch's remote head and whether it is clean."""
    head = _git(clone, "rev-parse", "HEAD")
    remote = _git(clone, "rev-parse", f"refs/remotes/origin/{branch}")
    dirty = _git(clone, "status", "--porcelain", "--untracked-files=no")
    return {
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "remote_head": remote.stdout.strip() if remote.returncode == 0 else "",
        "dirty": dirty.stdout.strip() if dirty.returncode == 0 else "unreadable",
    }


def stale_reason(state: dict[str, str], branch: str) -> str | None:
    if not state["head"] or not state["remote_head"]:
        return f"the clone has no readable head or origin/{branch}"
    if state["head"] != state["remote_head"]:
        return f"the clone is at {state['head']}, not at origin/{branch} {state['remote_head']}"
    if state["dirty"]:
        return "the clone has uncommitted changes to tracked files"
    return None


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _record(state_dir: Path, body: dict[str, object]) -> None:
    body["ran_at"] = _utc_now().isoformat()
    _write_atomically(state_dir / "last-run.json", json.dumps(body, sort_keys=True))


def _published(state_dir: Path) -> dict[str, dict[str, str]]:
    """What the last successful publish of each overlay named: its source_ref and when this tick asserted it."""
    try:
        held = json.loads((state_dir / _LAST_LOAD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return (
        {str(k): dict(v) for k, v in held.items() if isinstance(v, dict)}
        if isinstance(held, dict)
        else {}
    )


def _unchanged(
    published: dict[str, dict[str, str]], head: str, ids: list[str], now: datetime
) -> bool:
    """True when every overlay was published from this revision and asserted less than ``REASSERT_AFTER_S`` ago."""
    for overlay_id in ids:
        last = published.get(overlay_id)
        if last is None or last.get("source_ref") != f"{overlay_id}@{head}":
            return False
        try:
            asserted = datetime.fromisoformat(str(last.get("asserted_at")))
        except ValueError:
            return False
        if (now - asserted).total_seconds() >= REASSERT_AFTER_S:
            return False
    return True


def _stale_alarms(
    overlay: ModelOverlayStoreLoadOverlay,
    ids: list[str],
    held: dict[str, dict[str, str]],
    now: datetime,
) -> list[ModelOverlayAssertionStaleAlarm]:
    """One alarm per listed id whose newest assertion is older than the overlay's bound, or that has none."""
    assertions: list[ModelOverlayAssertion] = []
    for overlay_id, last in held.items():
        try:
            assertions.append(
                ModelOverlayAssertion(
                    overlay_id=overlay_id,
                    digest=str(last.get("digest") or "0" * 64),
                    asserted_at=datetime.fromisoformat(str(last.get("asserted_at"))),
                )
            )
        except ValueError:
            continue  # an unreadable time is no assertion
    result = HandlerOverlayAssertionStale().handle(
        ModelOverlayAssertionCheck(
            overlay_ids=tuple(ids),
            assertions=tuple(assertions),
            now=now,
            stale_after_s=overlay.assertion_stale_after_s,
            owner=overlay.alarm_owner,
            owner_by_id=overlay.alarm_owner_by_id,
        )
    )
    return list(result.alarms)


def _raise_alarms(
    overlay: ModelOverlayStoreLoadOverlay,
    ids: list[str],
    state_dir: Path,
    now: datetime,
) -> list[ModelOverlayAssertionStaleAlarm]:
    """Name every listed id whose newest assertion is older than the bound, on stderr and on the alarm topic.

    The tick runs this on every way it can end, because the ways it loads nothing (a clone that is not at the head of
    its branch, a run the overlay refuses, a broker that does not answer) are the ways an id goes unasserted: an alarm
    reached only after a successful load would stay silent exactly when nothing was loaded."""
    held = {i: v for i, v in _published(state_dir).items() if i in ids}
    alarms = _stale_alarms(overlay, ids, held, now)
    for alarm in alarms:
        sys.stderr.write(f"{NODE}: ALARM {alarm.model_dump_json()}\n")
    if alarms:
        try:
            asyncio.run(_alarm(overlay, alarms, None))
        except Exception as exc:  # the stderr line above already names each id
            sys.stderr.write(f"{NODE}: alarm bus: {type(exc).__name__}: {exc}\n")
    return alarms


def _alarm_rows(
    alarms: list[ModelOverlayAssertionStaleAlarm],
) -> list[dict[str, Any]]:
    return [a.model_dump(mode="json") for a in alarms]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", dest="registry_root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    faulthandler.dump_traceback_later(float(_deadline_s()), exit=True)
    # A tick run in-process must not leave the process-ending timer armed.
    try:
        try:
            overlay = load_structured_overlay(
                NODE, ModelOverlayStoreLoadOverlay, os.environ
            )
        except NodeOverlayError as exc:
            sys.stderr.write(f"{NODE}: {exc}\n")
            return EXIT_REFUSED
        if not (overlay.clone_relpath and overlay.clone_branch and overlay.overlay_ids):
            sys.stderr.write(
                f"{NODE}: the overlay names no clone_relpath, clone_branch and overlay_ids for the tick\n"
            )
            return EXIT_REFUSED
        clone = args.registry_root / overlay.clone_relpath
        state = clone_state(clone, overlay.clone_branch)
        reason = stale_reason(state, overlay.clone_branch)
        if reason is not None:
            sys.stderr.write(f"{NODE}: nothing loaded: {reason}\n")
            stale = _raise_alarms(
                overlay, list(overlay.overlay_ids), args.state_dir, _utc_now()
            )
            _record(
                args.state_dir,
                {
                    "loaded": [],
                    "refused": reason,
                    "alarms": _alarm_rows(stale),
                    **state,
                },
            )
            return EXIT_STALE_CLONE
        now = _utc_now()
        ids = list(overlay.overlay_ids)
        held = {i: v for i, v in _published(args.state_dir).items() if i in ids}
        due = [i for i in ids if not _unchanged(held, state["head"], [i], now)]
        unchanged = [f"{i}@{state['head']}" for i in ids if i not in due]
        loaded: list[dict[str, Any]] = []
        refused: dict[str, str] = {}
        if due:
            try:
                batch = asyncio.run(_run(overlay, clone, due, None))
            except (NodeOverlayError, ValueError) as exc:
                sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
                failed = _raise_alarms(overlay, ids, args.state_dir, now)
                _record(
                    args.state_dir,
                    {
                        "loaded": [],
                        "refused": str(exc),
                        "alarms": _alarm_rows(failed),
                        **state,
                    },
                )
                return EXIT_REFUSED
            except (
                Exception
            ) as exc:  # a broker that does not answer is a reading, not a crash
                sys.stderr.write(f"{NODE}: bus: {type(exc).__name__}: {exc}\n")
                failed = _raise_alarms(overlay, ids, args.state_dir, now)
                _record(
                    args.state_dir,
                    {
                        "loaded": [],
                        "refused": f"bus: {exc}",
                        "alarms": _alarm_rows(failed),
                        **state,
                    },
                )
                return EXIT_BUS
            loaded = [result.model_dump(mode="json") for result in batch.results]
            refused = {r.overlay_id: r.reason for r in batch.refused}
            for row in loaded:
                sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
            for overlay_id, reason in refused.items():
                sys.stderr.write(f"{NODE}: refused {overlay_id}: {reason}\n")
            # A refused id keeps the assertion it had, so its age keeps growing until it loads again.
            held.update(
                {
                    row["overlay_id"]: {
                        "source_ref": row["source_ref"],
                        "digest": row["digest"],
                        "asserted_at": now.isoformat(),
                    }
                    for row in loaded
                }
            )
            _write_atomically(
                args.state_dir / _LAST_LOAD_FILE, json.dumps(held, sort_keys=True)
            )
        alarms = _raise_alarms(overlay, ids, args.state_dir, now)
        _record(
            args.state_dir,
            {
                "loaded": loaded,
                "unchanged": unchanged,
                "refused": "; ".join(f"{i}: {r}" for i, r in refused.items()),
                "refused_ids": refused,
                "alarms": _alarm_rows(alarms),
                **state,
            },
        )
        return EXIT_REFUSED if refused or alarms else 0
    finally:
        faulthandler.cancel_dump_traceback_later()


def _deadline_s() -> int:
    from importlib import resources

    import yaml

    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    return int(contract["descriptor"]["timeout_ms"]) // 1000


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
