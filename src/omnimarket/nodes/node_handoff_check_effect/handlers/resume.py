# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resume from the newest committed handoff: re-probe its stale lines and list what changed."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    load_rows,
    open_claims,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.readers import (
    read_prs,
    run_probe,
    run_reader,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelFact,
    ModelHandoffCheckRequest,
    ModelHandoffCheckResult,
    ModelHandoffPolicy,
    ModelLedgerRow,
    fmt_ts,
    parse_ts,
)

__all__ = ["newest_handoff", "reprobe", "resume"]


def newest_handoff(clone: Path, ref: str, directory: str) -> tuple[str, str] | None:
    """The newest committed handoff at ``ref``; a file only in the worktree is ignored."""
    rc, out = run_reader(
        ["git", "-C", str(clone), "ls-tree", "--name-only", ref, f"{directory}/"]
    )
    names = sorted(n for n in out.splitlines() if rc == 0 and n.endswith("-handoff.md"))
    if not names:
        return None
    rc, body = run_reader(["git", "-C", str(clone), "show", f"{ref}:{names[-1]}"])
    return (names[-1], body) if rc == 0 else None


def reprobe(
    f: ModelFact,
    policy: ModelHandoffPolicy,
    now: dt.datetime,
    recorded: Mapping[str, object],
    rows: Sequence[ModelLedgerRow],
    pr_reads: Path | None,
) -> str | None:
    kind, _sep, name = f.probe.partition(":")
    if f.probe in recorded:
        return str(recorded[f.probe])
    if kind == "pr":
        state, _src, _at = read_prs([name], pr_reads, policy, now)
        rec = state.get(name)
        return f"state={rec.get('state', '?')} ci={rec.get('ci', '?')}" if rec else None
    if kind == "svc":
        spec = next((p for p in policy.probes if p.name == name), None)
        return run_probe(spec, now).verdict if spec else None
    if kind == "lane":
        return "open" if name in open_claims(rows) else "closed"
    return None


def resume(
    request: ModelHandoffCheckRequest, policy: ModelHandoffPolicy, ledger_path: str
) -> ModelHandoffCheckResult:
    if not request.kb_clone:
        raise ValueError("resume: kb_clone is required (the shared document clone)")
    now = (
        parse_ts(request.now)
        if request.now
        else dt.datetime.now(dt.UTC).replace(microsecond=0)
    )
    directory = request.directory or policy.output.directory
    found = newest_handoff(Path(request.kb_clone), request.ref, directory)
    if not found:
        return ModelHandoffCheckResult(
            command=EnumHandoffCommand.RESUME,
            exit_code=3,
            output=[
                f"RESUME: no handoff under {directory} at {request.ref} in {request.kb_clone}"
            ],
        )
    name, body = found
    recorded: dict[str, object] = (
        json.loads(Path(request.probes).read_text(encoding="utf-8"))
        if request.probes
        else {}
    )
    ledger = Path(ledger_path)
    rows = load_rows(
        ledger,
        Path(request.archive_dir) if request.archive_dir else ledger.parent / "archive",
    )
    made = re.search(r"assembled (\S+Z)", body)
    since = parse_ts(made.group(1)) if made else None
    out = [
        f"RESUME: newest handoff {name} (assembled {made.group(1) if made else 'unknown'}), resumed {fmt_ts(now)}"
    ]
    changed = unchanged = unreadable = 0
    pr_reads = Path(request.pr_reads) if request.pr_reads else None
    for line in body.splitlines():
        f = ModelFact.parse_line(line)
        if not f or not f.probe or not f.stale(now):
            continue
        new = reprobe(f, policy, now, recorded, rows, pr_reads)
        if new is None:
            unreadable += 1
            out.append(f"  UNREADABLE {f.probe}: {f.text[:100]} (was {f.val or '?'})")
        elif new == f.val:
            unchanged += 1
            out.append(f"  UNCHANGED {f.probe}: {new}")
        else:
            changed += 1
            out.append(
                f"  CHANGED {f.probe}: {f.val or '?'} -> {new} (read {f.read_at}, re-read {fmt_ts(now)})"
            )
    skipped = sum(
        1
        for ln in body.splitlines()
        if (x := ModelFact.parse_line(ln)) and x.stale(now) and not x.probe
    )
    if since:
        new_rows = [
            r
            for r in rows
            if r.ts > since and r.type in ("RULING", "OPERATOR-CONSENT", "HOLD")
        ]
        out.append(
            f"  NEW since handoff: {len(new_rows)} RULING/OPERATOR-CONSENT/HOLD row(s)"
            + "".join(
                f"\n    {fmt_ts(r.ts)} {r.type} ticket={r.cells.get('ticket', '-')}"
                for r in new_rows[-10:]
            )
        )
    out.append(
        f"RESUME SUMMARY changed={changed} unchanged={unchanged} unreadable={unreadable} stale_without_probe={skipped}"
    )
    return ModelHandoffCheckResult(
        command=EnumHandoffCommand.RESUME, exit_code=0, output=out
    )
