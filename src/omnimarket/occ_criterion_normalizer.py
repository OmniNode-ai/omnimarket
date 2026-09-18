# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One comparison text, computed identically from markdown and from rich text.

OMN-18332 AC2f. The transcriber has to answer one question per criterion: is
the text that is live today still the text the ticket was CREATED with? The two
sides of that comparison arrive in different formats and there is no way to
convert either into the other:

* the live side is the issue's ``description`` -- markdown;
* the creation side is a ``documentContentHistory`` entry's ``contentData`` -- a
  ProseMirror document. No markdown form of a PAST revision is exposed;
  ``DocumentContent.content`` is markdown for the CURRENT revision only.

Hashing a raw markdown criterion against a rich-text snapshot therefore never
matches, which would mark every criterion of every ticket as rewritten and
close nothing. Rendering the snapshot back to markdown does not work either:
markdown is not canonical, so ``**bold**`` and ``__bold__`` are the same
document and different bytes.

**So neither side is converted to the other. Both are projected onto a third
form** -- block structure preserved, inline formatting discarded -- and the
projection is what :func:`omnimarket.occ_criterion_units.criterion_units` then
parses and hashes. Block structure is kept because it is what the criteria
parser segments on (a heading opens the section, a list item opens an item);
inline formatting is discarded because it is the half that cannot round-trip.

What this makes invisible, stated rather than implied: emphasis added or
removed, a term wrapped in code formatting, a link's URL changed under
unchanged link text, and a reflowed line. A criterion whose WORDING moves is
still caught, which is the property AC2 rests on. A link retargeted under
identical anchor text is the one real gap; it is narrower than the alternative,
which is catching nothing because nothing ever matches.

TWO asymmetries are NOT presentation and are removed rather than tolerated
(OMN-18667). Both make an UNEDITED criterion read as moved, which refuses its
acceptance with no human act available to clear it, because no human edited
anything.

**Second: a code span's content was literal on one side only.** ``_CODE_SPAN``
unwraps a span to its content and ``_EMPHASIS`` then runs over the whole line,
so an asterisk that was INSIDE backticks is deleted once the backticks are
gone -- the comment on ``_EMPHASIS`` below says the ordering exists to prevent
exactly that, and unwrapping first defeats it. ``the `a*b` token`` projected to
``the ab token`` while the rich-text side, which drops the ``code`` mark and
keeps the text, projected ``the a*b token``. No backslash is needed to trigger
it. It held OMN-18667's own AC1, whose text cites ``` `\\-`, `\\*`, `\\_`, `\\#` ```
and lost two of the four. So a code span's content is now parked out of the
strippers' reach as well.

**First: the serializer's escaping.** The markdown side is written by Linear's
serializer, which
backslash-escapes a character that would otherwise be read as block structure
at the start of a line, while the rich-text side holds that character bare. A
criterion that WRAPS onto such a line therefore differed by one byte with no
edit behind it. Measured on OMN-18620, whose ``documentContentHistory`` has a
single entry at its ``createdAt``: five of six criteria hashed equal and AC5
alone differed, on a leading ``--`` the serializer had written as ``\\--``. The
acceptance was withheld, the ticket held on ``gap_ac_unbound``, and no human
act could clear it because no human had edited anything. So the markdown side
now inverts the serializer's escaping before anything else -- and the
**markdown side alone**: applying the same pass to the rich text would turn an
author's literal ``\\-`` into ``-`` and reintroduce the asymmetry in the
opposite direction.

Measured on a real ticket (OMN-18331, 17 blocks) the live markdown and the
latest ``contentData`` revision project onto **17 of 17 byte-identical blocks**
through the functions below. That measurement is pinned as a test over a
committed snapshot of that ticket, because a normalizer proven against a
hand-built fixture only proves the fixture.
"""

from __future__ import annotations

import re
from typing import Any, Final

__all__ = [
    "markdown_comparison_text",
    "rich_text_comparison_text",
]

# --- markdown side ---------------------------------------------------------

#: Linear renders an issue reference into the description as an ``<issue>``
#: element wrapping the identifier, and into ``contentData`` as an
#: ``issueMention`` node whose ``label`` attr is that same identifier. Unwrapping
#: to the identifier is what makes the two agree.
_ISSUE_TAG: Final[re.Pattern[str]] = re.compile(
    r"<issue\b[^>]*>(.*?)</issue>", re.DOTALL
)
_IMAGE: Final[re.Pattern[str]] = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK: Final[re.Pattern[str]] = re.compile(
    r"\[([^\]]*)\]\(\s*<?[^)\s]*>?\s*(?:\"[^\"]*\")?\)"
)
_AUTOLINK: Final[re.Pattern[str]] = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_CODE_SPAN: Final[re.Pattern[str]] = re.compile(r"`+([^`]*)`+")

#: Asterisk and tilde emphasis run anywhere; underscore emphasis only at a word
#: boundary. Without the boundary condition ``ROLLING_WORK_LEDGER.md`` loses its
#: underscores on the markdown side and keeps them on the rich-text side, and
#: an untouched criterion citing a snake_case path reads as rewritten. That is
#: not hypothetical: it was the last of two mismatches in the 17-block
#: measurement above.
_EMPHASIS: Final[re.Pattern[str]] = re.compile(
    r"\*\*\*|\*\*|\*|~~|(?<!\w)_{1,3}|_{1,3}(?!\w)"
)

#: CommonMark: "any ASCII punctuation character may be backslash-escaped", and
#: a backslash before anything else is a literal backslash. That rule is exactly
#: what a markdown serializer inverts, so unescaping this set and nothing else
#: is the serializer's inverse rather than a guess at which escapes Linear emits
#: today. Linear's is the prosemirror-markdown one, which escapes
#: ``` ` * \ ~ [ ] _ ``` anywhere and ``: # - * +`` plus a ``\d+.`` ordinal at
#: the start of a line -- a strict subset.
_ASCII_PUNCT: Final[frozenset[str]] = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

#: Scans one code span at a position. Escapes do NOT apply inside a code span
#: (CommonMark), and the rich-text side keeps that text verbatim, so unescaping
#: in there would drop a character on one side only. A criterion citing a regex
#: -- ``\d+\.`` -- is the realistic case. Deliberately as loose as ``_CODE_SPAN``
#: below so the two agree on where a span begins and ends.
_CODE_SPAN_SCAN: Final[re.Pattern[str]] = re.compile(r"`+[^`]*`+")

#: An unescaped character is parked on a private-use codepoint while the inline
#: strippers run, then restored. It cannot be done before they run -- ``\*``
#: would become a bare ``*`` for ``_EMPHASIS`` to delete -- and it cannot be
#: done after, because ``_EMPHASIS`` would already have deleted the ``*`` and
#: left the orphaned backslash. Either order loses the character the author
#: typed, on the markdown side only.
_ESCAPE_SENTINEL_BASE: Final[int] = 0xE000

_MD_HEADING: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+")
_MD_LIST_ITEM: Final[re.Pattern[str]] = re.compile(r"^(\s*)([*+-]|\d+[.)])\s+")

#: Every list item projects to this one marker regardless of the bullet
#: character or ordinal it was written with, so a bullet changed from ``-`` to
#: ``*``, or a list renumbered, is not read as a rewritten criterion. The marker
#: is one the vendored ``_LIST_ITEM`` pattern matches.
_LIST_MARKER: Final[str] = "* "


def _park_literal_punctuation(text: str) -> tuple[str, bool]:
    """Every character that is TEXT rather than markup, parked out of reach.

    Two sources, both of which the rich-text side keeps verbatim while the
    strippers below would otherwise consume them on the markdown side alone:

    1. a backslash escape -- ``\\X`` becomes the character ``X``;
    2. the CONTENT of a code span -- literal by definition, so nothing inside
       one may be read as emphasis, a link, or anything else. The backtick runs
       are left in place so ``_CODE_SPAN`` still unwraps the span itself.

    Escapes are NOT resolved inside a code span, because CommonMark does not
    resolve them there and the rich-text side keeps the backslash.

    Returns the parked text and whether anything was parked. The flag exists so
    a body with neither source -- which is most of them -- takes the identical
    path it took before this function existed, and cannot be perturbed by the
    restore pass.
    """
    if "\\" not in text and "`" not in text:
        return text, False
    # A body already carrying a sentinel codepoint could not be restored
    # unambiguously, so the escapes are left alone rather than risk turning
    # one character into another. That is the pre-OMN-18667 projection for
    # such a body: a stale comparison, never a corrupted one.
    if any(
        _ESCAPE_SENTINEL_BASE + 0x21 <= ord(ch) <= _ESCAPE_SENTINEL_BASE + 0x7E
        for ch in text
    ):
        return text, False

    out: list[str] = []
    index = 0
    length = len(text)
    parked = False
    while index < length:
        char = text[index]
        if char == "`":
            span = _CODE_SPAN_SCAN.match(text, index)
            if span is not None:
                opening = len(span.group(0)) - len(span.group(0).lstrip("`"))
                closing = len(span.group(0)) - len(span.group(0).rstrip("`"))
                body = span.group(0)[opening : len(span.group(0)) - closing]
                out.append("`" * opening)
                for content_char in body:
                    if content_char in _ASCII_PUNCT:
                        out.append(chr(_ESCAPE_SENTINEL_BASE + ord(content_char)))
                        parked = True
                    else:
                        out.append(content_char)
                out.append("`" * closing)
                index = span.end()
                continue
        if char == "\\" and index + 1 < length and text[index + 1] in _ASCII_PUNCT:
            out.append(chr(_ESCAPE_SENTINEL_BASE + ord(text[index + 1])))
            index += 2
            parked = True
            continue
        out.append(char)
        index += 1
    return "".join(out), parked


def _restore_escaped_punctuation(text: str) -> str:
    """Every parked sentinel back to the character it stood for."""
    return "".join(
        chr(ord(ch) - _ESCAPE_SENTINEL_BASE)
        if _ESCAPE_SENTINEL_BASE + 0x21 <= ord(ch) <= _ESCAPE_SENTINEL_BASE + 0x7E
        else ch
        for ch in text
    )


def _strip_inline_markup(text: str) -> str:
    """Markdown inline markup removed, the text it decorated kept.

    Order matters: images before links (an image is a link with a leading
    ``!``), links before autolinks, and code spans before emphasis so an
    asterisk INSIDE backticks is not read as emphasis.

    Backslash escapes are resolved FIRST and parked (OMN-18667), because an
    escaped character is text the author typed, not markup -- the rich-text
    side carries it bare, and every stripper below would otherwise treat the
    markup character as markup on the markdown side alone.
    """
    text, parked = _park_literal_punctuation(text)
    text = _ISSUE_TAG.sub(lambda match: match.group(1), text)
    text = _IMAGE.sub(lambda match: match.group(1), text)
    text = _LINK.sub(lambda match: match.group(1), text)
    text = _AUTOLINK.sub(lambda match: match.group(1), text)
    text = _CODE_SPAN.sub(lambda match: match.group(1), text)
    text = _EMPHASIS.sub("", text)
    return _restore_escaped_punctuation(text) if parked else text


def markdown_comparison_text(description: str) -> str:
    """Project a markdown description onto the comparison form.

    One block per line, blank lines dropped, headings keeping their ``#`` run
    and list items normalised to a single marker -- because those two are what
    the criteria parser segments on and everything else about a block is
    presentation.
    """
    blocks: list[str] = []
    for raw in description.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        heading = _MD_HEADING.match(line)
        if heading is not None:
            body = " ".join(_strip_inline_markup(line[heading.end() :]).split())
            if body:
                blocks.append(f"{heading.group(1)} {body}")
            continue
        item = _MD_LIST_ITEM.match(line)
        if item is not None:
            body = " ".join(_strip_inline_markup(line[item.end() :]).split())
            if body:
                blocks.append(_LIST_MARKER + body)
            continue
        body = " ".join(_strip_inline_markup(line).split())
        if body:
            blocks.append(body)
    return "\n".join(blocks)


# --- rich-text side --------------------------------------------------------

#: Node types whose children are blocks in their own right rather than inline
#: content, and which contribute no marker themselves.
_TRANSPARENT_BLOCKS: Final[frozenset[str]] = frozenset({"doc", "blockquote"})

#: Node types each of whose children is one list item.
_LIST_BLOCKS: Final[frozenset[str]] = frozenset(
    {"bullet_list", "ordered_list", "todo_list", "task_list"}
)

#: Inline nodes that stand for an entity and carry their display text on an
#: attr rather than as a ``text`` child.
_MENTION_NODES: Final[frozenset[str]] = frozenset(
    {
        "issueMention",
        "mention",
        "userMention",
        "projectMention",
        "documentMention",
        "cycleMention",
    }
)

#: A soft line break inside one paragraph. Markdown writes the two halves as two
#: LINES, so projecting it to a space would join two blocks on one side only --
#: which was the first of the two mismatches in the 17-block measurement.
_HARD_BREAK: Final[str] = "\n"


def _inline_text(node: dict[str, Any]) -> str:
    """The visible text of an inline node or an inline subtree, marks dropped.

    Marks are not consulted at all: ``strong``, ``em``, ``code``, ``link`` and
    Linear's own ``attribution`` mark all decorate text without changing it, and
    discarding them is the whole point of the projection.
    """
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hard_break":
        return _HARD_BREAK
    if kind in _MENTION_NODES:
        attrs = node.get("attrs") or {}
        return str(attrs.get("label") or attrs.get("title") or "")
    children = node.get("content") or []
    return "".join(_inline_text(child) for child in children if isinstance(child, dict))


def _emit(node: dict[str, Any], marker: str, blocks: list[str]) -> None:
    kind = node.get("type")
    children = [
        child for child in (node.get("content") or []) if isinstance(child, dict)
    ]

    if kind in _TRANSPARENT_BLOCKS:
        for child in children:
            _emit(child, marker, blocks)
        return
    if kind in _LIST_BLOCKS:
        for child in children:
            _emit(child, _LIST_MARKER, blocks)
        return
    if kind == "list_item":
        first = True
        for child in children:
            _emit(child, marker if first else "", blocks)
            first = False
        return
    if kind == "heading":
        level = (node.get("attrs") or {}).get("level", 1)
        try:
            depth = max(1, min(6, int(level)))
        except (TypeError, ValueError):
            depth = 1
        body = " ".join(_inline_text(node).split())
        if body:
            blocks.append("#" * depth + " " + body)
        return

    body = _inline_text(node)
    if not body.strip():
        # A block with no text of its own (a nested list, a table) still has
        # block children worth projecting. Recursing rather than dropping is
        # what keeps a criterion inside one from disappearing.
        for child in children:
            _emit(child, marker, blocks)
        return
    first = True
    for segment in body.split(_HARD_BREAK):
        collapsed = " ".join(segment.split())
        if not collapsed:
            continue
        blocks.append((marker + collapsed) if (marker and first) else collapsed)
        first = False


def rich_text_comparison_text(content_data: dict[str, Any]) -> str:
    """Project a ProseMirror ``contentData`` document onto the comparison form.

    The output is directly comparable to :func:`markdown_comparison_text` over
    the same document's markdown, and is what the criteria parser is handed.
    """
    if not isinstance(content_data, dict):
        msg = (
            "a revision's contentData must be a rich-text document object; got "
            f"{type(content_data).__name__}"
        )
        raise TypeError(msg)
    blocks: list[str] = []
    _emit(content_data, "", blocks)
    return "\n".join(blocks)
