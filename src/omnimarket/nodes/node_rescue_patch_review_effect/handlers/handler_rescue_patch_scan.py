# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review unreviewed saved patches with a scratch index and write a heartbeat."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..models import ModelRescuePatchScanRequest, ModelRescuePatchScanResult
from .adapter_compute_reviewer import review_patch_with_compute_node
from .protocol_rescue_patch_reviewer import ProtocolRescuePatchReviewer

LANE = "rescue-patch-review"
PATCH_NAMES = ("commits.patch", "uncommitted.patch")


def _git(
    clone: Path, argv: list[str], *, index: Path | None = None, stdin: str = ""
) -> int:
    env = dict(os.environ)
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    done = subprocess.run(
        ["git", *argv],
        cwd=clone,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


def _apply_facts(clone: Path, base: str, patch_text: str) -> tuple[bool, bool] | None:
    """(applies, already_on_base) against ``base``, or None if the base is unreadable."""
    if _git(clone, ["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"]) != 0:
        return None
    with tempfile.TemporaryDirectory(prefix="rescue-patch-review-") as scratch:
        index = Path(scratch) / "index"
        if _git(clone, ["read-tree", base], index=index) != 0:
            return None
        applies = (
            _git(
                clone,
                ["apply", "--cached", "--check", "-"],
                index=index,
                stdin=patch_text,
            )
            == 0
        )
        present = (
            _git(
                clone,
                ["apply", "--cached", "--reverse", "--check", "-"],
                index=index,
                stdin=patch_text,
            )
            == 0
        )
    return applies, present


def _stamp(value: str) -> str:
    return value or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _directories(root: Path) -> list[Path]:
    found = []
    for manifest in sorted(root.rglob("MANIFEST.json")):
        directory = manifest.parent
        if "discard" in directory.relative_to(root).parts:
            continue
        found.append(directory)
    return found


class HandlerRescuePatchScan:
    """One tick: review the unreviewed saved patches and leave the heartbeat."""

    def __init__(self, *, reviewer: ProtocolRescuePatchReviewer | None = None) -> None:
        self._reviewer = reviewer or review_patch_with_compute_node

    def handle(
        self, request: ModelRescuePatchScanRequest
    ) -> ModelRescuePatchScanResult:
        now = _stamp(request.now)
        reviewed = needs_read = discarded = already = unjudgeable = 0
        pending: list[str] = []
        detail: list[str] = []
        root_present = request.rescue_root.is_dir()
        for directory in _directories(request.rescue_root) if root_present else []:
            if (directory / "VERDICT.json").exists():
                already += 1
                continue
            try:
                manifest = json.loads((directory / "MANIFEST.json").read_text())
                # An older manifest names no repo: the directory is <ticket>__<repo>.
                repo = str(
                    manifest.get("repo") or directory.name.rsplit("__", 1)[-1]
                ).rsplit("/", 1)[-1]
                base = str(manifest["base"])
            except (OSError, ValueError, KeyError) as error:
                unjudgeable += 1
                detail.append(
                    f"{directory}: manifest unreadable ({type(error).__name__})"
                )
                continue
            patch_text = "\n".join(
                (directory / name).read_text(errors="replace")
                for name in PATCH_NAMES
                if (directory / name).is_file()
            )
            clone = request.registry_root / repo
            if not (clone / ".git").exists():
                unjudgeable += 1
                detail.append(
                    f"{directory}: {repo} is not cloned under {request.registry_root}"
                )
                continue
            facts = (
                _apply_facts(clone, base, patch_text)
                if patch_text.strip()
                else (False, False)
            )
            if facts is None:
                unjudgeable += 1
                detail.append(f"{directory}: base {base} is not readable in {repo}")
                continue
            applies, present = facts
            result = self._reviewer(
                patch_text=patch_text, applies_to_base=applies, already_on_base=present
            )
            (directory / "VERDICT.json").write_text(
                json.dumps(
                    {
                        "dir": str(directory),
                        "verdict": result.verdict,
                        "engine": "deterministic",
                        "why": result.reason,
                        "deterministic": result.reason,
                        "applies_to_base": {base: "clean" if applies else "fails"},
                        "decided_utc": now,
                        "lane": LANE,
                    },
                    indent=1,
                )
                + "\n"
            )
            reviewed += 1
            if result.is_discard:
                discarded += 1
            else:
                needs_read += 1
                pending.append(str(directory))
        request.state_dir.mkdir(parents=True, exist_ok=True)
        (request.state_dir / "last-run.json").write_text(
            json.dumps(
                {
                    "ran_utc": now,
                    "rescue_root": str(request.rescue_root),
                    "root_present": root_present,
                    "reviewed": reviewed,
                    "needs_read": needs_read,
                    "discarded": discarded,
                    "already_reviewed": already,
                    "unjudgeable": unjudgeable,
                    "pending": pending,
                    "detail": detail,
                },
                indent=1,
            )
            + "\n"
        )
        return ModelRescuePatchScanResult(
            reviewed=reviewed,
            needs_read=needs_read,
            discarded=discarded,
            already_reviewed=already,
            unjudgeable=unjudgeable,
            pending=tuple(pending),
            detail=tuple(detail),
        )
