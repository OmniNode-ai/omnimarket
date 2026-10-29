# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A message the operator reads names every ticket and PR in plain words (OMN-20894).

Four rules, from RULING 2026-10-10T16:55:54Z as corrected by CORRECTION 2026-10-10T16:56:54Z
and RULING 2026-10-10T17:54:32Z (only steps doable from a phone when the operator is remote):

* Bare ids, ALWAYS ON in every mode. An ``OMN-n``, ``repo#n`` or ``PR #n`` token is bare unless
  its sentence, or the whole list item it sits in, names something in plain words: at least two
  plain words (three letters or more, not filler such as "the", "ticket" or "PR") once the ids
  are taken out. Accepted forms: ``the infra startup-check fix (omnibase_infra#4802)``,
  ``OMN-20892 (the access-mode detector)``, ``- OMN-20892: the access-mode detector``, a later
  ``Then omnimarket#4530 merges.`` inside a list item whose lead line names it, and a table row
  whose other cells name it. A list item is its lead line plus its continuation lines (the
  non-blank lines after it, and indented lines after a blank line). "Done: OMN-20892." or
  "OMN-20892 is done" is bare. An id inside inline code or a quotation (double quotes, curly
  quotes, single quotes not part of a word) is an example, not prose, and is not flagged.
* Local file paths, REMOTE mode (and every push, which lands on the phone). A path under the
  home, volume, private, tmp, var or opt roots, a ``~/`` path or an ``$OMNI_HOME/`` path cannot be
  opened from a phone; at the terminal they are clickable and allowed.

* Phone-infeasible steps, REMOTE mode (and every push). A step that needs a terminal or a key
  ("open a terminal and create a key", "generate an SSH key", ``ssh-keygen``) cannot be done from
  a phone; do it yourself, or ask for a decision the operator can give with a tap or a reply.

* Repeated reply, ALWAYS ON in every mode (OMN-20893). A reply whose normalised text (case,
  digits and times masked) matches one of the session's last three replies, with no tool call
  between them, is refused: a re-firing goal stop hook answered by the same line each turn ended
  the turn about 150 times. The refusal asks for one concrete action or for ending without a
  reply. A reply with no text is never checked, so the guard cannot itself force a reply.

Fenced code blocks and web links are not scanned: a quoted command or a URL is not prose. Inline
code and quotations are still scanned for paths and phone steps; only their ids are skipped. The
check is structural, not semantic: plain words anywhere in the sentence or list item count as a
name even when they describe something else ("I checked the logs, OMN-20892" passes), so it
catches the bare id, the common failure, and not every weak name.
"""

from __future__ import annotations

import re

from ..models.enum_access_mode import EnumAccessMode
from ..models.model_hook_verdict import ModelHookVerdict
from ..models.model_operator_reply_request import ModelOperatorReplyRequest

ID_PATTERN = re.compile(
    r"(?<![\w/-])(?:OMN-\d+|PR ?#\d+|[A-Za-z][A-Za-z0-9_.-]*#\d+)(?![\w-])"
)
PATH_PATTERN = re.compile(
    r"(?<![\w.:/~$@-])(?:file://|~/|\$OMNI_HOME/|\$HOME/|"
    r"/(?:Users|Volumes|private|tmp|var|opt|home|Library|etc)/)[^\s)\]>'\"`,;]*"
)
STEP_VERBS = r"(?:open|launch|start)"
STEP_PLACES = r"(?:terminal|command[ -]line|command prompt)"
KEY_VERBS = r"(?:create|generate|mint|issue)"
PHONE_INFEASIBLE_PATTERN = re.compile(
    rf"\b{STEP_VERBS}\s+(?:up\s+)?(?:a|an|the|your)\s+(?:new\s+)?{STEP_PLACES}\b"
    rf"|\b(?:run|paste|type)\b[^\n.]{{0,60}}\b(?:in|into|at|on)\s+(?:a|an|the|your)\s+"
    rf"(?:new\s+)?{STEP_PLACES}\b"
    rf"|\b{KEY_VERBS}\s+(?:a|an|the|your|new)\s+(?:new\s+)?(?:[A-Za-z]+\s+){{0,2}}"
    r"(?:key|keypair|key pair)(?![-\w])"
    r"|\bssh-keygen\b|\bgpg\s+--(?:gen-key|full-generate-key)\b",
    re.IGNORECASE,
)
FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1[^\n]*$", re.DOTALL | re.MULTILINE)
WEB_LINK = re.compile(r"https?://\S+")
LINK_TARGET = re.compile(r"\]\((?:https?://)[^)]*\)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
LIST_LEAD = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
QUOTED_SPANS = (
    re.compile(r"(?<!`)`(?!`)([^`\n]*)`(?!`)"),
    re.compile(r'"([^"\n]*)"'),
    re.compile(r"\u201c([^\u201d\n]*)\u201d"),
    re.compile(r"(?<!\w)'([^'\n]*)'(?!\w)"),
)
PLAIN_WORD = re.compile(r"^[A-Za-z][A-Za-z'\u2019-]+$")
FILLER = frozenset(
    {
        "the",
        "and",
        "for",
        "see",
        "per",
        "via",
        "with",
        "from",
        "into",
        "onto",
        "its",
        "this",
        "that",
        "these",
        "those",
        "also",
        "only",
        "was",
        "were",
        "are",
        "has",
        "had",
        "have",
        "not",
        "but",
        "then",
        "now",
        "here",
        "there",
        "ticket",
        "tickets",
        "issue",
        "issues",
        "pr",
        "prs",
        "pull",
        "request",
        "requests",
        "number",
        "item",
        "items",
        "under",
        "done",
        "merged",
        "open",
        "closed",
        "landed",
        "filed",
        "same",
        "still",
    }
)
ID_EXAMPLE = "the infra startup-check fix (omnibase_infra#4802)"
REPLY_GUARD = "operator_access_mode_reply_check"
REPLY_HEAD = "BLOCKED (operator reply, OMN-20894): "
REPEAT_HEAD = "BLOCKED (operator reply, OMN-20893): "
REPEAT_WINDOW = 3
NON_WORD = re.compile(r"[^a-z0-9]+")
DIGITS = re.compile(r"\d+")


def normalise_reply(text: str) -> str:
    """``text`` lower-cased, digits (so clock times and counts) masked, punctuation squeezed."""
    return NON_WORD.sub(" ", DIGITS.sub("0", text.lower())).strip()


def _plain_words(text: str) -> int:
    count = 0
    for raw in ID_PATTERN.sub(" ", text).split():
        token = raw.strip(".,;:!?()[]{}\"'`*_|")
        if len(token) >= 3 and PLAIN_WORD.match(token) and token.lower() not in FILLER:
            count += 1
    return count


def _scannable(text: str) -> str:
    text = FENCE.sub(" ", text)
    text = LINK_TARGET.sub("]", text)
    return WEB_LINK.sub(" ", text)


def _unquoted_ids(text: str) -> str:
    """``text`` with every id inside inline code or a quotation blanked; other words are kept."""
    chars = list(text)
    for pattern in QUOTED_SPANS:
        for span in pattern.finditer(text):
            for match in ID_PATTERN.finditer(text, span.start(1), span.end(1)):
                chars[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|")


def _item_end(lines: list[str], lead: int) -> int:
    """One past the last line of the list item whose lead line is ``lines[lead]``."""
    end = lead + 1
    while end < len(lines):
        line = lines[end]
        if LIST_LEAD.match(line) or _is_table_row(line):
            return end
        if line.strip():
            end += 1
            continue
        after = end
        while after < len(lines) and not lines[after].strip():
            after += 1
        if after == len(lines):
            return end
        follow = lines[after]
        if (
            not follow.startswith("  ")
            or LIST_LEAD.match(follow)
            or _is_table_row(follow)
        ):
            return end
        end = after + 1
    return end


def _units(text: str) -> list[tuple[str, str | None]]:
    """Each line of ``text`` with the whole list item it belongs to, or None outside a list."""
    lines = text.splitlines()
    units: list[tuple[str, str | None]] = []
    index = 0
    while index < len(lines):
        if _is_table_row(lines[index]) or not LIST_LEAD.match(lines[index]):
            units.append((lines[index], None))
            index += 1
            continue
        end = _item_end(lines, index)
        item = "\n".join(lines[index:end])
        units.extend((line, item) for line in lines[index:end])
        index = end
    return units


def bare_ids(text: str) -> list[str]:
    """Every id token in ``text`` with no plain-words name in its sentence or list item, in
    order, once each."""
    found: list[str] = []
    for line, item in _units(_unquoted_ids(_scannable(text))):
        if _is_table_row(line):
            segments = [line] if _plain_words(line) < 2 else []
        elif item is not None:
            segments = [line] if _plain_words(item) < 2 else []
        else:
            segments = [s for s in SENTENCE_END.split(line) if _plain_words(s) < 2]
        for segment in segments:
            for match in ID_PATTERN.finditer(segment):
                if match.group(0) not in found:
                    found.append(match.group(0))
    return found


def phone_infeasible_steps(text: str) -> list[str]:
    """The phrases in ``text`` that ask for a step needing a terminal or a key, once each."""
    found: list[str] = []
    for match in PHONE_INFEASIBLE_PATTERN.finditer(_scannable(text)):
        phrase = " ".join(match.group(0).split()).lower()
        if phrase not in found:
            found.append(phrase)
    return found


def local_paths(text: str) -> list[str]:
    found: list[str] = []
    for match in PATH_PATTERN.finditer(_scannable(text)):
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def repeats_recent_reply(text: str, recent_replies: tuple[str, ...]) -> bool:
    """True when ``text`` says what one of the last three ``recent_replies`` said."""
    wanted = normalise_reply(text)
    if not wanted:
        return False
    return any(
        normalise_reply(prior) == wanted for prior in recent_replies[-REPEAT_WINDOW:]
    )


class HandlerOperatorReplyCheck:
    """Refuse an operator-facing message with a bare id, a local path where it cannot open, or
    a repeat of one of the last three replies."""

    def handle(self, request: ModelOperatorReplyRequest) -> ModelHookVerdict:
        if repeats_recent_reply(request.text, request.recent_replies):
            return ModelHookVerdict.refuse(
                REPEAT_HEAD
                + "repeated reply: this reply says what one of your last three"
                " replies said, with no tool call since. Do one concrete action this turn (a"
                " due tick, a finished lane, a free slot) or end without a reply.",
                REPLY_GUARD,
                "repeated-reply",
            )
        problems: list[str] = []
        classes: list[str] = []
        ids = bare_ids(request.text)
        if ids:
            classes.append("bare-ticket-or-pr-ids")
            shown = ", ".join(ids[:6]) + (" and more" if len(ids) > 6 else "")
            problems.append(
                f"bare ticket or PR ids ({shown}): name each one in plain words with the id"
                f" beside it, for example: {ID_EXAMPLE}"
            )
        if request.mode is EnumAccessMode.REMOTE or request.to_phone:
            paths = local_paths(request.text)
            if paths:
                classes.append("local-file-paths")
                shown = ", ".join(paths[:4]) + (" and more" if len(paths) > 4 else "")
                problems.append(
                    f"local file paths ({shown}): the operator is on a phone and cannot open"
                    " them; say what the file holds, paste the part that matters, or publish it"
                    " and give the link"
                )
            steps = phone_infeasible_steps(request.text)
            if steps:
                classes.append("phone-infeasible-steps")
                shown = ", ".join(f"'{step}'" for step in steps[:4])
                problems.append(
                    f"steps a phone cannot do ({shown}): the operator is on a phone with no"
                    " terminal and no keys; do the step yourself, or ask for a decision they"
                    " can give by replying"
                )
        if not problems:
            return ModelHookVerdict.allow()
        return ModelHookVerdict.refuse(
            REPLY_HEAD
            + "; ".join(problems)
            + ". Rewrite the message and send it again.",
            REPLY_GUARD,
            "-and-".join(classes),
        )
