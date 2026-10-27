# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host closeout retention. Standalone stdlib payload for a remote Python interpreter.

The launcher supplies its final receipt and current ledger after closing ownership.
Unknown ownership, missing evidence and git metadata preserve both run directories.
Only this module's manifest-bearing archives participate in report expiration.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z")
_FINAL = frozenset(
    {
        "done",
        "failed",
        "rejected-no-delegation",
        "interrupted",
        "timeout",
        "remote-failed",
        "lane-error",
        "lane-timeout",
        "no-result",
        "crashed",
        "host-limited",
    }
)
_ARTIFACT_KEYS = frozenset({"report", "report_path", "result_file", "result_path"})


def _owners(ledger: str) -> tuple[set[str], set[str]]:
    active: dict[str, str] = {}
    known: set[str] = set()
    for line in ledger.splitlines():
        parts = [cell.strip() for cell in line.split(" | ")]
        if len(parts) < 3:
            continue
        cells = dict(cell.split("=", 1) for cell in parts[2:] if "=" in cell)
        lane = cells.get("lane", "")
        if parts[1] == "CLAIM" and lane:
            active[lane] = parts[0]
            known.add(lane)
        elif (
            parts[1] == "TERMINAL"
            and cells.get("closes-CLAIM") == active.get(lane)
            and lane in active
        ):
            del active[lane]
    return set(active), known


def _inside(root: Path, path: Path) -> Path:
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact outside lab root") from exc
    if ".." in relative.parts:
        raise ValueError("parent traversal")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("symlink in retention path")
    return relative


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _references(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ARTIFACT_KEYS and isinstance(item, str) and item.startswith("/"):
                paths.append(item)
            elif isinstance(item, (dict, list)):
                paths.extend(_references(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_references(item))
    return paths


def retain_run(
    root: Path,
    receipt: dict[str, Any],
    ledger: str,
    *,
    keep_days: int = 7,
    delete: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Archive one final run, optionally delete scratch trees, expire managed reports.

    A successful archive is required even when the working directories are absent.
    Git-bearing trees are left for the runner's existing guarded git closeout.
    ``now`` exists for deterministic expiration tests; timestamps are UTC epochs.
    """
    root = root.absolute()
    if root.name != "lab-run" or root.is_symlink() or root.resolve() != root:
        raise ValueError("lab root must be a real lab-run directory")
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ValueError("keep_days must be a positive integer")
    lane, run = receipt.get("lane"), receipt.get("run_id")
    if (
        not isinstance(lane, str)
        or not isinstance(run, str)
        or not _NAME.fullmatch(lane)
        or not _NAME.fullmatch(run)
    ):
        raise ValueError("unsafe lane or run name")
    if receipt.get("final") is not True or receipt.get("status") not in _FINAL:
        raise ValueError("receipt is not final")
    ended_at = datetime.fromisoformat(
        str(receipt.get("ended_at", "")).replace("Z", "+00:00")
    )
    if ended_at.tzinfo is None:
        raise ValueError("ended_at must name a timezone")
    active, known = _owners(ledger)
    if lane not in known or lane in active:
        raise ValueError("lane ownership not closed in ledger")
    matching_run = False
    for line in ledger.splitlines():
        parts = [cell.strip() for cell in line.split(" | ")]
        if len(parts) < 3 or parts[1] != "CLAIM":
            continue
        cells = dict(cell.split("=", 1) for cell in parts[2:] if "=" in cell)
        if cells.get("lane") == lane and cells.get("run") == run:
            matching_run = True
    if not matching_run:
        raise ValueError("receipt run does not match a ledger CLAIM")
    trees = [root / "runs" / run, root / "runs" / (run + ".ctx")]
    archive = root / "reports" / lane / run
    for path in [*trees, archive]:
        _inside(root, path)
    references = _references(receipt)
    references.extend(
        re.findall(
            r"(?:report|result_file|result_path)=(/[^\s|`]+)",
            str(receipt.get("result", "")),
        )
    )
    for line in ledger.splitlines():
        if f" | lane={lane} |" in line:
            references.extend(
                re.findall(r"(?:report|result_file|result_path)=(/[^\s|`]+)", line)
            )
    paths = {Path(p) for p in references}
    ctx = trees[1]
    for name in ("result.txt", "result.json", "report.md"):
        path = ctx / name
        if path.exists():
            paths.add(path)
    sources: dict[str, Path] = {}
    for path in sorted(paths):
        relative_path = _inside(root, path)
        if not path.is_file():
            raise ValueError(f"missing artifact: {relative_path}")
        sources[str(relative_path)] = path
    archive.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".retention-", dir=archive.parent))
    try:
        hashes: dict[str, str] = {}
        for relative, source in sources.items():
            dest = staging / "files" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            hashes[relative] = _digest(dest)
            if hashes[relative] != _digest(source):
                raise OSError("artifact changed during copy")
        (staging / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (staging / "result.txt").write_text(str(receipt.get("result", "")))
        manifest = {
            "retention_version": 1,
            "lane": lane,
            "run_id": run,
            "ended_at": receipt["ended_at"],
            "files": hashes,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if archive.exists():
            # A retry must never overwrite the only copy of an earlier artifact.
            for relative, digest in hashes.items():
                saved = archive / "files" / relative
                _inside(root, saved)
                if not saved.is_file() or _digest(saved) != digest:
                    raise ValueError("archive collision or changed artifact")
        else:
            staging.rename(archive)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    removed: list[str] = []
    # All git metadata, including a nested worktree, requires the existing git guard.
    git_present = any(tree.exists() and any(tree.rglob(".git")) for tree in trees)
    if delete and not git_present:
        for tree in trees:
            _inside(root, tree)
            if tree.exists():
                shutil.rmtree(tree)
                removed.append(tree.name)
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    pruned: list[str] = []
    for saved in sorted((root / "reports").glob("*/*/manifest.json")):
        _inside(root, saved)
        if saved.parent == archive:
            continue
        try:
            data = json.loads(saved.read_text())
            saved_receipt = saved.parent / "receipt.json"
            _inside(root, saved_receipt)
            archived_receipt = json.loads(saved_receipt.read_text())
            stamp = datetime.fromisoformat(
                data["ended_at"].replace("Z", "+00:00")
            ).timestamp()
            managed = (
                data.get("retention_version") == 1
                and data.get("lane") == saved.parent.parent.name
                and data.get("run_id") == saved.parent.name
                and archived_receipt.get("final") is True
                and archived_receipt.get("status") in _FINAL
                and archived_receipt.get("lane") == data.get("lane")
                and archived_receipt.get("run_id") == data.get("run_id")
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if managed and stamp < cutoff and data["lane"] not in active:
            shutil.rmtree(saved.parent)
            pruned.append(str(saved.parent.relative_to(root / "reports")))
    return {"archive": str(archive), "removed": removed, "pruned": pruned}


def retention_script(
    root: str,
    receipt: dict[str, Any],
    ledger: str,
    *,
    keep_days: int = 7,
    delete: bool = False,
) -> str:
    """Ship this exact tested stdlib implementation; no host package upgrade needed."""
    import base64

    ledger = "\n".join(
        line
        for line in ledger.splitlines()
        if " | CLAIM |" in line or " | TERMINAL |" in line
    )
    payload = base64.b64encode(
        json.dumps(
            {
                "root": root,
                "receipt": receipt,
                "ledger": ledger,
                "keep_days": keep_days,
                "delete": delete,
            }
        ).encode()
    ).decode()
    source = Path(__file__).read_text().split("\ndef retention_script(", 1)[0]
    return (
        "python3 - <<'LANE_RETENTION_PY'\n"
        + source
        + f"""\nimport base64
request = json.loads(base64.b64decode({payload!r}))
request['root'] = Path(request['root'])
try:
    print('LANE-RETENTION ' + json.dumps(retain_run(**request), sort_keys=True))
except (OSError, ValueError) as exc:
    print('LANE-RETENTION-KEPT ' + str(exc))
    raise SystemExit(1)
LANE_RETENTION_PY
"""
    )


def main() -> int:
    """Build the host payload in the dispatch interpreter, without deleting locally.

    The stdlib launcher invokes this module through its dispatch venv instead of
    importing Market into the launcher's unrelated system Python. Only the emitted
    script is sent to the host, where the existing ownership/archive guards apply.
    """
    import argparse
    import sys

    from .models.model_remote_lane_retention import ModelRemoteLaneRetentionRequest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--script", required=True, type=Path, help="JSON retention request"
    )
    args = parser.parse_args()
    try:
        request = ModelRemoteLaneRetentionRequest.model_validate_json(
            args.script.read_text(), strict=True
        )
        script = retention_script(**request.model_dump())
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"LANE-RETENTION-KEPT {exc}\n")
        return 1
    sys.stdout.write(script)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
