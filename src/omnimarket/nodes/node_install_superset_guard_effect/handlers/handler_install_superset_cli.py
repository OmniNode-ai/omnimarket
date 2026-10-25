# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check and record superset plans for the landing controller's installer (install.sh).

The check compares the live tree and commits before writing plan and drop files.
The record command stores the accepted plan and appends its install history.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, resolve_setting

from ..models.model_install_superset import (
    ModelCarriedCommit,
    ModelDrop,
    ModelSupersetRequest,
    ModelSupersetResult,
)
from .handler_install_superset import (
    HandlerInstallSuperset,
    SupersetError,
    git,
    resolve,
    text,
    tree_differences,
)

PLUGIN_INSTALL_OVERLAY = "plugin_install"
PLUGIN_INSTALL_FIELDS = frozenset({"ledger_ticket"})
REFUSED = "INSTALL-SUPERSET-REFUSED"
CONTROLLER_REL = "skills/merge-drain/scripts/landing_controller.py"
SIDECAR_REL = "skills/merge-drain/scripts/landing-controller.sha256"


class RefusalError(Exception):
    """The install is refused; the message says why and what to pass."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def source_commit(source: Path) -> tuple[Path, str, str]:
    """``(repo, prefix, HEAD)`` of the clean git work tree ``source`` sits in."""
    try:
        top = Path(text(git(source, "rev-parse", "--show-toplevel")).strip()).resolve()
    except SupersetError as exc:
        raise RefusalError(
            f"BRANCH-SOURCE-NOT-GIT: a branch install names its commit, and {source} is in no git work tree (install from the worktree of the branch, not a copy)"
        ) from exc
    prefix = source.resolve().relative_to(top).as_posix()
    plugin = str(Path(prefix).parent)
    status = text(git(top, "status", "--porcelain", "--", plugin, "src"))
    dirty = [line.strip() for line in status.splitlines() if line]
    if dirty:
        raise RefusalError(
            f"BRANCH-SOURCE-DIRTY: {top} has {len(dirty)} uncommitted change(s) under {plugin} or src, so the installed tree would equal no commit: {'; '.join(dirty[:5])}"
        )
    return (top, prefix, resolve(top, "HEAD"))


def named_live_commit(
    a: argparse.Namespace, record: dict[str, Any], superset: dict[str, Any]
) -> tuple[str, str]:
    """``(commit, where it came from)`` of the live install, before it is checked."""
    if a.live_commit:
        return (a.live_commit, "--live-commit")
    if superset.get("commit"):
        return (str(superset["commit"]), "landing_install.json superset.commit")
    revision = load_json(Path(a.state_dir) / "installed-revision.json")
    sidecar = Path(a.install_dir) / SIDECAR_REL
    live_sha = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
    if (
        "branch_until" not in record
        and revision.get("source_commit")
        and live_sha
        and (revision.get("controller_sha256") == live_sha)
    ):
        return (str(revision["source_commit"]), "installed-revision.json")
    raise RefusalError(
        "LIVE-UNRECORDED: the live install records no commit (installed before this check); pass --live-commit <the commit it was installed from>, which is checked file by file against the live tree; the last entry of INSTALLED-FROM-BRANCHES.txt names it"
    )


def live_commit(
    a: argparse.Namespace,
    repo: Path,
    prefix: str,
    record: dict[str, Any],
    superset: dict[str, Any],
) -> tuple[str, str]:
    """``(commit, origin)`` of the live install, checked against the live tree; "" for none."""
    install_dir = Path(a.install_dir)
    if not (install_dir / CONTROLLER_REL).is_file():
        return ("", "no live install")
    commit, origin = named_live_commit(a, record, superset)
    try:
        commit = resolve(repo, commit)
        compared, differ = tree_differences(
            repo, commit, str(superset.get("prefix") or prefix), install_dir
        )
    except SupersetError as exc:
        raise RefusalError(f"LIVE-COMMIT-UNKNOWN: {exc}") from exc
    if not compared or differ:
        detail = f"{len(differ)} of {compared} file(s) differ: {', '.join(differ[:8])}"
        raise RefusalError(
            f"LIVE-TREE-MISMATCH: the live tree is not {commit[:12]} ({origin}); {(detail if compared else 'no file compared')}. Name the commit it was installed from with --live-commit"
        )
    return (commit, origin)


def drop_row(
    a: argparse.Namespace, result: ModelSupersetResult, index: int, reason: str
) -> str:
    m = result.dropped[index]
    ticket = resolve_setting(
        PLUGIN_INSTALL_OVERLAY, "ledger_ticket", fields=PLUGIN_INSTALL_FIELDS
    )
    return f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} | STATUS | lane={a.lane} | ticket={ticket} | actor=script:install-superset | model=none | INSTALL-SUPERSET-DROP target=landing-controller commit={m.commit} installed_by={m.lane} live={result.live_commit[:12]} new={result.new_commit[:12]} | {reason} ({m.subject})".replace(
        "\n", " "
    )


def cmd_check(a: argparse.Namespace) -> int:
    if len(a.drop) != len(a.reason) or any(not r.strip() for r in a.reason):
        raise RefusalError("every --drop <commit> needs its own --reason <text>")
    history_path = Path(a.history)
    history = (
        history_path.read_text("utf-8", errors="replace")
        if history_path.is_file()
        else ""
    )
    record = load_json(Path(a.install_dir) / "landing_install.json")
    raw_superset = record.get("superset")
    superset = raw_superset if isinstance(raw_superset, dict) else {}
    repo, prefix, new = source_commit(Path(a.source))
    live, origin = live_commit(a, repo, prefix, record, superset)
    keys = ("commit", "patch_id", "lane", "subject")
    carried = tuple(
        ModelCarriedCommit(**{k: str(c.get(k, "")) for k in keys})
        for c in superset.get("carried") or []
        if isinstance(c, dict)
    )
    try:
        reasons = {resolve(repo, d): r for d, r in zip(a.drop, a.reason, strict=True)}
        result = HandlerInstallSuperset().handle(
            ModelSupersetRequest(
                repo=repo,
                new_commit=new,
                lane=a.lane,
                live_commit=live,
                live_carried=carried,
                drops=tuple(
                    (ModelDrop(commit=d, reason=r) for d, r in reasons.items())
                ),
                main_ref=a.main_ref,
                history=history,
            )
        )
    except SupersetError as exc:
        raise RefusalError(f"GIT-READ-FAILED: {exc}") from exc
    for m in result.missing:
        sys.stderr.write(
            f"INSTALL-SUPERSET-MISSING {m.commit[:12]} lane={m.lane} {m.subject}\n"
        )
    if result.unused_drops:
        raise RefusalError(
            f"--drop names {', '.join(s[:12] for s in result.unused_drops)}, which the new tree is not missing; drop only a commit this check prints; nothing was changed"
        )
    if result.verdict == "refused":
        raise RefusalError(
            f"{len(result.missing)} commit(s) of the live install {live[:12]} ({origin}) are not in {new[:12]}: build the new install on the live one (merge or cherry-pick them), or pass --drop <commit> --reason <text> for each one left out on purpose; nothing was changed"
        )
    try:
        rows = [
            drop_row(a, result, i, reasons[m.commit])
            for i, m in enumerate(result.dropped)
        ]
    except NodeOverlayError as exc:
        raise RefusalError(str(exc)) from exc
    Path(a.drop_rows).write_text("".join(row + "\n" for row in rows), encoding="utf-8")
    plan = {
        "commit": new,
        "repo": str(repo),
        "prefix": prefix,
        "lane": a.lane,
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "over": live,
        "carried": [c.model_dump() for c in result.carried],
        "dropped": [
            {"commit": m.commit, "lane": m.lane, "reason": reasons[m.commit]}
            for m in result.dropped
        ],
    }
    Path(a.plan).write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
    how = ", ".join((f"{c[:12]} by {w}" for c, w in result.held if w != "ancestor"))
    sys.stdout.write(
        f"INSTALL-SUPERSET-CONTAINED new={new[:12]} live={live[:12] or 'none'} ({origin}) held {how or 'by ancestry'}; dropped={len(result.dropped)} carried={len(result.carried)}\n"
    )
    return 0


def cmd_record(a: argparse.Namespace) -> int:
    plan = load_json(Path(a.plan))
    if not plan.get("commit"):
        raise RefusalError(f"no plan at {a.plan}")
    record_path = Path(a.install_record)
    record = load_json(record_path)
    record["superset"] = plan
    write_json(record_path, record)
    dropped = ",".join(d["commit"][:12] for d in plan.get("dropped", [])) or "none"
    with Path(a.history).open("a", encoding="utf-8") as fh:
        fh.write(
            f"\nSUPERSET {plan['at']} lane={plan['lane']} commit={plan['commit']} over={plan['over'] or 'none'} carried={len(plan.get('carried', []))} dropped={dropped} (written by install.sh; the typed record is superset in landing_install.json)\n"
        )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    for flag in (
        "--install-dir",
        "--state-dir",
        "--source",
        "--lane",
        "--history",
        "--plan",
    ):
        c.add_argument(flag, required=True)
    c.add_argument(
        "--drop-rows",
        required=True,
        help="where the ledger row of each drop is written",
    )
    c.add_argument("--live-commit", default="")
    c.add_argument("--main-ref", default="origin/main")
    c.add_argument("--drop", action="append", default=[])
    c.add_argument("--reason", action="append", default=[])
    r = sub.add_parser("record")
    for flag in ("--plan", "--install-record", "--history"):
        r.add_argument(flag, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return cmd_check(a) if a.cmd == "check" else cmd_record(a)
    except RefusalError as exc:
        sys.stderr.write(f"{REFUSED}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
