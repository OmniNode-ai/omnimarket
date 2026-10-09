# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a bound check may go to the acceptor, and whether two lanes are one actor (OMN-20675).

Ported from the live closer (the dod-closeout-sweep workflow), rule for rule. A check that only
reads source text, file existence or pull request state shows a change landed, not that the
criterion holds: a symbol grep once passed on a ticket whose first criterion was false. No read,
no clock, no write.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    ModelBindingCheck,
)

CHECK_KINDS = ("test", "command", "lab_readback")
# Commands that only read source text, files or PR metadata.
SOURCE_READERS = frozenset(
    [
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "cat",
        "head",
        "tail",
        "sed",
        "awk",
        "wc",
        "ls",
        "find",
        "test",
        "[",
        "[[",
        "stat",
        "file",
        "sort",
        "uniq",
        "cut",
        "tr",
        "jq",
        "true",
        "echo",
        "printf",
        "realpath",
        "readlink",
        "basename",
        "dirname",
    ]
)
GREP_FAMILY = frozenset(["grep", "egrep", "fgrep", "rg", "ag", "ack"])
GIT_READ_SUBCOMMANDS = frozenset(
    [
        "grep",
        "show",
        "cat-file",
        "log",
        "ls-files",
        "ls-tree",
        "diff",
        "blame",
        "rev-parse",
        "merge-base",
    ]
)
# Prefixes that run the command after them; the stage is classified by what they run.
WRAPPERS = frozenset(
    [
        "env",
        "command",
        "exec",
        "time",
        "nice",
        "timeout",
        "nohup",
        "sudo",
        "xargs",
        "stdbuf",
    ]
)
SHELLS = frozenset(("bash", "sh", "zsh"))
INTERPRETERS = frozenset(("node", "python", "python3", "ruby", "perl"))
MAX_NESTING = 4

_A = re.ASCII
# An inline interpreter snippet that only probes the filesystem is a file-exists check.
_FS_PROBE = re.compile(
    r"\b(accessSync|existsSync|statSync|readFileSync|readdirSync|os\.path\.(exists|isfile|isdir)"
    r"|\.exists\(\)|\.is_file\(\)|\.is_dir\(\)|\.read_text\(\)|File\.exist|-e\s)",
    _A,
)
_FS_ONLY_IMPORTS = re.compile(
    r"(\bimport\s+(os|pathlib|sys)(\s*,\s*(os|pathlib|sys))*\b"
    r"|\bfrom\s+(os|pathlib)(\.path)?\s+import\b"
    r"|\brequire\(\s*['\"](node:)?(fs|path)['\"]\s*\))",
    _A,
)
_IMPORT_WORD = re.compile(r"\b(import|require)\b", _A)
_MUTATING = re.compile(
    r"\b(docker\s+(restart|rm|stop|start|kill|compose\s+(up|down|restart|rm)|exec\s+\S+\s+(rm|kill))"
    r"|kubectl\s+(apply|delete|rollout|set|scale|patch|edit|create|replace|annotate|label)"
    r"|helm\s+(install|upgrade|uninstall)"
    r"|curl\b[\s\S]*-X\s*(POST|PUT|DELETE|PATCH)"
    r"|curl\b[\s\S]*--data"
    r"|git\s+push"
    r"|gh\s+pr\s+(merge|close|create|edit)"
    r"|rpk\s+topic\s+(produce|create|delete)"
    r"|kcat\b[\s\S]*-P)\b",
    _A | re.IGNORECASE,
)
_SQL_WRITE = re.compile(
    r"(^|;)\s*(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy)\b",
    _A | re.IGNORECASE,
)
_SQL_QUOTED = re.compile(r"'(?:[^']|'')*'")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=", _A)
_PULLS_PATH = re.compile(r"/pulls\b", _A)
_LEADING_PATH = re.compile(r"^.*/")
_REPO_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_OWNER_SLUG = re.compile(r"omninode-ai/([^/\s]+)", re.IGNORECASE)
_TEST_SELECTOR = re.compile(
    r"[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_./-]+(\[[^\]`$;&|<>\n\\]*\])?)*"
)
_MERGED_REF = re.compile(r"origin/dev|[0-9a-f]{40}")
_LANE_TOKEN = re.compile(r"\blane=(\S+)", _A)
_TIMEOUT_DURATION = re.compile(r"\d+[smhd]?", _A)
_NICE_LEVEL = re.compile(r"-?\d+", _A)

# What each refusal class means, in the words the open comment uses.
REFUSAL_REASONS = {
    "no-check": "the binder returned no check object",
    "no-label": "the check carries no label",
    "unknown-kind": "the check kind is not test, command or lab_readback",
    "no-expected-observation": "the check names no expected observation",
    "no-test-selector": "a test check names no pytest node id",
    "test-selector-not-a-test-id": "the test selector is not a pytest node id (path/to/test_x.py::test_name)",
    "no-repo": "the test check names no single registry repo to run it in",
    "not-on-merged-dev": "the check does not run at origin/dev or a full merged sha",
    "no-command": "the check carries no runnable command",
    "no-cwd": "a command check names no working directory",
    "no-host": "a lab readback names no host",
    "not-read-only": "the command writes (a deploy, a push, a POST or SQL that changes state), and a check must only read",
    "pr-exists-only": "the command only reads pull request state, which shows a change landed, not that the criterion holds",
    "symbol-grep-only": "the command only reads source text for a symbol, which shows code exists, not that the criterion holds",
    "inspection-only": "the command only reads files or their existence and executes nothing",
}
UNKNOWN_REFUSAL_REASON = (
    "the check is not runnable as written for its kind, so it was never re-run"
)


def refusal_reason(refusal_class: str) -> str:
    reason = REFUSAL_REASONS.get(refusal_class) or UNKNOWN_REFUSAL_REASON
    return f"binding refused ({refusal_class}): {reason}"


def split_shell(command: str) -> list[list[str]]:
    """Segments (on unquoted && || ; and newlines) of pipeline stages (on unquoted |).

    Quote-aware, and drops unquoted comments, so a pattern such as 'a|b' or a trailing
    '# note' is never a stage of its own.
    """
    segments: list[list[str]] = []
    stages: list[str] = []
    current = ""
    quote = ""

    def end_stage() -> None:
        nonlocal current
        if current.strip():
            stages.append(current.strip())
        current = ""

    def end_segment() -> None:
        nonlocal stages
        end_stage()
        if stages:
            segments.append(stages)
        stages = []

    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                current += c + command[i + 1]
                i += 2
                continue
            if c == quote:
                quote = ""
            current += c
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            current += c
        elif c == "\\" and i + 1 < n:
            current += c + command[i + 1]
            i += 1
        elif c == "#" and (i == 0 or command[i - 1].isspace()):
            while i < n and command[i] != "\n":
                i += 1
            end_segment()
            continue
        elif c in ("\n", ";"):
            end_segment()
        elif (c == "&" and i + 1 < n and command[i + 1] == "&") or (
            c == "|" and i + 1 < n and command[i + 1] == "|"
        ):
            i += 1
            end_segment()
        elif c == "|":
            end_stage()
        else:
            current += c
        i += 1
    end_segment()
    return segments


def words_of(stage: str) -> list[str]:
    """Words of one stage, quote-aware; quotes are removed from each word."""
    words: list[str] = []
    current = ""
    quote = ""
    anything = False
    for c in stage:
        if quote:
            if c == quote:
                quote = ""
            else:
                current += c
            continue
        if c in ("'", '"'):
            quote = c
            anything = True
        elif c.isspace():
            if current or anything:
                words.append(current)
            current = ""
            anything = False
        else:
            current += c
    if current or anything:
        words.append(current)
    return words


def _basename(word: str) -> str:
    return _LEADING_PATH.sub("", word)


def _classify_words(w: list[str], depth: int) -> str:
    i = 0
    while i < len(w) and _ENV_ASSIGNMENT.match(w[i]):
        i += 1
    cmd = _basename(w[i]) if i < len(w) else ""
    if not cmd:
        return "read"
    if cmd in ("cd", "export", "set", "unset", "source", "."):
        return "skip"
    if cmd in WRAPPERS:
        j = i + 1
        while j < len(w) and (
            w[j].startswith("-")
            or _ENV_ASSIGNMENT.match(w[j])
            or (cmd == "timeout" and _TIMEOUT_DURATION.fullmatch(w[j]))
            or (cmd == "nice" and _NICE_LEVEL.fullmatch(w[j]))
        ):
            j += 1
        if j >= len(w):
            return "read" if cmd == "xargs" else "skip"
        return _classify_words(w[j:], depth + 1)
    if cmd in SHELLS and i + 2 < len(w) and w[i + 1] == "-c" and depth < MAX_NESTING:
        inner = kinds_of(w[i + 2], depth + 1)
        for kind in ("exec", "pr", "grep"):
            if kind in inner:
                return kind
        return "read"
    if cmd in INTERPRETERS and i + 2 < len(w) and w[i + 1] in ("-e", "-c"):
        code = w[i + 2]
        rest = _FS_ONLY_IMPORTS.sub(" ", code)
        if _FS_PROBE.search(code) and not _IMPORT_WORD.search(rest):
            return "read"
        return "exec"
    subcommand = w[i + 1] if i + 1 < len(w) else ""
    if cmd == "git" and subcommand in GIT_READ_SUBCOMMANDS:
        return "grep" if subcommand == "grep" else "read"
    if cmd == "gh" and (
        subcommand == "pr"
        or (subcommand == "api" and any(_PULLS_PATH.search(x) for x in w[i + 2 :]))
    ):
        return "pr"
    if cmd in GREP_FAMILY:
        return "grep"
    if cmd in SOURCE_READERS:
        return "read"
    return "exec"


def kinds_of(command: str, depth: int = 0) -> list[str]:
    stages = [stage for segment in split_shell(command) for stage in segment]
    kinds = [_classify_words(words_of(stage), depth) for stage in stages]
    return [kind for kind in kinds if kind != "skip"]


def writes_sql(command: str) -> bool:
    """Whether a psql call in the command would run SQL that changes state."""
    for segment in split_shell(command):
        for stage in segment:
            w = words_of(stage)
            at = next((k for k, x in enumerate(w) if _basename(x) == "psql"), -1)
            if at < 0:
                continue
            for k in range(at + 1, len(w) - 1):
                if w[k] in ("-c", "--command"):
                    sql = _SQL_QUOTED.sub("''", w[k + 1])
                    if _SQL_WRITE.search(sql):
                        return True
    return False


def normalize_repo(repo: str | None) -> str:
    """The registry repo as a bare name: the owner/name slug loses its owner."""
    text = (repo or "").strip()
    match = _OWNER_SLUG.fullmatch(text)
    return match.group(1) if match else text


def _text(value: str | None) -> str:
    return (value or "").strip()


def refusal_of(check: ModelBindingCheck | None) -> str | None:
    """None when the check may go to the acceptor, else the refusal class."""
    if check is None:
        return "no-check"
    if not _text(check.label):
        return "no-label"
    if check.kind not in CHECK_KINDS:
        return "unknown-kind"
    if not _text(check.expect):
        return "no-expected-observation"
    if check.kind == "test":
        if not _text(check.selector):
            return "no-test-selector"
        if not _TEST_SELECTOR.fullmatch(_text(check.selector)):
            return "test-selector-not-a-test-id"
        if check.repo is None or not _REPO_NAME.fullmatch(normalize_repo(check.repo)):
            return "no-repo"
        if not _MERGED_REF.fullmatch(check.ref or ""):
            return "not-on-merged-dev"
        return None
    command = _text(check.command)
    if not command:
        return "no-command"
    if check.kind == "command":
        if not _text(check.cwd):
            return "no-cwd"
        if not _MERGED_REF.fullmatch(check.ref or ""):
            return "not-on-merged-dev"
    if check.kind == "lab_readback" and not _text(check.host):
        return "no-host"
    if _MUTATING.search(command) or writes_sql(command):
        return "not-read-only"
    kinds = kinds_of(command)
    if not kinds:
        return "no-command"
    if "exec" in kinds:
        return None
    if "pr" in kinds:
        return "pr-exists-only"
    if "grep" in kinds:
        return "symbol-grep-only"
    return "inspection-only"


def identities(actor: str) -> list[str]:
    folded = actor.lower().strip()
    if not folded:
        return []
    found = {folded}
    found.update(_LANE_TOKEN.findall(folded))
    return sorted(found)


def same_actor(a: str, b: str) -> bool:
    """Two actors are independent only when both are named and share no identity.

    A blank actor names nobody, so it can never establish an independent acceptance.
    """
    ia = identities(a)
    ib = set(identities(b))
    if not ia or not ib:
        return True
    return any(x in ib for x in ia)
