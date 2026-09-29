# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The request a delegate prompt makes, separated from the material it carries (OMN-19523).

WHY THIS EXISTS. Task-class selection used to search the whole prompt. A prompt
is usually a short request followed by the material the request is about: a
diff, a log, a merge conflict, a linter message, a sample prompt. On
2026-09-25 the delegation capability matrix measured the result: a pasted
``pytest`` comment in a YAML merge conflict classed a conflict resolution as a
test module (run 6bfe152a), a quoted linter line reading ``needs review``
classed a code edit as a review (run 20118c8a), quoted sample prompts opening
``Summarize in one sentence`` classed a Python fix as a summary (run 40e9de70),
and ``No praise, no summary of the change`` classed a code review as a summary
(run a8219faf). Each answer was then graded on a shape nobody asked for.

WHAT COUNTS AS MATERIAL, each by syntax and never by vocabulary:

* **Fenced code blocks** (backtick or tilde fences, CommonMark rules: a closing
  fence is the same character, at least as long; an unclosed fence runs to
  the end of the prompt).
* **Inline code spans** (a run of backticks and its matching run).
* **Paths, refs and repository slugs**: a whitespace-free token with a slash
  between word characters (``tests/nodes/x``, ``origin/dev``,
  ``OmniNode-ai/omnimarket``). It names a thing; its segments are not words
  of the request, so ``tests/`` in a pasted command cannot qualify ``pytest``
  as a request to write tests (run 87a0138d).
* **Quoted strings on one line**: straight or curly double quotes, and single
  quotes that open after a non-word character and close before one, so an
  apostrophe (``don't``, ``the lanes' rows``) never opens a quote.

WHAT THE OPENING SENTENCE IS. The first sentence of the request's first
non-blank line, without list or heading markers and without a leading
"please". A request usually opens with its verb and object ("Write three
Pydantic model modules", "Review this pull request diff") and then states the
facts the work needs, and those facts are full of nouns that are also class
phrases ("an immutable digest", "a one-line docstring"). The resolver lets the
opening sentence decide when, and only when, it opens with a declared phrase.

WHAT A NEGATION IS. A phrase preceded, within :data:`NEGATION_WINDOW_WORDS`
words of its own clause, by a negating word (``not``, ``no``, ``never``,
``without``, ``don't`` ...) is the caller saying what they do NOT want. It is
never a request for it. Clause punctuation ends the window, so ``if it is not
ready, review it`` still asks for a review.

Nothing here knows any class's vocabulary. The classes' phrases stay the
contract's; this module only decides which part of the prompt they are read
against.
"""

from __future__ import annotations

import re

__all__ = [
    "NEGATION_WINDOW_WORDS",
    "instruction_text",
    "instruction_word_count",
    "is_negated",
    "opening_sentence",
]

#: How many words before a phrase a negating word may sit and still negate it.
#: Three covers ``not a code review``, ``no need to summarize`` and ``do not
#: write a docstring`` (the cue is the third word back in each) without
#: reaching into an unrelated earlier phrase of the same clause.
NEGATION_WINDOW_WORDS = 3

#: Words that negate what follows them in the same clause. ``nor`` and the
#: contracted forms are included because a lowercase prompt is searched.
_NEGATION_CUES = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "nor",
        "don't",
        "dont",
        "doesn't",
        "didn't",
        "isn't",
        "aren't",
        "won't",
        "shouldn't",
        "mustn't",
        "cannot",
        "can't",
        "avoid",
        "skip",
        "instead",
        "rather",
    }
)

#: Markers a line may open with that are not words of the request: headings,
#: list bullets, list numbers and block quotes.
_LINE_MARKERS = re.compile(r"^[\s#>*+\-]*(?:\d+[.)]\s+)?")

#: A courtesy word that may precede the opening verb.
_COURTESY = re.compile(r"^please[\s,]+")

#: The end of a sentence: terminal punctuation followed by space or the end.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")

#: Punctuation that ends a clause, and with it a negation's reach.
_CLAUSE_BREAK = re.compile(r"[.;:!?,()\[\]\n]")

#: One word as the negation window counts words: letters, digits and the
#: apostrophe of a contraction.
_WINDOW_WORD = re.compile(r"[\w']+")

#: An opening code fence: up to three spaces of indent, then three or more
#: backticks or tildes.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")

#: An inline code span: a run of backticks, anything but that run, the same run.
_INLINE_CODE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)

#: A path, ref or slug: no whitespace, and a slash between word characters.
_PATH_TOKEN = re.compile(r"(?<!\S)\S*\w/\w\S*")

#: Quoted strings confined to one line.
_DOUBLE_QUOTED = re.compile(r'"[^"\n]*"')
_CURLY_QUOTED = re.compile("\u201c[^\u201d\n]*\u201d")
_SINGLE_QUOTED = re.compile(r"(?<![\w'])'[^'\n]*'(?![\w'])")
_CURLY_SINGLE_QUOTED = re.compile("(?<!\\w)\u2018[^\u2019\n]*\u2019(?!\\w)")


def _strip_fenced_blocks(prompt: str) -> str:
    """Return ``prompt`` with every fenced block replaced by a blank line."""
    kept: list[str] = []
    fence: str | None = None
    for line in prompt.splitlines():
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            if opened is None:
                kept.append(line)
                continue
            fence = opened.group(1)
            kept.append("")
            continue
        stripped = line.strip()
        if (
            stripped
            and stripped[0] == fence[0]
            and len(stripped) >= len(fence)
            and set(stripped) == {fence[0]}
        ):
            fence = None
    return "\n".join(kept)


def instruction_text(prompt: str) -> str:
    """Return the words of ``prompt`` that make the request, not the material.

    Fenced blocks go first, so a quote character inside code can never pair
    with one in the instruction. Each removed span becomes a single space, so
    the words on either side stay separate words.
    """
    text = _strip_fenced_blocks(prompt)
    text = _INLINE_CODE.sub(" ", text)
    text = _PATH_TOKEN.sub(" ", text)
    for pattern in (
        _DOUBLE_QUOTED,
        _CURLY_QUOTED,
        _SINGLE_QUOTED,
        _CURLY_SINGLE_QUOTED,
    ):
        text = pattern.sub(" ", text)
    return text


def instruction_word_count(prompt: str) -> int:
    """Return how many words the request itself is, for the class shape gates."""
    return len(instruction_text(prompt).split())


def is_negated(text: str, start: int) -> bool:
    """Return whether the phrase starting at ``start`` in ``text`` is negated.

    Only the text before the phrase and inside its own clause is read, and only
    the last :data:`NEGATION_WINDOW_WORDS` words of it.
    """
    before = text[:start]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    clause = before[breaks[-1].end() :] if breaks else before
    words = _WINDOW_WORD.findall(clause)[-NEGATION_WINDOW_WORDS:]
    return any(word in _NEGATION_CUES for word in words)


def opening_sentence(instruction: str) -> str:
    """Return the request's opening sentence, lowercased, markers removed.

    ``instruction`` is the output of :func:`instruction_text`. An instruction
    with no non-blank line has an empty opening sentence, which opens with no
    phrase at all.
    """
    for line in instruction.splitlines():
        stripped = _LINE_MARKERS.sub("", line).strip().lower()
        if not stripped:
            continue
        stripped = _COURTESY.sub("", stripped)
        end = _SENTENCE_END.search(stripped)
        return stripped[: end.start()] if end else stripped
    return ""
