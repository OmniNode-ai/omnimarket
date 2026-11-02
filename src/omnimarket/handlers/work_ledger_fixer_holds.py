# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository fixer HOLD semantics shared by landing and lab fill."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow


def _cell(text: str, name: str) -> str:
    for part in text.split("\n", 1)[0].split("|"):
        part = part.strip().strip("`")
        if part.startswith(name + "="):
            return part[len(name) + 1 :].strip()
    return ""


def fixer_hold_reasons(
    rows: Iterable[ModelLandingLedgerRow], now: datetime
) -> dict[str, str]:
    """Unreleased, unexpired scope=fixer HOLDs, retaining the declaring row.

    RELEASE re= accepts the HOLD's id or timestamp, as in the landing controller.
    A RELEASE that names a pr= is scoped: it lifts those PRs only (see
    ``fixer_hold_released_prs``) and leaves the repository HOLD in force.
    Owner-qualified repository names are normalized to their short name.
    """
    rows = [row for row in rows if row.ts <= now.strftime("%Y-%m-%dT%H:%M:%SZ")]
    released = {
        _cell(row.text, "re")
        for row in rows
        if row.rtype == "RELEASE" and not _cell(row.text, "pr")
    } - {""}
    out: dict[str, str] = {}
    for row in rows:
        if row.rtype != "HOLD" or _cell(row.text, "scope") != "fixer":
            continue
        if (_cell(row.text, "id") or row.ts) in released or row.ts in released:
            continue
        until = _cell(row.text, "until")
        if until:
            fmt = "%Y-%m-%dT%H:%M:%SZ" if until.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
            try:
                expires = datetime.strptime(until, fmt).replace(tzinfo=UTC)
            except ValueError:
                expires = None
            if expires is not None and expires <= now:
                continue
        for repo in _cell(row.text, "repo").split(","):
            repo = repo.strip().split("/", 1)[-1]
            if repo:
                out[repo] = row.text.split("\n", 1)[0]
    return out


def fixer_hold_released_prs(
    rows: Iterable[ModelLandingLedgerRow], now: datetime, owner: str = "OmniNode-ai"
) -> list[str]:
    """``owner/repo#n`` of every PR a scoped RELEASE lifts out of a HOLD still in force.

    A scoped RELEASE carries ``re=`` (the HOLD's id or timestamp) and ``pr=``
    (``repo#n``, comma-separated for several). It lifts nothing else in that repository.
    """
    rows = sorted(rows, key=lambda row: row.ts)
    live = fixer_hold_reasons(rows, now)
    holds = {
        (_cell(row.text, "id") or row.ts): row
        for row in rows
        if row.rtype == "HOLD" and _cell(row.text, "scope") == "fixer"
    }
    holds.update({row.ts: row for row in holds.values()})
    out: set[str] = set()
    for row in rows:
        if row.rtype != "RELEASE" or row.ts > now.strftime("%Y-%m-%dT%H:%M:%SZ"):
            continue
        hold = holds.get(_cell(row.text, "re"))
        if hold is None:
            continue
        held = {
            r.strip().split("/", 1)[-1] for r in _cell(hold.text, "repo").split(",")
        }
        for pr in _cell(row.text, "pr").split(","):
            repo, sep, number = pr.strip().partition("#")
            short = repo.split("/", 1)[-1]
            if sep and number.isdigit() and short in live and short in held:
                out.add(f"{repo if '/' in repo else f'{owner}/{repo}'}#{number}")
    return sorted(out)
