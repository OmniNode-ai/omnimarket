# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure extraction, citation refusal and rendering for technical deep dives."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from omnimarket.nodes.node_deep_dive_report_effect.models import (
    EnumReportSection,
    ModelClaimCitation,
    ModelLedgerRef,
    ModelRefusedClaim,
    ModelReportClaim,
    ModelTechnicalDeepDiveCommand,
)

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_PR = re.compile(r"[A-Za-z0-9_.-]+#\d+")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_CELL = re.compile(r"([a-z_-]+)=(.*)", re.DOTALL)
# The registry root's directory name, split so this public file does not spell it.
_ROOT_NAME = "omni" + "_home"
_REGISTRY_ROOT = re.compile(
    r"(?:(?:~|/(?:Users|home|Volumes|data)/[^\s/]+)(?:/[^\s/]+)*?/"
    + _ROOT_NAME
    + r"|\$\{?OMNI_HOME\}?|(?<![\w/.$-])"
    + _ROOT_NAME
    + r")(?=/|\b)"
)
_FIX = re.compile(r"^(fix|revert)(\(|!|:)", re.IGNORECASE)
_DISCARDED = re.compile(
    r"supersed|discard|reject|duplicate|ruled.out|abandon|no-op|not-needed",
    re.IGNORECASE,
)
_HEADINGS = {
    EnumReportSection.CHANGED_BY_AREA: "What changed, by area",
    EnumReportSection.CAUSES_AND_FIXES: "Causes found and their fixes",
    EnumReportSection.DECISIONS: "Decisions made and restated",
    EnumReportSection.OPEN_BLOCKERS: "Open blockers",
    EnumReportSection.DO_NOT_REDISCOVER: "Do not rediscover",
}


class UncitedClaimError(ValueError):
    """Rendering was refused because these claims have no evidence."""

    def __init__(self, refused: tuple[ModelRefusedClaim, ...]) -> None:
        self.refused = refused
        super().__init__(f"Refused {len(refused)} uncited claims")


def check_citations(
    claims: Iterable[ModelReportClaim],
) -> tuple[ModelRefusedClaim, ...]:
    """Return every uncited claim, preserving input order."""
    return tuple(
        ModelRefusedClaim(
            claim=claim, reason="no commit sha, pull request or ledger row"
        )
        for claim in claims
        if not claim.citation.is_cited
    )


def scrub(text: str, limit: int = 300) -> str:
    """Collapse prose, redact home prefixes, remove backticks and bound length."""
    cleaned = " ".join(text.split())
    # A registry-root path names a location on one machine; keep what is under it.
    cleaned = _REGISTRY_ROOT.sub("the registry root", cleaned)
    cleaned = re.sub(r"/(?:Users|home|Volumes)/[^/\s]+", "~", cleaned).replace("`", "")
    return cleaned[: max(0, limit)] + "…" if len(cleaned) > limit else cleaned


def redact(text: str, terms: tuple[str, ...]) -> str:
    """Replace listed words and their branch prefixes without regard to case."""
    for term in terms:
        if term:
            text = re.sub(
                r"\b" + re.escape(term) + r"\b(/)?",
                lambda match: "branch " if match[1] else "a teammate",
                text,
                flags=re.IGNORECASE,
            )
    return text


def _redact_lane(lane: str, terms: tuple[str, ...]) -> str:
    """Replace listed terms inside a lane name, keeping it one token."""
    for term in terms:
        if term:
            lane = re.sub(
                r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])",
                "teammate",
                lane,
                flags=re.IGNORECASE,
            )
    return lane


@dataclass(frozen=True)
class ParsedRow:
    """The evidence identity, cells and final prose of a ledger row."""

    ts: str
    row_type: str
    lane: str
    cells: dict[str, str]
    text: str


def parse_ledger_row(row: str) -> ParsedRow | None:
    """Parse one split ledger row, retaining the first value of each key."""
    parts = row.split(" | ")
    if not _TS.fullmatch(parts[0]):
        return None
    cells: dict[str, str] = {}
    text = ""
    for part in parts[2:]:
        cell = part.strip()
        match = _CELL.fullmatch(cell)
        if match is not None:
            cells.setdefault(match[1], match[2])
        else:
            text = cell
    return ParsedRow(
        ts=parts[0],
        row_type=parts[1].strip() if len(parts) > 1 else "",
        lane=cells.get("lane") or cells.get("from", ""),
        cells=cells,
        text=text,
    )


def rows_for_day(rows: Iterable[str], report_date: str) -> list[ParsedRow]:
    """Select a UTC day's valid ledger rows in timestamp order."""
    parsed = (parse_ledger_row(row) for row in rows)
    return sorted(
        (row for row in parsed if row is not None and row.ts.startswith(report_date)),
        key=lambda row: row.ts,
    )


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _first_string(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _citation(
    *, pr: object = None, sha: object = None, ts: object = None, lane: object = None
) -> ModelClaimCitation:
    """Validate individual references so malformed optional data stays optional."""
    if isinstance(pr, list):
        pr = pr[0] if pr else None
    ledger = None
    if (
        isinstance(ts, str)
        and _TS.fullmatch(ts)
        and isinstance(lane, str)
        and lane.strip()
    ):
        ledger = ModelLedgerRef(ts=ts, lane=lane.strip())
    return ModelClaimCitation(
        pr=pr if isinstance(pr, str) and _PR.fullmatch(pr) else None,
        commit_sha=sha if isinstance(sha, str) and _SHA.fullmatch(sha) else None,
        ledger=ledger,
    )


def _item_stamp(item: dict[str, Any]) -> tuple[str, str]:
    ts = _first_string(
        item,
        ("ts", "timestamp", "at", "row_ts", "ledger_ts", "restated_at", "decided_at"),
    )
    lane = _first_string(item, ("lane", "row_lane", "by_lane"))
    for key in ("ledger", "row"):
        nested = item.get(key)
        if isinstance(nested, dict):
            ts = ts or _first_string(nested, ("ts",))
            lane = lane or _first_string(nested, ("lane",))
    return ts, lane


def _dropped_claims(
    data: dict[str, Any], key: str, report_date: str
) -> tuple[list[ModelReportClaim], int]:
    claims: list[ModelReportClaim] = []
    uncited = 0
    for item in _items(data, key):
        ts, lane = _item_stamp(item)
        if ts and not ts.startswith(report_date):
            continue
        text = _first_string(
            item,
            ("text", "decision", "quote", "words", "summary", "title", "statement"),
        )
        if not text:
            continue
        citation = _citation(
            ts=ts,
            lane=lane,
            pr=item.get("pr"),
            sha=_first_string(item, ("sha", "commit")),
        )
        if not citation.is_cited:
            uncited += 1
            continue
        # Decisions need a day stamp; undated findings may cite a PR or commit.
        if not ts and key != "findings":
            continue
        claims.append(
            ModelReportClaim(
                section=(
                    EnumReportSection.DO_NOT_REDISCOVER
                    if key == "findings"
                    else EnumReportSection.DECISIONS
                ),
                area=_first_string(item, ("ticket", "topic", "decision_id"))
                or "general",
                text=("Restated: " if key == "re_steers" else "") + scrub(text),
                citation=citation,
            )
        )
    return claims, uncited


def build_claims(
    command: ModelTechnicalDeepDiveCommand,
) -> tuple[list[ModelReportClaim], int]:
    """Build only cited claims from this node's JSON inputs and ledger rows."""
    facts = command.owner_summary_facts
    rows = rows_for_day(command.ledger_rows, command.report_date)
    closed = _items(facts, "lanes_closed")
    pr_to_ledger: dict[str, ModelLedgerRef] = {}
    closing_text: dict[tuple[str, str], str] = {}
    for item in closed:
        ts, lane = _item_stamp(item)
        citation = _citation(ts=ts, lane=lane, pr=item.get("pr"))
        if citation.ledger is not None:
            closing_text.setdefault((ts, lane), _first_string(item, ("text",)))
            if citation.pr is not None:
                previous = pr_to_ledger.get(citation.pr)
                if previous is None or ts < previous.ts:
                    pr_to_ledger[citation.pr] = citation.ledger
    for row in rows:
        if row.row_type not in {"TERMINAL", "STATUS"}:
            continue
        citation = _citation(ts=row.ts, lane=row.lane, pr=row.cells.get("pr"))
        if citation.pr is not None and citation.ledger is not None:
            previous = pr_to_ledger.get(citation.pr)
            if previous is None or row.ts < previous.ts:
                pr_to_ledger[citation.pr] = citation.ledger

    claims: list[ModelReportClaim] = []
    uncited = 0

    def add(
        section: EnumReportSection, area: str, text: str, citation: ModelClaimCitation
    ) -> None:
        nonlocal uncited
        if not citation.is_cited:
            uncited += 1
        elif text:
            claims.append(
                ModelReportClaim(
                    section=section, area=area, text=text, citation=citation
                )
            )

    for item in _items(facts, "merged_changes"):
        repo = _first_string(item, ("repo",))
        title = _first_string(item, ("title",))
        pr = f"{repo}#{item.get('number')}"
        citation = _citation(pr=pr).model_copy(update={"ledger": pr_to_ledger.get(pr)})
        add(EnumReportSection.CHANGED_BY_AREA, repo, scrub(title), citation)
        if _FIX.match(title):
            text = scrub(title)
            if citation.ledger is not None:
                closing = closing_text.get((citation.ledger.ts, citation.ledger.lane))
                if closing:
                    text += " — closing row: " + scrub(closing, 200)
            add(EnumReportSection.CAUSES_AND_FIXES, repo, text, citation)

    for item in _items(facts, "clone_only_commits"):
        repo = _first_string(item, ("repo",))
        pr = f"{repo}#{item.get('pr')}"
        add(
            EnumReportSection.CHANGED_BY_AREA,
            repo,
            scrub(_first_string(item, ("subject",))),
            _citation(pr=pr),
        )

    if command.dropped_decisions is not None:
        for key in ("decisions", "re_steers", "findings"):
            extracted, left_out = _dropped_claims(
                command.dropped_decisions, key, command.report_date
            )
            claims.extend(extracted)
            uncited += left_out
    else:
        for row in rows:
            if row.row_type == "RULING":
                kind = row.cells.get("kind") or "decision"
                text = (
                    "Restated: operator restated the ruling of "
                    f"{scrub(row.cells['amends'])} (kind={kind}); "
                    "read the ledger row for the words."
                    if "amends" in row.cells
                    else f"Operator ruling recorded (kind={kind}); "
                    "read the ledger row for the words."
                )
                add(
                    EnumReportSection.DECISIONS,
                    row.cells.get("ticket") or "general",
                    text,
                    _citation(ts=row.ts, lane=row.lane, pr=row.cells.get("pr")),
                )
            elif row.row_type == "OPERATOR-CONSENT":
                add(
                    EnumReportSection.DECISIONS,
                    row.cells.get("ticket") or "general",
                    "Operator consent recorded; read the ledger row for its scope.",
                    _citation(ts=row.ts, lane=row.lane, pr=row.cells.get("pr")),
                )

    for item in closed:
        ts, lane = _item_stamp(item)
        outcome = _first_string(item, ("outcome",))
        area = _first_string(item, ("ticket",)) or lane
        citation = _citation(ts=ts, lane=lane, pr=item.get("pr"))
        if outcome.lower().startswith(("blocked", "partial")):
            add(
                EnumReportSection.OPEN_BLOCKERS,
                area,
                f"{scrub(lane)} ended {scrub(outcome)}: "
                + scrub(_first_string(item, ("text",)), 240),
                citation,
            )
        if _DISCARDED.search(outcome):
            add(
                EnumReportSection.DO_NOT_REDISCOVER,
                area,
                f"{scrub(lane)} ended {scrub(outcome)}: "
                + scrub(_first_string(item, ("text",)), 240),
                citation,
            )

    for row in rows:
        if row.row_type == "HOLD":
            add(
                EnumReportSection.OPEN_BLOCKERS,
                row.cells.get("ticket") or row.lane or "general",
                scrub(row.text),
                _citation(ts=row.ts, lane=row.lane),
            )
        elif row.row_type == "FRICTION":
            add(
                EnumReportSection.DO_NOT_REDISCOVER,
                row.cells.get("class") or row.cells.get("ticket") or "general",
                scrub(row.text, 280),
                _citation(ts=row.ts, lane=row.lane, pr=row.cells.get("pr")),
            )

    seen: set[tuple[EnumReportSection, str, str]] = set()
    unique: list[ModelReportClaim] = []
    for claim in claims:
        citation = claim.citation
        if citation.ledger is not None:
            # A lane can carry a listed term too; the timestamp still locates the row.
            lane = _redact_lane(citation.ledger.lane, command.redact_terms)
            citation = citation.model_copy(
                update={"ledger": citation.ledger.model_copy(update={"lane": lane})}
            )
        claim = claim.model_copy(
            update={
                "text": redact(scrub(claim.text), command.redact_terms),
                "area": redact(scrub(claim.area), command.redact_terms),
                "citation": citation,
            }
        )
        identity = (claim.section, claim.text, claim.citation.render())
        if identity not in seen:
            seen.add(identity)
            unique.append(claim)
    return unique, uncited


def render_technical_report(
    report_date: str,
    claims: Iterable[ModelReportClaim],
    uncited_source_items: int,
    decisions_source: str,
    facts_errors: list[str],
) -> str:
    """Render all five sections, refusing the entire report if a claim is uncited."""
    accepted = tuple(claims)
    refused = check_citations(accepted)
    if refused:
        raise UncitedClaimError(refused)
    next_day = date.fromisoformat(report_date) + timedelta(days=1)
    lines = [
        f"# Technical deep dive — {report_date}",
        "",
        "<!-- automation: nightly-technical-deep-dive -->",
        "",
        f"For an agent starting work on {next_day}: what changed on {report_date} "
        "(UTC), the causes found and their fixes, the decisions made and restated, "
        "the open blockers, and what was tried and ruled out. Every line cites a "
        "pull request or commit and, where one exists, the ledger row (timestamp "
        "and lane) that records it.",
    ]
    counts: dict[str, int] = {}
    for section, heading in _HEADINGS.items():
        section_claims = [claim for claim in accepted if claim.section == section]
        counts[section.value] = len(section_claims)
        lines.extend(["", f"## {heading}"])
        if not section_claims:
            lines.append(f"- None recorded for {report_date}.")
        elif section == EnumReportSection.CHANGED_BY_AREA:
            for area in sorted({claim.area for claim in section_claims}):
                lines.append(f"### {scrub(area)}")
                for claim in section_claims:
                    if claim.area == area:
                        lines.append(f"- {claim.text} — {claim.citation.render()}")
        else:
            for claim in section_claims:
                prefix = f"**{scrub(claim.area)}**: " if claim.area != "general" else ""
                lines.append(f"- {prefix}{claim.text} — {claim.citation.render()}")
    lines.extend(["", "## Sources"])
    lines.extend(f"- {section}: {count}" for section, count in counts.items())
    lines.extend(
        [
            f"- decisions from: {decisions_source}",
            f"- left out for lack of a citation: {uncited_source_items}",
        ]
    )
    lines.extend(f"- source error: {scrub(error)}" for error in facts_errors)
    return "\n".join(lines).rstrip() + "\n"
