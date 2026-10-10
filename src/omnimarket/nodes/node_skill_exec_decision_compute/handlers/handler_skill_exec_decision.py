# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the skill executor (OMN-20686).

The caller supplies argument text, environment and file facts, then runs the planned
command. This handler preserves the retired executor's parsing, refusals, commands
and reports without reading files, running processes or consulting a clock.

RULING relations remain the caller's choice: automatically adding --amends once
marked unrelated same-ticket rulings amended (2026-09-28). Only corrected examples
suggest relations; the command never adds them on the caller's behalf.
"""

from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePath

from omnimarket.nodes.node_skill_exec_decision_compute.models.model_skill_exec_decision import (
    ModelSkillExecRequest,
    ModelSkillExecResult,
)

ROW_TYPES = (
    "CLAIM",
    "STATUS",
    "TERMINAL",
    "FRICTION",
    "CORRECTION",
    "RULING",
    "OPERATOR-CONSENT",
    "MSG",
    "ACK",
    "HOLD",
    "RELEASE",
)
RULING_KINDS = ("ask", "ruling", "consent", "hold", "release")
MSG_KINDS = ("msg", "hold", "ack", "release")
STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
STAMP_RE = re.compile(rf"^{STAMP}$")

# Row kinds that must carry an executor (OMN-19898, T2 shape iii of the merge-drain-that-merges
# plan): when the caller passes neither --actor nor --model, the lane's own environment supplies
# both or the row is refused before the fork, never partially attributed and never silently sent
# on to the onex-ledger attribution guard (which only enforces this for CLAIM).
ATTRIBUTED_ROW_TYPES = ("CLAIM", "STATUS", "TERMINAL")

# A `'` with a letter on both sides is a contraction or possessive ("agent's", "don't"), never a
# shell quote boundary. shlex has no way to tell the two apart, so it treats every `'` as a
# delimiter and an unmatched one anywhere in the header raises "No closing quotation" — refusing
# ordinary English inside a quoted header phrase (OMN-19898, T2 shape i). Swap each one for a
# sentinel with no special meaning to shlex before splitting, then restore it in every resulting
# token: this changes nothing for a header that already parsed cleanly, and turns the header-fed
# apostrophe into ordinary content instead of a break in the quoting.
_CONTRACTION_APOSTROPHE = re.compile(r"(?<=\w)'(?=\w)")
_APOSTROPHE_SENTINEL = "\x00APOSTROPHE\x00"


def _protect_contraction_apostrophes(text: str) -> str:
    return _CONTRACTION_APOSTROPHE.sub(_APOSTROPHE_SENTINEL, text)


def _restore_contraction_apostrophes(tokens: list[str]) -> list[str]:
    return [token.replace(_APOSTROPHE_SENTINEL, "'") for token in tokens]


USAGE = {
    "operator-ruling": (
        "<ask|ruling|consent|hold|release> --lane <lane> [record.sh flags]  (or --kind <that word>)\n"
        "---\n"
        "<the operator's words, verbatim; ruling and consent only>\n"
        "  flags as record.sh takes them: ask --ticket --question --options --recommend;\n"
        "  ruling --question Q --ticket (--mechanism PATH | --mechanism-ticket OMN-n | --kind decision)\n"
        "         [--amends S] [--supersedes S] [--unrelated S] [--re ID] [--note] [--join-lines]\n"
        "         [--relay-to L --relay-text T]; every open ruling on --ticket is named once as\n"
        "         --amends (it changes it), --supersedes (it replaces it) or --unrelated (a different\n"
        "         matter under the same ticket; that ruling stays in force), or it is refused;\n"
        "  consent --approved '<a; b>' --out-of-scope '<c; d>' [--amends <earlier consent stamp>] [--away]\n"
        "         [--approved-by X] [--join-lines]  (both scope lists are header flags, never the body);\n"
        "  hold --to L|--repo R|--pr N with --until/--after/--proof; release --re <hold id>.\n"
        "  --lane may be left out when --session-env names a session.env that sets SESSION_LANE."
    ),
    "ledger-write": (
        "<TYPE> --lane <lane> [onex-ledger row flags]\n"
        "---\n"
        "<the row's free text: one line, no pipe>\n"
        "  TYPE: CLAIM STATUS TERMINAL FRICTION CORRECTION MSG ACK HOLD RELEASE\n"
        "  (RULING and OPERATOR-CONSENT go through /omni:operator-ruling, which keeps the words\n"
        "  verbatim and adds the OUT OF SCOPE floor).\n"
        "  e.g. CLAIM --lane L --ticket OMN-n --actor A --model M --est-hours 2 --displaces X\n"
        "       TERMINAL --lane L --friction none --outcome done --closes-claim <claim stamp>\n"
        "or: append-rows, then --- and the pre-built rows, one per line."
    ),
    "ledger-msg": (
        "<msg|hold|ack|release> --lane <lane> [onex-ledger row flags]\n"
        "---\n"
        "<the message text: one line, no pipe>\n"
        "  msg --to <lane|all|operator> [--question Q] [--ticket OMN-n]; ack --to L --re <id>;\n"
        "  hold --to L|--repo R|--pr N|--surface S with --until/--after/--proof; release --re <hold id>\n"
        "or: inbox <lane[,lane...]> [--limit N]"
    ),
    "friction-record": (
        "--lane <lane> (--ticket OMN-n | --existing OMN-n) --cost '~15 lane-minutes' [--source-lane L]\n"
        "---\n"
        "<SYMPTOM IN CAPS. What happened, citing rows by stamp and lane. Correct move: ...>"
    ),
}


class ArgsError(Exception):
    """The argument text cannot be used; exit 64 with the skill's usage."""


@dataclass(frozen=True)
class Parsed:
    header: list[str]
    body: str | None


def parse_arguments(text: str) -> Parsed:
    """Split the argument text into shell-quoted header tokens and a verbatim body."""
    if text.startswith("ARGUMENTS: "):
        text = text[len("ARGUMENTS: ") :]
    lines = text.splitlines(keepends=True)
    header_lines: list[str] = []
    body: str | None = None
    seen_header_content = False
    for index, line in enumerate(lines):
        if line.rstrip("\r\n").strip() == "---":
            if not seen_header_content:
                # A stray leading `---` fence (a caller opening the argument text with one, as
                # if it needed a matching pair of markdown-style fences) before any real header
                # token: drop it and keep scanning for the separator that actually ends the
                # header. The header can never be legitimately empty (every skill's first word
                # is required), so a `---` this early can only be noise, never the real marker.
                continue
            body = "".join(lines[index + 1 :]).removesuffix("\n")
            break
        if line.strip():
            seen_header_content = True
        header_lines.append(line)
    header_text = "".join(header_lines)
    try:
        header = shlex.split(header_text, comments=False, posix=True)
    except ValueError:
        # A contraction/possessive apostrophe inside a quoted phrase broke the quoting (T2 shape
        # i). Neutralize every letter-flanked `'` and retry before giving up.
        protected = _protect_contraction_apostrophes(header_text)
        try:
            header = _restore_contraction_apostrophes(
                shlex.split(protected, comments=False, posix=True)
            )
        except ValueError as exc:
            fixed = _protect_contraction_apostrophes(header_text).replace(
                _APOSTROPHE_SENTINEL, "\\'"
            )
            raise ArgsError(
                "the header cannot be split into shell words: an apostrophe inside a quoted "
                f"phrase still breaks the quoting: {exc}\nFIX: quote the header as:\n{fixed}"
            ) from exc
    return Parsed(header, body)


def _take(header: list[str], flag: str) -> tuple[list[str], str | None]:
    """Remove ``flag VALUE`` (or ``flag=VALUE``) from the header; return the rest and the value."""
    rest: list[str] = []
    value: str | None = None
    tokens = iter(header)
    for token in tokens:
        if token == flag:
            value = next(tokens, None)
            if value is None:
                raise ArgsError(f"{flag} needs a value")
        elif token.startswith(flag + "="):
            value = token[len(flag) + 1 :]
        else:
            rest.append(token)
    return rest, value


def _has(header: list[str], flag: str) -> bool:
    return any(token == flag or token.startswith(flag + "=") for token in header)


def _degrade_leading_dashes(word: str, keywords: tuple[str, ...]) -> str:
    """A common mistake: the type/action word is written as if it were a flag, with a stray
    leading ``-``/``--``/``---`` (``--CLAIM``, ``---msg``). If stripping those dashes yields a
    known keyword, strip them so the grammar sees the plain word it always required. Anything
    else is returned unchanged and still fails its own "first word must be one of" check below —
    this never widens what a valid first word is, it only repairs one specific misspelling of one
    before the check runs."""
    stripped = word.lstrip("-")
    if stripped != word and stripped.lower() in keywords:
        return stripped
    return word


def _split_ruling_kind(header: list[str]) -> tuple[str, list[str]] | None:
    """The operator-ruling kind and the header without it, or None when the header names none.

    The kind is the leading word. ``--kind <k>`` is accepted as an alias for it (OMN-17427: three
    callers wrote ``--kind consent`` and were refused), but only for a value that is a record kind:
    ``ruling --kind decision`` is the ruling's own process/decision flag and passes through.
    """
    first = _degrade_leading_dashes(header[0], RULING_KINDS).lower()
    if first in RULING_KINDS:
        rest = header[1:]
        alias = None
    else:
        if not header[0].startswith("-"):
            return None  # a stray first word is not a flag; the alias never hides it
        rest = list(header)
        alias = "--kind"
    rest, value = _take(rest, "--kind")
    if alias:
        if value is None or value.lower() not in RULING_KINDS:
            return None
        return value.lower(), rest
    if value is not None:
        if value.lower() not in RULING_KINDS:
            rest = [*rest, "--kind", value]  # the ruling's own --kind process|decision
        elif value.lower() != first:
            raise ArgsError(
                f"{first!r} and --kind {value} name two different kinds; keep one"
            )
    return first, rest


class _FactsNeededError(Exception):
    """Suspend a plan until the caller supplies a missing fact."""

    def __init__(self, result: ModelSkillExecResult) -> None:
        super().__init__(result.status)
        self.result = result


def _body_file(
    args_file: PurePath, body: str, suffix: str, writes: dict[str, str]
) -> str:
    path = str(args_file.with_name(args_file.name + suffix))
    writes[path] = body
    return path


def _reject_pipe_body(body: str) -> None:
    """A ledger row is one pipe-delimited line, so a literal ``|`` in ordinary free text would
    corrupt it on read-back; onex-ledger's own cell guard already refuses this (``_cell`` in
    ``omnibase_internal.ledger.verbs``) and that guard is unchanged and un-widened by this check.
    This only catches it here, before the round trip to that subprocess, so the refusal names the
    exact fix instead of onex-ledger's generic "correct the arguments and run the command again"."""
    if "|" in body:
        raise ArgsError(
            "the free text after --- may not contain a pipe (|): a ledger row is one "
            "pipe-delimited line, and a literal | would corrupt it on read-back. Replace | with "
            "';' or another separator, or drop it, and resubmit."
        )


def _dry_run_guard(env: dict[str, str], exists: dict[str, bool]) -> str | None:
    if not env.get("ONEX_DRY_RUN"):
        return None
    home = env.get("OMNI_HOME", "")
    if home:
        path = str(PurePath(home) / ".dryrun-sandbox")
        if path not in exists:
            raise _FactsNeededError(
                ModelSkillExecResult(status="need", need_exists=[path])
            )
        if exists[path]:
            return None
    return (
        "REFUSED 2 dry-run-guard: ONEX_DRY_RUN set outside a sandbox registry root\n"
        "FIX: set OMNI_HOME to a sandbox with .dryrun-sandbox, or unset ONEX_DRY_RUN"
    )


def build_command(
    skill: str,
    parsed: Parsed,
    request: ModelSkillExecRequest,
    env: dict[str, str],
    writes: dict[str, str],
) -> list[str]:
    """The one command the skill runs for this argument text. Raises ArgsError."""
    args_file = PurePath(request.args_file)
    skills = PurePath(request.plugin_dir) / "skills"
    ledger_cli = PurePath(request.plugin_dir) / "scripts" / "onex_ledger.py"
    header = list(parsed.header)
    body = parsed.body
    if not header:
        raise ArgsError("no arguments before the --- line")

    if skill == "operator-ruling":
        found = _split_ruling_kind(header)
        if found is None:
            raise ArgsError(
                f"first word must be one of {', '.join(RULING_KINDS)} (or pass --kind <one of them>), "
                f"got {header[0]!r}"
            )
        kind, rest = found
        if body is not None:
            if kind not in ("ruling", "consent"):
                raise ArgsError(
                    f"{kind} takes no --- body; only ruling and consent carry words"
                )
            if _has(rest, "--words-file"):
                raise ArgsError(
                    "pass the words after --- or with --words-file, not both"
                )
            if body == "":
                raise ArgsError("the operator's words after --- are empty")
            rest += ["--words-file", _body_file(args_file, body, ".words", writes)]
        elif kind in ("ruling", "consent") and not _has(rest, "--words-file"):
            raise ArgsError(
                f"{kind} needs the operator's words, verbatim, after a line holding ---"
            )
        return [
            "bash",
            str(skills / "operator-ruling" / "scripts" / "record.sh"),
            kind,
            *rest,
        ]

    if skill == "friction-record":
        rest = header[1:] if header[0] == "record" else header
        if body is not None:
            if _has(rest, "--text") or _has(rest, "--text-file"):
                raise ArgsError("pass the text after --- or with --text, not both")
            if body == "":
                raise ArgsError("the friction text after --- is empty")
            _reject_pipe_body(body)
            rest += ["--text-file", _body_file(args_file, body, ".text", writes)]
        return [
            "bash",
            str(skills / "friction-record" / "scripts" / "record.sh"),
            *rest,
        ]

    if skill in ("ledger-write", "ledger-msg"):
        allowed = (
            MSG_KINDS if skill == "ledger-msg" else tuple(t.lower() for t in ROW_TYPES)
        )
        extra_keyword = "inbox" if skill == "ledger-msg" else "append-rows"
        action = _degrade_leading_dashes(header[0], (*allowed, extra_keyword))
        rest = header[1:]
        if skill == "ledger-msg" and action.lower() == "inbox":
            if body is not None:
                raise ArgsError("inbox takes no --- body")
            if not rest:
                raise ArgsError("inbox needs the lane (or a comma list of lanes)")
            return [request.python, str(ledger_cli), "inbox", *rest]
        if skill == "ledger-write" and action.lower() == "append-rows":
            if rest:
                raise ArgsError("append-rows takes no flags; put the rows after ---")
            if not body:
                raise ArgsError("append-rows needs the rows after a line holding ---")
            return [
                request.python,
                str(ledger_cli),
                "append-rows",
                _body_file(args_file, body, ".rows", writes),
            ]
        kind = action.upper()
        if kind.lower() not in allowed:
            raise ArgsError(
                f"first word must be one of {', '.join(a.upper() for a in allowed)}, got {action!r}"
            )
        if (
            kind in ATTRIBUTED_ROW_TYPES
            and not _has(rest, "--actor")
            and not _has(rest, "--model")
        ):
            lane_actor = env.get("ONEX_LANE_ACTOR", "").strip()
            lane_model = env.get("ONEX_LANE_MODEL", "").strip()
            if lane_actor and lane_model:
                rest = [*rest, "--actor", lane_actor, "--model", lane_model]
            else:
                raise ArgsError(
                    f"a {kind} row needs --actor and/or --model (who and what wrote it); neither "
                    "was passed and ONEX_LANE_ACTOR/ONEX_LANE_MODEL are not both set in the "
                    "environment\nFIX: pass --actor <actor> --model <model> in the header, or set "
                    "both ONEX_LANE_ACTOR and ONEX_LANE_MODEL before invoking this skill"
                )
        if kind in ("RULING", "OPERATOR-CONSENT"):
            raise ArgsError(
                f"a {kind} row goes through /omni:operator-ruling, which keeps the words verbatim "
                "and adds the OUT OF SCOPE floor"
            )
        # `--parent`/`--consent` are not onex-ledger row flags (verbs.py's row parser defines
        # neither), so passed as flags they are rejected downstream as "unrecognized arguments".
        # The established convention (rule 18's consent citation, the lane-brief CLAIM template)
        # is that both are free-text tokens inside the row's own body. Lift them there instead of
        # refusing, so the common mistake of passing them as flags simply works.
        rest, parent_value = _take(rest, "--parent")
        rest, consent_value = _take(rest, "--consent")
        lifted = [
            f"{name}={value}"
            for name, value in (("parent", parent_value), ("consent", consent_value))
            if value is not None
        ]
        if body is not None:
            if _has(rest, "--text") or _has(rest, "--text-file"):
                raise ArgsError("pass the text after --- or with --text, not both")
            text = " ".join([*lifted, body]) if body else " ".join(lifted)
        elif lifted:
            # Preserve header-fed prose when lifting metadata: two --text-file flags would
            # silently replace the caller's file, and --text plus --text-file is refused.
            rest, header_text = _take(rest, "--text")
            rest, text_file = _take(rest, "--text-file")
            if header_text is not None and text_file is not None:
                raise ArgsError(
                    "pass --text or --text-file, not both; keep one text source"
                )
            if text_file is not None:
                if text_file not in request.texts:
                    raise _FactsNeededError(
                        ModelSkillExecResult(status="need", need_texts=[text_file])
                    )
                header_text = request.texts[text_file]
                if header_text is None:
                    raise ArgsError(
                        "--text-file could not be read as UTF-8; pass a readable UTF-8 file "
                        "or put the text after ---"
                    )
                header_text = header_text.removesuffix("\n")
            text = " ".join([*lifted, header_text]) if header_text else " ".join(lifted)
        else:
            text = None
        if text:
            _reject_pipe_body(text)
            rest += ["--text-file", _body_file(args_file, text, ".text", writes)]
        return [request.python, str(ledger_cli), "row", kind, *rest]

    raise ArgsError(f"unknown skill {skill!r}; one of {', '.join(USAGE)}")


# A refusal that changes nothing about the caller's own arguments (production, staging and
# credential scopes, and the budget/lock classes) gets no example: a "corrected" copy of a
# request that policy refuses would only invite the same request again.
_NO_EXAMPLE = re.compile(
    r"names (production|staging in an away|a credential)|^(REFUSED|RETRY) 124 ", re.M
)
_OPEN_UNNAMED = re.compile(r"open ruling\(s\) (\S+) on ")
_EXAMPLE_CODES = (2, 3, 64, 65)


def _guess_ruling_kind(header: list[str]) -> str:
    if _has(header, "--approved") or _has(header, "--out-of-scope"):
        return "consent"
    if _has(header, "--question"):
        return "ask"
    if any(
        _has(header, f)
        for f in ("--to", "--repo", "--pr", "--until", "--after", "--proof")
    ):
        return "hold"
    if _has(header, "--re") and not _has(header, "--ticket"):
        return "release"
    return "ruling"


def corrected_example(
    skill: str,
    header: list[str],
    body: str | None,
    env: dict[str, str],
    output: str = "",
) -> str | None:
    """One invocation shaped as the skill takes it, built from the caller's own arguments.

    Only operator-ruling has one, because its refusals were the recurring ones (OMN-17427: the kind
    given as ``--kind``, the scope lists missing from the header, ``--amends`` on a consent). The
    caller's tokens are kept as they wrote them, moved into the accepted shape, and every flag the
    kind needs that they left out is added as a quoted ``<placeholder>``. The operator's words are
    never copied back: a placeholder line stands in for them.
    """
    if skill != "operator-ruling" or not header or _NO_EXAMPLE.search(output):
        return None
    try:
        found = _split_ruling_kind(header)
    except ArgsError:
        found = None
    if found is None:
        kind = _guess_ruling_kind(header)
        rest = (
            [t for t in header if t != header[0]]
            if not header[0].startswith("-")
            else list(header)
        )
    else:
        kind, rest = found
    add: list[str] = []
    if not _has(rest, "--lane") and not env.get("SESSION_LANE"):
        add += ["--lane", "<lane>"]
    if kind == "ask" and not _has(rest, "--question"):
        add += ["--question", "<one plain question>"]
    if kind == "ruling":
        if not _has(rest, "--question"):
            add += [
                "--question",
                "<the question or options the operator's words answer>",
            ]
        if _has(rest, "--relay-to") and not _has(rest, "--relay-text"):
            add += ["--relay-text", "<the binding instruction for the addressed lanes>"]
        if _has(rest, "--relay-text") and not _has(rest, "--relay-to"):
            add += ["--relay-to", "<lane or all>"]
        if not any(
            _has(rest, f) for f in ("--mechanism", "--mechanism-ticket", "--kind")
        ):
            add += ["--kind", "decision"]
        unnamed = _OPEN_UNNAMED.search(output)
        for stamp in unnamed.group(1).split(",") if unnamed else []:
            add += ["--amends", stamp]
    if kind == "consent":
        if not _has(rest, "--approved"):
            add += ["--approved", "<what is allowed; each item after a semicolon>"]
        if not _has(rest, "--out-of-scope"):
            add += [
                "--out-of-scope",
                "<what is not allowed; each item after a semicolon>",
            ]
    if kind == "hold":
        if not any(_has(rest, f) for f in ("--to", "--repo", "--pr")):
            add += ["--to", "<lane|all>"]
        if not any(_has(rest, f) for f in ("--until", "--after", "--proof")):
            add += ["--until", "<UTC stamp>"]
    if kind == "release" and not _has(rest, "--re"):
        add += ["--re", "<hold id>"]
    line = shlex.join([kind, *rest, *add])
    if kind in ("ruling", "consent") and not _has(rest, "--words-file"):
        line += "\n---\n" + (
            "<the same words, unchanged>"
            if body
            else "<the operator's words, verbatim>"
        )
    note = ""
    if kind == "ruling" and _OPEN_UNNAMED.search(output):
        note = "\nper stamp: --amends if the ruling changes it, --supersedes if it replaces it, --unrelated if it is a different matter"
    return f"EXAMPLE (your own arguments in the accepted shape; replace each <placeholder>):\n{line}{note}"


def _split_trailer(output: str) -> tuple[str, str]:
    """The result and refusal lines, and the STEP/TIMING trailer that closes them."""
    lines = output.splitlines()
    cut = next(
        (i for i, line in enumerate(lines) if line.startswith(("STEP ", "TIMING "))),
        len(lines),
    )
    return "\n".join(lines[:cut]), "\n".join(lines[cut:])


def _budget(env: dict[str, str]) -> float:
    try:
        budget = float(env.get("OMNI_SKILL_EXEC_BUDGET_S", "180"))
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError
    except ValueError:
        budget = 180.0
    return budget


def _run_output(request: ModelSkillExecRequest) -> tuple[int, str]:
    budget = _budget(request.env)
    command = request.command
    stdout, stderr = request.stdout, request.stderr
    timed_out = request.timed_out
    code = 124 if timed_out else request.returncode

    progress = []
    diagnostics = []
    # Never suppress stderr (rule 16): a script that wrote to it said something.
    for line in stderr.splitlines():
        if line.startswith(("STEP ", "TIMING ")):
            progress.append(line + "\n")
        else:
            diagnostics.append(f"STDERR: {line}\n")
    # The result line stays first, as every caller and test has always read it; the STEP and
    # TIMING trailer (which step ran, and for how long) follows the result and any refusal.
    output = stdout
    if diagnostics:
        if output and not output.endswith("\n"):
            output += "\n"
        output += "".join(diagnostics)
    if timed_out:
        script = PurePath(command[1] if len(command) > 1 else command[0]).name
        if output and not output.endswith("\n"):
            output += "\n"
        output += (
            f"REFUSED 124 budget: {script} exceeded {budget:g}s; "
            "nothing after the last OK line was written\n"
            "FIX: read the detail file named above, then rerun once the host load drops\n"
        )
    if progress:
        if output and not output.endswith("\n"):
            output += "\n"
        output += "".join(progress)
    return code, output


def cite_as(output: str) -> str | None:
    """How a later row cites what was appended, from the result line."""
    for line in output.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "OK" and len(tokens) >= 3:
            kind, stamp, fields = tokens[1], tokens[2], tokens[3:]
        elif (
            tokens[0] in ("RULING", "CONSENT", "HOLD", "RELEASE", "ASK", "FRICTION")
            and len(tokens) >= 2
        ):
            kind, stamp, fields = tokens[0], tokens[1], tokens[2:]
        else:
            continue
        if not STAMP_RE.match(stamp):
            continue
        values = dict(field.split("=", 1) for field in fields if "=" in field)
        kind = {"CONSENT": "OPERATOR-CONSENT", "ASK": "MSG"}.get(kind, kind)
        if "id" in values:
            return f"{kind} id={values['id']}"
        lane = values.get("lane") or values.get("from")
        cite = f"{kind} {stamp}" + (f" lane={lane}" if lane else "")
        if kind == "OPERATOR-CONSENT" and "cite" in values:
            cite = f"{values['cite']} ({cite})"
        return cite
    return None


_MISSING_CELL = re.compile(r"\bcarries no (\w+)=? cell\b")


def missing_cell(output: str) -> str | None:
    """The cell a refusal says the row lacks (``worktree``, ``delegated``), from its own sentence."""
    for line in output.splitlines():
        if line.startswith("REFUSED "):
            found = _MISSING_CELL.search(line)
            if found:
                return found.group(1)
    return None


def _args_refusal(
    skill: str,
    error: ArgsError,
    header: list[str],
    body: str | None,
    env: dict[str, str],
    writes: dict[str, str],
    env_updates: dict[str, str],
) -> ModelSkillExecResult:
    usage = USAGE.get(skill, "")
    example = corrected_example(skill, header, body, env)
    return ModelSkillExecResult(
        status="refused",
        exit_code=64,
        output=(
            f"REFUSED 64 args: {error}\n"
            + (f"{example}\n" if example else "")
            + f"FIX: invoke /omni:{skill} with arguments shaped as:\n{usage}\n"
        ),
        write_files=writes,
        env_updates=env_updates,
    )


def _plan(request: ModelSkillExecRequest) -> ModelSkillExecResult:
    if request.argument_text is None:
        return ModelSkillExecResult(
            status="refused",
            exit_code=64,
            output=(
                f"REFUSED 64 args: cannot read {request.args_file}: {request.read_error}\n"
                "FIX: write the argument text to that file first\n"
            ),
        )
    env = dict(request.env)
    header: list[str] = []
    body: str | None = None
    writes: dict[str, str] = {}
    env_updates: dict[str, str] = {}
    try:
        parsed = parse_arguments(request.argument_text)
        body = parsed.body
        # Assign separately: a failed second take must report the first take's header.
        header, ledger = _take(parsed.header, "--ledger")
        header, session = _take(header, "--session-env")
        if session:
            if request.session_env_error is not None:
                raise ArgsError(request.session_env_error)
            if request.session_env is None:
                return ModelSkillExecResult(status="need", need_session_env=session)
            env.update(request.session_env)
            env_updates.update(request.session_env)
        if ledger:
            env["ONEX_LEDGER_PATH"] = ledger
            env_updates["ONEX_LEDGER_PATH"] = ledger
        command = build_command(
            request.skill, Parsed(header, body), request, env, writes
        )
        refused = _dry_run_guard(env, request.exists)
    except ArgsError as exc:
        return _args_refusal(request.skill, exc, header, body, env, writes, env_updates)
    except _FactsNeededError as exc:
        return exc.result
    if refused:
        return ModelSkillExecResult(
            status="refused",
            exit_code=2,
            output=refused + "\n",
            write_files=writes,
            env_updates=env_updates,
        )
    return ModelSkillExecResult(
        status="command",
        command=command,
        write_files=writes,
        env_updates=env_updates,
        budget_s=_budget(env),
    )


def _report(request: ModelSkillExecRequest) -> ModelSkillExecResult:
    header: list[str] = []
    body: str | None = None
    try:
        parsed = parse_arguments(request.argument_text or "")
        body = parsed.body
        header, _ = _take(parsed.header, "--ledger")
        header, _ = _take(header, "--session-env")
    except ArgsError:
        # A report normally follows a successful plan; retain any parsed context.
        pass
    code, output = _run_output(request)
    head, trailer = _split_trailer(output)
    lines = [head] if head.strip() else []
    if code == 0:
        cite = cite_as(output)
        if cite:
            lines.append(f"CITE-AS: {cite}")
    elif code in _EXAMPLE_CODES:
        example = corrected_example(request.skill, header, body, request.env, output)
        if example:
            lines.append(example)
        cell = missing_cell(output)
        if cell:
            lines.append(f"MISSING-CELL: {cell}")
    if trailer:
        lines.append(trailer)
    lines.append(f"TIMING skill_exec total={request.elapsed_s:.1f}s")
    return ModelSkillExecResult(
        status="done", exit_code=code, output="\n".join(lines) + "\n"
    )


def _usage(skills: list[str]) -> ModelSkillExecResult:
    output = ""
    for skill in skills or list(USAGE):
        if skill not in USAGE:
            return ModelSkillExecResult(
                status="usage",
                exit_code=64,
                output=output,
                stderr=f"unknown skill {skill!r}; one of {', '.join(USAGE)}\n",
            )
        output += f"/omni:{skill} arguments:\n{USAGE[skill]}\n\n"
    return ModelSkillExecResult(status="usage", exit_code=0, output=output)


class HandlerSkillExecDecision:
    """Stateless compute: plan and report the skill execution from caller-supplied facts."""

    def handle(self, request: ModelSkillExecRequest) -> ModelSkillExecResult:
        if request.phase == "usage":
            return _usage(request.usage_skills)
        if request.phase == "report":
            return _report(request)
        return _plan(request)
