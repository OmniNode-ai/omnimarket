# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic checks for plain-language Slack standup drafts."""

from __future__ import annotations

import re
from dataclasses import dataclass

from omnimarket.nodes.node_standup_draft_check_compute.models import (
    ModelStandupDraftCheckRequest,
    ModelStandupDraftCheckResult,
    ModelStandupFinding,
)

MAX_WORDS, MAX_LINKS, MAX_REFS = 170, 3, 2
MIN_THEMES, MAX_THEMES, MAX_THEME_WORDS, MAX_THEME_SENTENCES = 2, 5, 40, 2
# Internal wording a non-technical reader cannot use (whole words, any case).
JARGON_WORDS = (
    "pytest",
    "ledger",
    "guard",
    "guards",
    "CI",
    "flake",
    "flaky",
    "crash-loop",
    "crashloop",
    "restart count",
    "lane",
    "lanes",
    "controller",
    "orchestrator",
    "hook",
    "hooks",
    "PR",
    "PRs",
    "pull request",
    "pull requests",
    "commit",
    "commits",
    "branch",
    "branches",
    "repo",
    "repos",
    "repository",
    "repositories",
    "merge",
    "merged",
    "merges",
    "merging",
    "rebase",
    "worktree",
    "runner",
    "runners",
    "daemon",
    "pipeline",
    "kafka",
    "redpanda",
    "broker",
    "DLQ",
    "ulimit",
    "SHA",
    "schema",
    "payload",
    "endpoint",
)
PREAMBLE_OPENERS = (
    "today i ",
    "today i'",
    "today we ",
    "my status",
    "status for",
    "status update",
    "status:",
    "update:",
    "updates:",
    "daily status",
    "standup:",
    "standup update",
    "standup for",
    "stand-up:",
    "yesterday i ",
    "yesterday i'",
    "this week i",
    "last 24",
    "quick update",
    "here is",
    "here's",
    "here are",
    "summary",
    "good morning",
    "good afternoon",
    "hi ",
    "hello",
    "i worked",
    "i spent",
    "i have been",
    "i've been",
    "worked on",
    "spent ",
    "continued ",
    "continuing ",
)
LINK_RE = re.compile(
    r"<(?P<slack_url>https?://[^\s>|]+)(?:\|(?P<slack_label>[^>]*))?>"
    r"|\[(?P<md_label>[^\]\n]*)\]\((?P<md_url>[^\s)]+)\)"
    r"|(?P<bare_url>https?://[^\s<>]+)"
)
MENTION_RE = re.compile(r"<[@#][^>]+>")
TICKET_RE = re.compile(r"\b[A-Z]{2,6}-\d+\b")
PR_RE = re.compile(r"(?<![\w.-])[\w.-]*#(\d+)\b|/(?:pull|issues)/(\d+)\b")
LIST_RE = re.compile(r"^\s*(?:>\s*)?(?:[-*+]\s|[•◦" "\u2013" r"—]|\d+[.)]\s)")
LEADING_RE = re.compile(r"^[\s`*_~>+\-•◦" "\u2013" r"—]+")
THEME_RE = re.compile(
    r"^\s*(?:[•-]\s+)?\*(?P<label>[^*\n:]{2,40}?):?\*:?\s+(?P<body>\S.*)$"
)
COUNT_RE = re.compile(
    r"\b\d[\d,]*\s+(?:[A-Za-z-]+\s+)?(?:changes?|fixes|updates|improvements)\b"
)
# Shapes of code and internal ids: a criterion id, a code mark, a file name, snake_case, a flag.
CODE_SHAPES = (
    ("criterion id", re.compile(r"\b[Cc]\d{1,3}\b")),
    ("code mark", re.compile(r"`")),
    ("file name", re.compile(r"\b[\w-]+\.(?:py|yaml|yml|json|sh|md|js|toml)\b")),
    ("identifier", re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")),
    ("command flag", re.compile(r"(?<!\w)--[a-z][\w-]+")),
)


@dataclass(frozen=True)
class Finding:
    """A named rule violation and its repair instruction."""

    rule: str
    message: str


@dataclass(frozen=True)
class CheckResult:
    """All metrics and ordered findings for one draft."""

    ok: bool
    words: int
    themes: int
    links: int
    refs: int
    findings: list[Finding]


def _link_parts(match: re.Match[str]) -> tuple[str, str, str]:
    if match.group("slack_url") is not None:
        label = match.group("slack_label")
        return label if label is not None else "link", match["slack_url"], ""
    if match.group("md_url") is not None:
        return match["md_label"], match["md_url"], ""
    raw = match["bare_url"]
    url = raw.rstrip(".,!?;:)")
    return "link", url, raw[len(url) :]


def visible_text(text: str) -> str:
    """Replace links and mentions and strip token-edge markup; nonwords vanish."""
    text = MENTION_RE.sub("mention", text)
    text = LINK_RE.sub(lambda m: _link_parts(m)[0] + _link_parts(m)[2], text)
    return re.sub(
        r"\S+",
        lambda m: m[0].strip("-•◦\u2013—`*_~>") if re.search(r"\w", m[0]) else "",
        text,
    )


def _refs(text: str) -> int:
    shown = (
        visible_text(text)
        + " "
        + " ".join(m[1] for m in map(_link_parts, LINK_RE.finditer(text)))
    )
    tickets = set(TICKET_RE.findall(shown))
    return len(tickets) + len({a or b for a, b in PR_RE.findall(shown)})


def _still_blocked_index(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if LEADING_RE.sub("", lines[index]).casefold().startswith("still blocked:"):
            return index if all(line.strip() for line in lines[index:]) else None
    return None


def _first_sentence(visible: str) -> str:
    return re.split(r"[.!?](?=\s|$)|\n", LEADING_RE.sub("", visible), maxsplit=1)[0]


def jargon_found(text: str) -> list[str]:
    """Every internal word, id shape or code mark in the draft, in reading order."""
    visible, found = visible_text(text), []
    for term in JARGON_WORDS:
        pattern = re.escape(term).replace(r"\ ", r"\s+").replace(r"\-", "[- ]")
        if match := re.search(rf"(?<![\w-]){pattern}(?![\w-])", visible, re.IGNORECASE):
            found.append((match.start(), term))
    for label, regex in CODE_SHAPES:
        if match := regex.search(
            text if label in ("code mark", "command flag") else visible
        ):
            found.append((match.start(), f"{label} {match.group(0)!r}"))
    return [term for _, term in sorted(found)]


def check(text: str) -> CheckResult:
    """Check every rule in order; a failing draft is rewritten, never shown."""
    visible, lines = visible_text(text), text.splitlines()
    links = [_link_parts(m) for m in LINK_RE.finditer(text)]
    words, refs, found = len(visible.split()), _refs(text), jargon_found(text)
    themes = [
        (n, m)
        for n, line in enumerate(lines, 1)
        if (m := THEME_RE.match(line))
        and not m["label"].casefold().startswith(("still blocked", "m4 delegation"))
    ]
    blocked_at = _still_blocked_index(lines)
    first, first_n = (
        _first_sentence(visible),
        next((n for n, x in enumerate(lines, 1) if x.strip()), 0),
    )
    opener = next((o for o in PREAMBLE_OPENERS if first.casefold().startswith(o)), None)
    opener = (
        "still blocked:" if first.casefold().startswith("still blocked:") else opener
    )
    out: list[tuple[bool, str, str]] = [
        (
            words > MAX_WORDS,
            "words",
            f"{words} words, limit {MAX_WORDS}; cut the least consequential sentence",
        ),
        (
            not first.strip() or opener is not None,
            "first-sentence",
            (
                f"first sentence starts with preamble {opener!r}"
                if opener
                else "first sentence is empty"
            )
            + "; lead with a claim of change",
        ),
        (
            bool(themes) and themes[0][0] == first_n,
            "headline",
            "the message opens with a themed line; open with one plain headline sentence",
        ),
        (
            blocked_at is None,
            "still-blocked",
            "final paragraph must start with 'Still blocked:'; name what is blocked and who must act, or none",
        ),
        (
            not MIN_THEMES <= len(themes) <= MAX_THEMES,
            "themes",
            f"{len(themes)} grouped themes, need {MIN_THEMES} to {MAX_THEMES}; group the work by what it gives "
            "people as '*Label:* one outcome sentence (about N changes).'",
        ),
    ]
    for n, m in themes:
        shown = visible_text(m["body"])
        sentences = len(
            [
                s
                for s in re.split(
                    r"(?<=[.!?])\s+", re.sub(r"\([^)]*\)", "", shown).strip()
                )
                if s
            ]
        )
        out.append(
            (
                COUNT_RE.search(shown) is None,
                "theme-count",
                f"line {n} ('{m['label']}') has no rough count; end it with one such as 'about 12 changes'",
            )
        )
        out.append(
            (
                len(shown.split()) > MAX_THEME_WORDS or sentences > MAX_THEME_SENTENCES,
                "theme-long",
                f"line {n} ('{m['label']}') runs past one outcome sentence and a count",
            )
        )
    out.append(
        (
            bool(found),
            "jargon",
            "internal wording a non-technical reader cannot use: "
            + ", ".join(found)
            + "; say what the work does for people instead",
        )
    )
    for n, line in enumerate(lines, 1):
        listed = TICKET_RE.search(line) or PR_RE.search(line) or "linear.app/" in line
        out.append(
            (
                bool(LIST_RE.match(line) and listed),
                "ref-list",
                f"line {n} is a list of ticket or PR references; write a sentence and name one only if someone must act",
            )
        )
    body_refs = _refs("\n".join(lines[:blocked_at])) if blocked_at is not None else refs
    out += [
        (len(links) > MAX_LINKS, "links", f"{len(links)} links, limit {MAX_LINKS}"),
        (
            body_refs > 0,
            "refs-in-body",
            f"{body_refs} ticket or PR references above 'Still blocked:'; name one only in the final paragraph",
        ),
        (
            refs > MAX_REFS,
            "refs",
            f"{refs} ticket or PR references, limit {MAX_REFS}; Linear holds the rest",
        ),
    ]
    findings = [Finding(rule, message) for hit, rule, message in out if hit]
    return CheckResult(not findings, words, len(themes), len(links), refs, findings)


def _content_words(description: str) -> set[str]:
    return set(re.findall(r"[a-z]{5,}", description.casefold()))


def milestone_warnings(text: str, facts: object) -> list[str]:
    """Warn about transitions named by no criterion id, probe name or half the description's words."""
    milestone = facts.get("milestone") if isinstance(facts, dict) else None
    transitions = milestone.get("transitions") if isinstance(milestone, dict) else None
    warnings = []
    for item in transitions if isinstance(transitions, list) else []:
        crit, probe, desc = (
            item.get(k, "") if isinstance(item, dict) else ""
            for k in ("criterion", "probe", "description")
        )
        if (
            not isinstance(crit, str)
            or not isinstance(probe, str)
            or not (crit or probe)
        ):
            continue
        wanted = _content_words(desc) if isinstance(desc, str) else set()
        if (
            (crit and re.search(rf"\b{re.escape(crit)}\b", text))
            or (probe and probe.casefold() in text.casefold())
            or (wanted and len(wanted & _content_words(text)) * 2 >= len(wanted))
        ):
            continue
        warnings.append(
            f"STANDUP-CHECK WARN milestone transition absent from draft: {crit or probe}"
        )
    return warnings


def m4_delegation_warnings(text: str) -> list[str]:
    """Warn when no sentence says where M4 delegation stands."""
    if re.search(r"\bM4\s+delegation\b", visible_text(text), re.IGNORECASE):
        return []
    return ["STANDUP-CHECK WARN no sentence on where M4 delegation stands"]


class HandlerStandupDraftCheck:
    """Stateless, pure draft verdict with ordered advisory warnings."""

    def handle(
        self, request: ModelStandupDraftCheckRequest
    ) -> ModelStandupDraftCheckResult:
        warnings = milestone_warnings(request.text, request.facts)
        warnings += m4_delegation_warnings(request.text)
        result = check(request.text)
        return ModelStandupDraftCheckResult(
            ok=result.ok,
            words=result.words,
            themes=result.themes,
            links=result.links,
            refs=result.refs,
            findings=tuple(
                ModelStandupFinding(rule=f.rule, message=f.message)
                for f in result.findings
            ),
            warnings=tuple(warnings),
        )
