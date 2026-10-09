# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the inputs of a recorded merge sweep from its scenario spec (OMN-20676).

``fixtures/recorded_sweep.json`` holds one trimmed snapshot of the PR watcher's state file and a
list of scenario specs over it (draft flips, run overrides, ``ready_at`` and changed files that the
watcher does not record yet, merges, ledger rows, controller ticks). The recorder ran the merge-sweep
skill's own ``sweep_read.live_reading`` over the files this module builds and stored the answer next
to each spec; the tests build the same files and run the node.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_sweep.json"


def load_recorded() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text())
    return loaded


def _detail(runs: list[list[Any]]) -> list[list[str]]:
    return [[str(r[0]), str(r[4]), str(r[3])] for r in runs]


def _ci_for(sha: str, runs: list[list[Any]], read_at: str) -> dict[str, Any]:
    """A watcher ``ci`` record from rows ``[name, status, conclusion, started_at, id]``."""
    red = [r[0] for r in runs if r[2] in ("failure", "timed_out", "startup_failure")]
    pending = [r[0] for r in runs if r[1] != "completed"]
    cancelled = [r[0] for r in runs if r[2] == "cancelled"]
    verdict = (
        "NONE" if not runs else "RED" if red else "PENDING" if pending else "GREEN"
    )
    return {
        "sha": sha,
        "read_at": read_at,
        "runs": [[r[0], r[1], r[2], r[3]] for r in runs],
        "detail": _detail(runs),
        "red": red,
        "pending": pending,
        "cancelled": cancelled,
        "stale_red": [],
        "last_red_completed_at": "",
        "total": len(runs),
        "verdict": verdict,
    }


def build_state(base: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The watcher state file this scenario reads: the base snapshot with the spec's changes."""
    state = copy.deepcopy(base)
    state["last_tick"] = spec["now_tick"]
    state["last_full_resync"] = spec["now_tick"]
    pick = spec.get("pick")
    if pick is not None:
        state["prs"] = {k: v for k, v in state["prs"].items() if k in set(pick)}
    for key in spec.get("set_draft", []):
        state["prs"][key]["facts"]["draft"] = True
    for key, runs in spec.get("runs", {}).items():
        rec = state["prs"][key]
        rec["ci"] = _ci_for(rec["facts"]["head_sha"], runs, spec["now_tick"])
    for extra in spec.get("extra_prs", []):
        key = f"{extra['repo']}#{extra['number']}"
        sha = extra["head_sha"]
        state["prs"][key] = {
            "facts": {
                "repo": extra["repo"],
                "number": extra["number"],
                "title": extra.get("title", ""),
                "base": extra["base"],
                "head_ref": extra["head_ref"],
                "head_sha": sha,
                "draft": extra.get("draft", False),
                "state": "OPEN",
                "labels": [],
            },
            "ci": _ci_for(sha, extra.get("runs", []), spec["now_tick"]),
        }
        state["repos"].setdefault(extra["repo"], {"default_branch": extra["base"]})
    if "merges" in spec:
        state["merges"] = {
            f"{m['repo']}#{m['number']}": {
                "repo": m["repo"],
                "number": m["number"],
                "merged_at": m["merged_at"],
                "title": "",
            }
            for m in spec["merges"]
        }
    return state


def ticks_text(spec: dict[str, Any]) -> str | None:
    ticks = spec.get("ticks")
    if ticks is None:
        return None
    return "\n".join(json.dumps(t, sort_keys=True) for t in ticks) + "\n"


def write_files(
    base: dict[str, Any], spec: dict[str, Any], root: Path
) -> dict[str, Path]:
    """Write the state, ledger, ticks and floors files of one scenario under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "state": root / "state.json",
        "ledger": root / "ROLLING_WORK_LEDGER.md",
        "floors": root / "landing_floors.json",
    }
    paths["state"].write_text(json.dumps(build_state(base, spec), sort_keys=True))
    paths["ledger"].write_text("\n".join(spec.get("ledger", [])) + "\n")
    paths["floors"].write_text(json.dumps({"per_repo": spec["floors"]}))
    ticks = ticks_text(spec)
    if ticks is not None:
        paths["ticks"] = root / "ticks.jsonl"
        paths["ticks"].write_text(ticks)
    return paths
