# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify what a DoD evidence check binds when it passes (OMN-15911).

Pure logic — no I/O, no env reads, no subprocess. Given the check mapping a
contract declares, decide whether passing it proves BEHAVIOR, merely binds
MERGE_STATE, stands in as a SURROGATE, or is INDETERMINATE.

Design rules, in priority order:

1. **Read the command, not the prose.** ``check_type`` and ``description`` are
   author-supplied and, per OMN-15391, are allowed to narrate a proof the
   command cannot produce. The single exception is ``check_type:
   file_exists``, which is not a command at all and is definitionally
   static-artifact inspection.
2. **Fail closed.** Anything unrecognized is INDETERMINATE, never BEHAVIOR.
   The consuming flip rule requires at least one behavior-proving check, so a
   misclassification toward BEHAVIOR releases a flip and a misclassification
   away from it merely holds one. Only the second direction is acceptable.
3. **The BEHAVIOR allowlist is tight and positive.** A command earns BEHAVIOR
   by naming a known test runner or the ONEX CLI, not by failing to look like
   anything else.
4. **The surrogate corpus lives in one place.** OMN-15391's
   ``omnimarket.occ_evidence_probative_class`` landed first and owns
   ``FOREIGN_SUITE_DENYLIST`` plus the bare-``gh pr view`` predicate; this
   module calls ``is_surrogate_check_value`` rather than keeping a second list
   that would drift out of step with it.
5. **Transparent prefixes resolve through, and the set is closed** (OMN-18135).
   A prefix is transparent when it can change WHERE, or WITH WHAT ENVIRONMENT,
   a command runs and can never change whether its exit status depends on the
   product diff. Exactly three qualify, and a contract author can rely on
   these:

   * a bare ``NAME=value`` assignment,
   * ``env``, including its options — ``-i``, ``-0``, ``--null``,
     ``--ignore-environment``, ``-u NAME`` / ``--unset=NAME``, ``-C DIR`` /
     ``--chdir=DIR``,
   * ``cd <dir>`` as a whole pipeline segment.

   Anything else still fails closed. ``bash -c "<command>"`` is NOT
   transparent: the command lives inside a quoted string this module does not
   parse, and guessing at it would be the one direction rule 2 forbids.

   **What this does not change**: a check that reaches a live surface through
   ``ssh``, ``aws``, ``kubectl``, ``curl`` or a hand-written shell loop is
   still not behaviour-proving, because its HEAD is not a runner. The ``onex``
   CLI is the one live path that is. Transparency resolves a prefix; it never
   widens the runner allowlist.

6. **A wrapper's own options belong to the wrapper** (OMN-18612). The words
   in ``_WRAPPER_WORDS`` were always skipped; their OPTIONS were not, so a
   token that an option consumes was returned as the command head. A
   per-wrapper table now says which options are self-contained and which
   consume the next token, and the two spellings ``--name value`` and
   ``--name=value`` resolve identically. The table is closed in both
   directions: a grammar applies only after its own wrapper word has been
   seen, and an option it does not list still ends the walk. Skipping an
   unlisted option would be the forbidden direction — ``uv run --with pytest
   ruff check src/`` would resolve its head to an INSTALLED PACKAGE name and
   call a lint run a behaviour proof.

   **A note for contract authors, because two gates look like they conflict
   and do not**: receipt hardening refuses a receipt whose ``commit_sha``
   names no repo, and asks for a ``repos/<owner>/<repo>/...`` reference. The
   ``gh api repos/<owner>/<repo>/commits/<sha> --jq .sha && <runner>`` form
   satisfies it and keeps its BEHAVIOR class, because the ``gh api`` segment
   reads merge state and the walk continues past it. Before OMN-18135 a
   ``cd``-prefixed form did NOT, which is what made the two gates look
   mutually exclusive; they never were.

Relationship to OMN-15391, stated precisely because the two look alike: that
module asks whether a command's exit status *can* depend on the product change
(vacuity), this one asks what a check that *passed* actually bound (proof
strength). Neither subsumes the other. The case that separates them is the one
OMN-15391 records as deliberately out of its scope — an asserted merge probe,
``gh pr view <n> --json state --jq '.state' | grep -q MERGED``, is probative
there (it can go red) and is MERGE_STATE here (it proves a merge, not a
behavior).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.occ_evidence_probative_class import is_surrogate_check_value

# Roll-up order for a multi-check evidence item: an item is VERIFIED only when
# EVERY one of its checks passed, so the strongest class among them is an
# honest label for what the item proved. MERGE_STATE outranks SURROGATE
# because it is at least bound to this ticket's own PR.
# OMN-18135 AC4: READBACK sits below MERGE_STATE and above SURROGATE. It is
# bound to THIS ticket's live surface, which a generic surrogate is not; and
# it is weaker than a merge-state read, which is at least a fact about this
# ticket's own PR. It can never outrank BEHAVIOR.
CHECK_PROOF_CLASS_PRECEDENCE: Final[tuple[EnumCheckProofClass, ...]] = (
    EnumCheckProofClass.BEHAVIOR,
    EnumCheckProofClass.MERGE_STATE,
    EnumCheckProofClass.READBACK,
    EnumCheckProofClass.SURROGATE,
    EnumCheckProofClass.INDETERMINATE,
)

# Test runners and product CLIs whose exit code is a statement about behavior.
_BEHAVIOR_WORDS: Final[frozenset[str]] = frozenset(
    {
        "pytest",
        "py.test",
        "tox",
        "nox",
        "unittest",
        # The ONEX CLI: running a node/skill executes the product itself.
        "onex",
        "vitest",
        "jest",
        "mocha",
        "rspec",
        "phpunit",
    }
)

# Runners that only mean "test" when paired with a test subcommand/target.
_BEHAVIOR_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("go", "test"),
    ("cargo", "test"),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("bun", "test"),
    ("dotnet", "test"),
    ("gradle", "test"),
    ("mvn", "test"),
)

# `make <target>` is behavior only when the target names a test.
_MAKE_TEST_TARGET_RE: Final[re.Pattern[str]] = re.compile(r"(^|[-_.])test")

# Wrapper words to skip when finding a segment's real command head.
_WRAPPER_WORDS: Final[frozenset[str]] = frozenset(
    {
        "uv",
        "run",
        "poetry",
        "pipenv",
        "hatch",
        "pdm",
        "env",
        "sudo",
        "nice",
        "ionice",
        "time",
        "command",
        "exec",
        "--",
    }
)

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*=",
)

# OMN-18135, generalised to a table by OMN-18612. A wrapper's OWN options
# belong to the wrapper, not to the command it wraps, and some of them consume
# the following token. Without that grammar the walk hands back an option --
# or an option's VALUE -- as the command head.
#
# OMN-18135 measured the first surface: `env PYTHONPATH=/x uv run pytest ...`
# classified BEHAVIOR while `env -u PYTHONPATH uv run pytest ...` classified
# INDETERMINATE, because the loop skipped `env` and then returned `-u` as the
# head. It fixed that with three `env`-shaped constants and a `saw_env` flag.
#
# OMN-18612 measured the same defect one wrapper over, on OMN-17276's own
# behaviour proof, against this module at origin/dev:
#
#   uv run pytest <file> -q                            -> BEHAVIOR
#   uv run --with-requirements <req> pytest <file> -q  -> INDETERMINATE
#
# The second spelling exists BECAUSE the first could not execute at its
# declared cwd -- the root project there deliberately omits a driver the
# service needs -- so the item was repaired to install the service's own
# requirements files. The repair made the proof runnable and, in the same
# stroke, made it uncountable: `behavior_proving_count` stayed 0 on a check
# that runs 27 tests green. No contract should have to spell its command a
# particular way to be counted.
#
# Two rules keep the table fail-closed, and they are the whole of the design.
#
# 1. A grammar is consulted only AFTER its own wrapper word has been seen. A
#    bare `-u` or `--with` at the head of some other command is still
#    unrecognised and still ends the walk.
# 2. An option absent from its wrapper's grammar is NOT skipped. Skipping
#    unknown options would be the one direction rule 2 of this module's
#    docstring forbids: `uv run --with pytest ruff check src/` would resolve
#    its head to `pytest` -- the name of an INSTALLED PACKAGE, not of the
#    program being run -- and manufacture a BEHAVIOR out of a lint run.
#
# A wrapper with no entry below is unchanged: its options are unrecognised and
# still end the walk, which is the fail-closed answer, not an oversight.


@dataclass(frozen=True)
class _WrapperOptionGrammar:
    """One wrapper's own options, split by whether they consume a value.

    ``flags`` are self-contained. ``flags_taking_a_value`` consume the
    following token, and a long one may instead carry its value attached as
    ``--name=value``; the two spellings are the same option and must classify
    identically (OMN-18612 AC2), which is why the attached form is derived
    here rather than enumerated as a second constant.
    """

    flags: frozenset[str] = frozenset()
    flags_taking_a_value: frozenset[str] = frozenset()


_WRAPPER_OPTION_GRAMMARS: Final[Mapping[str, _WrapperOptionGrammar]] = {
    # coreutils env. The OMN-18135 set, unchanged in meaning; the long forms
    # gain their separated spelling (`--unset NAME`) for free, which GNU
    # accepts and the hand-written regex did not.
    "env": _WrapperOptionGrammar(
        flags=frozenset({"-i", "-0", "--null", "--ignore-environment"}),
        flags_taking_a_value=frozenset({"-u", "--unset", "-C", "--chdir"}),
    ),
    # `uv run`. The value-taking set is the reason this ticket exists; the
    # boolean set must be accurate for the same reason rule 2 above states,
    # since a value-taking option misfiled as boolean is exactly the error
    # that can promote a lint run to BEHAVIOR.
    "uv": _WrapperOptionGrammar(
        flags=frozenset(
            {
                "--isolated",
                "--frozen",
                "--locked",
                "--no-sync",
                "--no-project",
                "--no-dev",
                "--only-dev",
                "--all-extras",
                "--no-editable",
                "--exact",
                "--offline",
                "--refresh",
                "--native-tls",
                "--no-config",
                "--preview",
                "-q",
                "--quiet",
                "-v",
                "--verbose",
            }
        ),
        flags_taking_a_value=frozenset(
            {
                "--with",
                "--with-requirements",
                "--with-editable",
                "--python",
                "-p",
                "--project",
                "--directory",
                "--env-file",
                "--extra",
                "--group",
                "--package",
                "--index",
                "--index-url",
                "--extra-index-url",
                "--find-links",
                "--constraint",
                "--override",
                "--refresh-package",
                "--no-binary-package",
                "--only-binary-package",
                "--python-preference",
                "--resolution",
                "--prerelease",
                "--color",
                "--cache-dir",
                "--config-file",
            }
        ),
    ),
    "poetry": _WrapperOptionGrammar(
        flags=frozenset(
            {"-n", "--no-interaction", "--no-plugins", "--no-ansi", "-q", "--quiet"}
        ),
        flags_taking_a_value=frozenset({"-C", "--directory", "--project"}),
    ),
    "nice": _WrapperOptionGrammar(
        flags_taking_a_value=frozenset({"-n", "--adjustment"}),
    ),
    "ionice": _WrapperOptionGrammar(
        flags=frozenset({"-t", "--ignore"}),
        flags_taking_a_value=frozenset(
            {"-c", "--class", "-n", "--classdata", "-p", "--pid"}
        ),
    ),
    "sudo": _WrapperOptionGrammar(
        flags=frozenset(
            {"-E", "--preserve-env", "-H", "-n", "--non-interactive", "-S", "--stdin"}
        ),
        flags_taking_a_value=frozenset(
            {"-u", "--user", "-g", "--group", "-D", "--chdir", "-R", "--chroot"}
        ),
    ),
    "time": _WrapperOptionGrammar(
        flags=frozenset({"-p", "--portability", "-v", "--verbose", "-a", "--append"}),
        flags_taking_a_value=frozenset({"-f", "--format", "-o", "--output"}),
    ),
}

# OMN-18135. Heads that change WHERE a command runs and can never change
# whether its exit status depends on the product diff. A segment consisting
# only of one of these proves nothing and refutes nothing, so it is skipped
# rather than read as an unrecognised command that ends the scan.
_TRANSPARENT_SEGMENT_HEADS: Final[frozenset[str]] = frozenset({"cd"})

# Heads whose reads bind PR / merge / repo state.
_GH_MERGE_STATE_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"pr", "run", "release", "search"}
)
_GIT_MERGE_STATE_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "log",
        "rev-parse",
        "rev-list",
        "merge-base",
        "show",
        "ls-remote",
        "ls-files",
        "branch",
        "tag",
        "describe",
        "diff",
        "cat-file",
        "status",
    }
)

# Heads that only ever inspect a static artifact.
_STATIC_INSPECTION_HEADS: Final[frozenset[str]] = frozenset(
    {
        "test",
        "[",
        "ls",
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "jq",
        "yq",
        "wc",
        "head",
        "tail",
        "find",
        "stat",
        "file",
        "sha256sum",
        "shasum",
        "md5sum",
        "diff",
        "cmp",
        "awk",
        "sed",
        "tr",
        "sort",
        "uniq",
        "cut",
        "basename",
        "dirname",
        "readlink",
        "realpath",
        "echo",
        "printf",
        "true",
        ":",
    }
)

# Which commands are surrogates is NOT decided here. OMN-15391 landed
# ``omnimarket.occ_evidence_probative_class`` first, and that module is the
# single definition of the surrogate corpus: ``FOREIGN_SUITE_DENYLIST`` (the
# ratcheted list of ticket-independent generic suites) and the bare-``gh pr
# view`` predicate. This module DELEGATES to it rather than keeping a second
# list that would drift.
#
# The two modules answer different questions and neither subsumes the other:
#
#   OMN-15391  "can this command's exit status depend on the product change?"
#              -> PROBATIVE / PR_STATE_SURROGATE / FOREIGN_SUITE_SURROGATE
#   OMN-15911  "what did this check BIND when it passed?"
#              -> behavior / merge-state / surrogate / indeterminate
#
# The residual OMN-15391 explicitly records as out of its scope is exactly what
# this axis catches: an ASSERTED merge probe
# (``gh pr view <n> --json state --jq '.state' | grep -q MERGED``) is probative
# by that module's definition — it can go red — and still proves only that a
# merge happened. It is MERGE_STATE here, and it can never release an autoclose
# flip.

# Shapes that discard a command's exit code, so the "proof" cannot fail.
_EXIT_CODE_LAUNDERING_RE: Final[re.Pattern[str]] = re.compile(
    r"(\|\||;|&&)\s*(true\b|:\s|:$|echo\b)|\|\s*true\b",
)

_SEGMENT_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\|\||&&|\||;|\n")


def _words(text: str) -> list[str]:
    """Shell-split ``text``, degrading to whitespace split on malformed input."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _normalize_head(word: str) -> str:
    """Reduce ``/usr/local/bin/pytest`` / ``python3.12`` to a comparable head."""
    head = word.rsplit("/", 1)[-1]
    if head.startswith("python"):
        return "python"
    return head


def _option_token_span(word: str, active: Sequence[_WrapperOptionGrammar]) -> int:
    """How many tokens ``word`` consumes as a wrapper option; 0 when it is none.

    ``--name=value`` is one token and ``--name value`` is two, which is what
    makes the two spellings of the same option classify identically
    (OMN-18612 AC2).

    Ambiguity resolves toward consuming the value, deliberately. ``-n`` is
    ``nice``'s adjustment and ``sudo``'s non-interactive flag, so a command
    wrapped in both is genuinely ambiguous to a table this shape. Reading it
    as value-taking over-consumes and swallows the runner, which fails closed
    to INDETERMINATE. Reading it as self-contained would hand back the
    option's VALUE as the command head, which is the one direction that can
    manufacture a BEHAVIOR.
    """
    if word.startswith("--") and "=" in word:
        name, attached = word.split("=", 1)[0], True
    else:
        name, attached = word, False
    if any(name in grammar.flags_taking_a_value for grammar in active):
        return 1 if attached else 2
    if any(word in grammar.flags for grammar in active):
        return 1
    return 0


def _segment_head_and_args(segment: str) -> tuple[str, list[str]]:
    """Return ``(head, args)`` for one pipeline segment, wrappers stripped."""
    words = _words(segment)
    index = 0
    active: list[_WrapperOptionGrammar] = []
    while index < len(words):
        word = words[index]
        if _ENV_ASSIGNMENT_RE.match(word):
            index += 1
            continue
        # OMN-18135, generalised by OMN-18612: a wrapper's options belong to
        # the wrapper, not to the command it wraps. Consulted only once that
        # wrapper's own word has been seen, so a bare `-u` or `--with` at the
        # head of some other command is still unrecognised and still fails
        # closed.
        if active:
            span = _option_token_span(word, active)
            if span:
                index += span
                continue
        normalized = _normalize_head(word)
        if normalized in _WRAPPER_WORDS:
            grammar = _WRAPPER_OPTION_GRAMMARS.get(normalized)
            if grammar is not None:
                active.append(grammar)
            index += 1
            continue
        return normalized, words[index + 1 :]
    return "", []


def _is_transparent_segment(segment: str) -> bool:
    """True when this segment only relocates the command that follows it.

    OMN-18135. ``cd <dir>`` is the whole of the set today. It is deliberately
    NOT treated as a static inspection: that would make a bare ``cd x`` a
    SURROGATE, which is a claim that something was inspected when nothing
    was. A segment that is transparent contributes to neither verdict, so a
    command made ENTIRELY of transparent segments still falls through to
    INDETERMINATE.
    """
    head, _args = _segment_head_and_args(segment)
    return head in _TRANSPARENT_SEGMENT_HEADS


def _python_module_head(args: Sequence[str]) -> str:
    """The module name in ``python -m <module>``, or an empty string."""
    for position, arg in enumerate(args):
        if arg == "-m" and position + 1 < len(args):
            return args[position + 1]
    return ""


def _is_test_target(arg: str, subcommand: str) -> bool:
    """True when ``arg`` invokes ``subcommand``, plainly or as a namespace.

    ``test:unit`` is the dominant npm convention and is the same execution as
    ``test``; ``test-nothing-here`` is not, and neither is ``testify``. The colon
    is what separates a namespaced script from a name that merely starts with the
    same letters, so this is deliberately not ``arg.startswith(subcommand)``.
    """
    return arg == subcommand or arg.startswith(f"{subcommand}:")


def _is_behavior(head: str, args: Sequence[str]) -> bool:
    """True when this segment executes a test runner or the ONEX CLI."""
    if head in _BEHAVIOR_WORDS:
        return True
    if head == "python":
        module = _python_module_head(args)
        return module.split(".", 1)[0] in _BEHAVIOR_WORDS or module in _BEHAVIOR_WORDS
    if head == "make":
        return any(
            _MAKE_TEST_TARGET_RE.search(arg) for arg in args if not arg.startswith("-")
        )
    for runner, subcommand in _BEHAVIOR_PAIRS:
        if head == runner and any(_is_test_target(arg, subcommand) for arg in args):
            return True
    return False


def _is_merge_state(head: str, args: Sequence[str]) -> bool:
    """True when this segment only reads PR / merge / repo state."""
    if head == "gh":
        if not args:
            return False
        if args[0] in _GH_MERGE_STATE_SUBCOMMANDS:
            return True
        if args[0] == "api":
            # A GET against the pulls/commits surface. Any explicit non-GET
            # method is a mutation and is not a state read.
            if "--method" in args or "-X" in args:
                return False
            return any(
                "/pulls" in arg or "/commits" in arg or "/branches" in arg
                for arg in args
            )
        return False
    if head == "git":
        if not args:
            return False
        if args[0] == "grep":
            return False
        return args[0] in _GIT_MERGE_STATE_SUBCOMMANDS
    return False


def _is_static_inspection(head: str) -> bool:
    return head in _STATIC_INSPECTION_HEADS


# -- OMN-18135 AC4: the asserted live readback -----------------------------
#
# Heads that read state from a RUNNING system rather than from the tree under
# test. `gh` is here as well as in the merge-state set because `gh api` also
# reads live content surfaces (`/contents`, `/actions`), which is what
# OMN-17771's checks do; the merge-state leg still claims `/pulls`,
# `/commits` and `/branches` first, so a PR probe keeps its own class.
_LIVE_READ_HEADS: Final[frozenset[str]] = frozenset(
    {
        "curl",
        "wget",
        "http",
        "httpie",
        "ssh",
        "kubectl",
        "oc",
        "aws",
        "gcloud",
        "az",
        "psql",
        "mysql",
        "mongosh",
        "redis-cli",
        "valkey-cli",
        "rpk",
        "kafka-topics",
        "kcadm",
        "kcadm.sh",
        "docker",
        "nc",
        "dig",
        "gh",
    }
)

# Tokens whose presence means the exit status can turn on what was READ. A
# bare read is green whatever the system says, which is the same vacuity
# OMN-15391 refuses elsewhere, so the assertion is the whole bar.
# Word-bounded on purpose. A naive substring match made `pytest tests/x.py`
# assert, because "pytest " ends in "test ", which turned every `ssh host
# uv run pytest ...` into a readback. Measured against this suite's own
# controls, which is what caught it.
_ASSERTION_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?x)
      \btest\b            # the test builtin, not the "test" inside pytest
    | (?<![\w-])\[\s      # the [ builtin
    | \bgrep\s+-[A-Za-z]*[qc]   # grep -q / -c: exit turns on the match
    | \bjq\s+-[A-Za-z]*e  # jq -e: exit turns on the filter's output
    | --fail
    | \bassert\b
    """,
)

# `curl -sf` / `-fsS` / `--fail`: the flag that makes curl's exit status
# follow the HTTP status instead of merely the transport.
_CURL_FAIL_FLAG_RE: Final[re.Pattern[str]] = re.compile(r"^-[A-Za-z]*f[A-Za-z]*$")


#: A live-read head anywhere in the command, including nested inside a
#: command substitution. Deliberately a TEXT scan and not a token walk:
#: `body="$(gh api ... )"` is the dominant shape in hand-written shell
#: evidence, and shlex hands that back as ONE word, so tokenizing finds
#: nothing. Measured on OMN-17771, where the token walk returned False on a
#: command that plainly calls `gh api` four times.
_LIVE_READ_IN_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[^\w./-])(" + "|".join(sorted(map(re.escape, _LIVE_READ_HEADS))) + r")\s",
)


def _reads_a_live_surface(text: str) -> bool:
    """True when the command reads state from a running system anywhere in it.

    Over-matching is possible -- the word `docker` inside a quoted string
    counts -- and is tolerated because the ASSERTION half still has to hold
    and because the only verdicts this can promote are INDETERMINATE and
    static-inspection SURROGATE. It cannot manufacture a BEHAVIOR.
    """
    return _LIVE_READ_IN_TEXT_RE.search(text) is not None


def _asserts_on_what_it_read(text: str, segments: Sequence[str]) -> bool:
    """True when the command's exit status can go red on the value read."""
    if _ASSERTION_RE.search(text) is not None:
        return True
    for segment in segments:
        head, args = _segment_head_and_args(segment)
        if head == "curl" and any(
            arg == "--fail" or _CURL_FAIL_FLAG_RE.match(arg) for arg in args
        ):
            return True
    return False


def _is_asserted_live_readback(text: str, segments: Sequence[str]) -> bool:
    """The OMN-18135 AC4 predicate: reads a running system AND asserts on it.

    Asked ONLY where the walk would otherwise return INDETERMINATE, which is
    what keeps this change monotone: it can promote indeterminate to readback
    and can never touch a BEHAVIOR, MERGE_STATE or SURROGATE verdict.

    Both halves are required and neither is sufficient. A read with no
    assertion is green whatever the system says. An assertion over the tree
    under test with no live read is static inspection, and the walk has
    already called it SURROGATE before this is reached.
    """
    return _reads_a_live_surface(text) and _asserts_on_what_it_read(text, segments)


def classify_command(command: str) -> EnumCheckProofClass:
    """Classify a shell command string. Pure; fails closed to INDETERMINATE."""
    text = command.strip()
    if not text:
        return EnumCheckProofClass.INDETERMINATE

    # OMN-15391's corpus first, and deliberately BEFORE the BEHAVIOR allowlist:
    # a denylisted foreign suite IS a real pytest run, so shape alone would
    # call it behavior. Delegating here keeps one definition of the surrogate
    # set across both lanes.
    if is_surrogate_check_value(text):
        return EnumCheckProofClass.SURROGATE

    # An exit code that cannot fail is not evidence of anything.
    if _EXIT_CODE_LAUNDERING_RE.search(text):
        return EnumCheckProofClass.INDETERMINATE

    segments = [
        segment.strip() for segment in _SEGMENT_SPLIT_RE.split(text) if segment.strip()
    ]
    if not segments:
        return EnumCheckProofClass.INDETERMINATE

    walked = _walk_verdict(segments)
    # OMN-18135 AC4, asked LAST and only over two verdicts.
    #
    # INDETERMINATE: the walk recognised nothing, so promoting it can demote
    # nothing.
    #
    # SURROGATE: only the STATIC-INSPECTION kind can reach here. OMN-15391's
    # corpus -- the foreign-suite denylist and the bare `gh pr view` probe --
    # is checked against the whole text at the top of this function and has
    # already returned. What is left is a command the walk called static
    # because its outermost head was `test` or `grep`, which is precisely the
    # misreading this ticket exists to correct when the thing being tested is
    # a RUNNING system: `test "$(rpk topic list | grep -c t)" = "1"` inspects
    # no artifact. Promoting that is a correction, not a loosening, and it is
    # gated on the command actually reading a live surface.
    if walked in (
        EnumCheckProofClass.INDETERMINATE,
        EnumCheckProofClass.SURROGATE,
    ) and _is_asserted_live_readback(text, segments):
        return EnumCheckProofClass.READBACK
    return walked


def _walk_verdict(segments: Sequence[str]) -> EnumCheckProofClass:
    """The pre-OMN-18135 segment walk, unchanged, extracted so AC4 can follow it."""
    saw_merge_state = False
    saw_static = False
    for segment in segments:
        # OMN-18135: relocation is not evidence. Skipping the segment rather
        # than classifying it is what stops `cd <dir> && uv run pytest ...`
        # ending the walk on `cd` and never reaching the runner.
        if _is_transparent_segment(segment):
            continue
        # OMN-18135: the OMN-15391 corpus is checked on the whole text above,
        # and that check does not see through a prefix. Without this per-
        # segment repeat, making `cd` transparent would promote
        # `cd occ && uv run pytest tests/test_evidence_admissibility.py -q`
        # from INDETERMINATE to BEHAVIOR -- turning a known ticket-independent
        # suite into a flip-releasing proof. That is the one direction this
        # change must not move, so the predicate is asked again here.
        if is_surrogate_check_value(segment):
            return EnumCheckProofClass.SURROGATE
        head, args = _segment_head_and_args(segment)
        if not head:
            return EnumCheckProofClass.INDETERMINATE
        if _is_behavior(head, args):
            return EnumCheckProofClass.BEHAVIOR
        if _is_merge_state(head, args):
            saw_merge_state = True
            continue
        if _is_static_inspection(head):
            saw_static = True
            continue
        return EnumCheckProofClass.INDETERMINATE

    if saw_merge_state:
        return EnumCheckProofClass.MERGE_STATE
    if saw_static:
        return EnumCheckProofClass.SURROGATE
    return EnumCheckProofClass.INDETERMINATE


def classify_check(check: Mapping[str, Any]) -> EnumCheckProofClass:
    """Classify one declared check mapping from a ``dod_evidence`` item."""
    check_type = check.get("check_type") or ""
    if check_type == "released":
        # OMN-18010 deliverable 2. Not a command, and deliberately NOT
        # BEHAVIOR: proving that a merge is contained in a released tag whose
        # version the index serves says the change is REACHABLE, and says
        # nothing whatever about whether it works. Classifying it BEHAVIOR
        # would let a release cut satisfy the behaviour-proving leg of a flip
        # rule — the same conflation OMN-15911 exists to prevent. MERGE_STATE
        # is the honest label: distribution state, one step past merge state.
        return EnumCheckProofClass.MERGE_STATE
    if check_type == "file_exists":
        # Not a command: a path existing is static-artifact inspection by
        # definition, whatever the item's prose claims it demonstrates.
        return EnumCheckProofClass.SURROGATE
    command = check.get("command") or check.get("check_value") or ""
    if not isinstance(command, str):
        return EnumCheckProofClass.INDETERMINATE
    return classify_command(command)


def classify_item_checks(checks: Iterable[Any]) -> EnumCheckProofClass:
    """Roll a ``dod_evidence`` item's checks up to a single class.

    An item is VERIFIED only when every one of its checks passed, so the
    strongest class present is an honest statement of what the item proved.
    An item with no usable checks is INDETERMINATE (it is SKIPPED or FAILED
    upstream and can never be behavior-proving).
    """
    found: set[EnumCheckProofClass] = set()
    for check in checks:
        if not isinstance(check, Mapping):
            found.add(EnumCheckProofClass.INDETERMINATE)
            continue
        found.add(classify_check(check))
    if not found:
        return EnumCheckProofClass.INDETERMINATE
    for candidate in CHECK_PROOF_CLASS_PRECEDENCE:
        if candidate in found:
            return candidate
    return EnumCheckProofClass.INDETERMINATE


__all__ = [
    "CHECK_PROOF_CLASS_PRECEDENCE",
    "classify_check",
    "classify_command",
    "classify_item_checks",
]
