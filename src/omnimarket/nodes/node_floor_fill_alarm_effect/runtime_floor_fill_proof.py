# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readers the floor fill contracts' integration checks run over a host's rows (``python -m <this module> <check> ...``).

Each check exits 0 when its proof holds and 1 with one line naming what is missing, so a shell check cannot pass on a
file it could not read. The install row (a STATUS row of the ticket carrying ``installed=<host>:<first 12 of the head>``)
is the revision anchor: only rows stamped at or after it are of the head's revision, so a CLAIM, a check line or a run
from an older install is never counted for this head.

- ``sending-runs``: two consecutive runs of the fill's run log (``seq`` n and n+1) that each sent at least one lane.
- ``claims-since-install``: at least ``--min`` CLAIM rows (the row type cell, never the alarm's own STATUS rows) carrying a
  whole ``key=floor:`` cell, stamped at or after the install row.
- ``checks-since-install``: a line of the alarm check log with outcome ``checked`` and ``alarms_found`` stamped at or after
  the install row.
- ``no-handoff``: the ledger file exists and holds no MSG to the landing controller naming the pull request.
- ``lab-head``: the pull request body's ``Lab: head=<sha>`` names a commit in merge-base..HEAD.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

CELL_SEPARATOR = " | "
FLOOR_KEY_PREFIX = "key=floor:"
INSTALL_SHA_CHARS = 12
LAB_HEAD = re.compile(r"(?:^|\n|\\n)Lab: head=([0-9a-f]{40})(?=\s|$)")


class ProofMissingError(Exception):
    """A proof the check reads is not there; the message names it."""


def _stamp(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _read_lines(path: Path, what: str) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProofMissingError(f"{what} {path} unreadable: {exc}") from exc


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.split(CELL_SEPARATOR)]


def install_stamp(ledger: Path, ticket: str, host: str, head: str) -> datetime:
    """The stamp of the first STATUS row of ``ticket`` carrying ``installed=<host>:<first 12 of head>`` and ``observed=``."""
    wanted = f"installed={host}:{head[:INSTALL_SHA_CHARS]}"
    for row in _read_lines(ledger, "ledger"):
        cells = _cells(row)
        if len(cells) < 3 or cells[1] != "STATUS" or f"ticket={ticket}" not in cells:
            continue
        if wanted in cells and any(c.startswith("observed=") for c in cells):
            stamp = _stamp(cells[0])
            if stamp is not None:
                return stamp
    raise ProofMissingError(f"no STATUS row of {ticket} carries {wanted} and observed=")


def sending_runs(runs_log: Path, since: datetime) -> None:
    sent: dict[int, int] = {}
    for line in _read_lines(runs_log, "run log"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            observed = _stamp(str(item["run"]["observed_at"]))
            if observed is None:
                raise ValueError("run has no aware observation time")
            if observed >= since:
                sent[int(item["seq"])] = int(item["run"]["lanes_sent"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProofMissingError(
                f"run log {runs_log} holds an unreadable line: {exc}"
            ) from exc
    for seq, lanes in sent.items():
        if lanes >= 1 and sent.get(seq + 1, 0) >= 1:
            return
    raise ProofMissingError("no two consecutive runs of the run log each sent a lane")


def claims_since_install(ledger: Path, since: datetime, minimum: int) -> None:
    count = 0
    for row in _read_lines(ledger, "ledger"):
        cells = _cells(row)
        stamp = _stamp(cells[0]) if cells else None
        if len(cells) < 3 or cells[1] != "CLAIM" or stamp is None or stamp < since:
            continue
        if any(cell.startswith(FLOOR_KEY_PREFIX) for cell in cells):
            count += 1
    if count < minimum:
        raise ProofMissingError(
            f"{count} floor CLAIM row(s) at or after the install, {minimum} needed"
        )


def checks_since_install(check_log: Path, since: datetime) -> None:
    for line in _read_lines(check_log, "check log"):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        stamp = _stamp(str(item.get("at", "")))
        if (
            item.get("outcome") == "checked"
            and "alarms_found" in item
            and stamp is not None
            and stamp >= since
        ):
            return
    raise ProofMissingError(
        "no alarm check line of outcome checked at or after the install"
    )


def no_handoff(ledger: Path, pull_request: str) -> None:
    for row in _read_lines(ledger, "ledger"):
        cells = _cells(row)
        if len(cells) < 3 or cells[1] != "MSG":
            continue
        fields = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
        if "landing-controller" not in fields.get("to", "").split(","):
            continue
        named = [
            name.removeprefix("OmniNode-ai/")
            for name in re.split(r"[,\s]+", fields.get("pr", ""))
        ]
        if pull_request.removeprefix("OmniNode-ai/") in named:
            raise ProofMissingError(
                f"the ledger holds a handoff row for {pull_request}"
            )


def lab_head(body_json: Path, checkout: Path) -> None:
    text = "\n".join(_read_lines(body_json, "pull request"))
    start = text.find("{")
    try:
        document = json.loads(text[start:] if start >= 0 else text)
        facts = document.get("facts", document.get("result", document))
        body = facts["body"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ProofMissingError("the pull request body field is unreadable") from exc
    if not isinstance(body, str):
        raise ProofMissingError("the pull request body field is not text")
    found = LAB_HEAD.findall(body)
    if not found:
        raise ProofMissingError(
            "the pull request body carries no Lab: head=<40-character sha> line"
        )
    sha = found[-1]
    # Resolve this checkout explicitly, including a linked worktree's gitfile; an
    # inherited lane GIT_DIR must never redirect a proof to a different checkout.
    env = {
        **os.environ,
        "GIT_DIR": str(checkout.resolve() / ".git"),
        "GIT_WORK_TREE": str(checkout.resolve()),
    }

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], env=env, capture_output=True, text=True, check=False
        )

    base = git("merge-base", "origin/dev", "HEAD")
    if base.returncode != 0:
        raise ProofMissingError("the pull request merge base is unreadable")
    branch_point = base.stdout.strip()
    if (
        git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0
        or git("merge-base", "--is-ancestor", sha, branch_point).returncode == 0
    ):
        raise ProofMissingError(f"Lab: head={sha} is outside merge-base..HEAD")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="floor_fill_proof_readers")
    sub = parser.add_subparsers(dest="check", required=True)
    runs = sub.add_parser("sending-runs")
    runs.add_argument("--runs", type=Path, required=True)
    for name in ("sending-runs", "claims-since-install", "checks-since-install"):
        item = runs if name == "sending-runs" else sub.add_parser(name)
        item.add_argument("--ledger", type=Path, required=True)
        item.add_argument("--ticket", required=True)
        item.add_argument("--host", required=True)
        item.add_argument("--head", required=True)
        if name == "claims-since-install":
            item.add_argument("--min", type=int, default=2, dest="minimum")
        elif name == "checks-since-install":
            item.add_argument("--check-log", type=Path, required=True)
    handoff = sub.add_parser("no-handoff")
    handoff.add_argument("--ledger", type=Path, required=True)
    handoff.add_argument("--pr", required=True)
    lab = sub.add_parser("lab-head")
    lab.add_argument("--body", type=Path, required=True)
    lab.add_argument("--checkout", type=Path, default=Path())
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.check == "sending-runs":
            since = install_stamp(args.ledger, args.ticket, args.host, args.head)
            sending_runs(args.runs, since)
        elif args.check == "claims-since-install":
            since = install_stamp(args.ledger, args.ticket, args.host, args.head)
            claims_since_install(args.ledger, since, args.minimum)
        elif args.check == "checks-since-install":
            since = install_stamp(args.ledger, args.ticket, args.host, args.head)
            checks_since_install(args.check_log, since)
        elif args.check == "no-handoff":
            no_handoff(args.ledger, args.pr)
        else:
            lab_head(args.body, args.checkout)
    except ProofMissingError as missing:
        sys.stderr.write(f"{missing}\n")
        return 1
    return 0


__all__: list[str] = [
    "ProofMissingError",
    "checks_since_install",
    "claims_since_install",
    "install_stamp",
    "lab_head",
    "main",
    "no_handoff",
    "sending_runs",
]

if __name__ == "__main__":
    sys.exit(main())
