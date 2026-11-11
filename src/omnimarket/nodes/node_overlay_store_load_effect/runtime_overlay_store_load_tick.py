# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of a host's overlay store loader, the unit the host installer renders (OMN-23353).

The unit passes ``--omni-home`` and ``--state-dir``. The overlay names the clone of the overlay home (relative to
the registry root), the branch that clone must sit at the head of, and the overlays to load. A clone that is not
at the head of that branch loads nothing and the tick exits non-zero: the store is never filled from a stale or
side branch, which is what the hand-started transient timer this unit replaces did.
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

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.overlay_store import ModelOverlayStoreLoadOverlay
from omnimarket.nodes.node_overlay_store_load_effect.runtime_overlay_store_load import (
    NODE,
    _run,
)

EXIT_REFUSED = 65
EXIT_STALE_CLONE = 66
EXIT_BUS = 69
_GIT_TIMEOUT_S = 30


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


def _record(state_dir: Path, body: dict[str, object]) -> None:
    body["ran_at"] = datetime.now(UTC).isoformat()
    _write_atomically(state_dir / "last-run.json", json.dumps(body, sort_keys=True))


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
            _record(args.state_dir, {"loaded": [], "refused": reason, **state})
            return EXIT_STALE_CLONE
        try:
            results = asyncio.run(_run(overlay, clone, list(overlay.overlay_ids), None))
        except (NodeOverlayError, ValueError) as exc:
            sys.stderr.write(f"{NODE}: {type(exc).__name__}: {exc}\n")
            _record(args.state_dir, {"loaded": [], "refused": str(exc), **state})
            return EXIT_REFUSED
        except (
            Exception
        ) as exc:  # a broker that does not answer is a reading, not a crash
            sys.stderr.write(f"{NODE}: bus: {type(exc).__name__}: {exc}\n")
            _record(args.state_dir, {"loaded": [], "refused": f"bus: {exc}", **state})
            return EXIT_BUS
        loaded = [result.model_dump(mode="json") for result in results]
        for row in loaded:
            sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
        _record(args.state_dir, {"loaded": loaded, "refused": "", **state})
        return 0
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
