#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""
CI/pre-commit check: no bare `python` interpreter shell-out from a hook
(OMN-17219; same defect class as OMN-16958).

Reported defect
---------------
macOS does not ship a bare `python` executable. Homebrew's `python@3.x` formula
installs `python3` only; a bare `python` exists solely inside an activated
virtualenv (or via a pyenv shim). A pre-commit hook whose `entry:` is
`python scripts/ci/foo.py` therefore hard-fails for any committer who is not
inside such an environment:

    - hook id: one-occ-producer
    - exit code: 1
    Executable `python` not found

`git commit` is refused outright; the only workaround is `uv run git commit`,
which is not a workflow anyone should have to discover.

Rule
----
1. `.pre-commit-config.yaml` `entry:` strings (`language: system` / `script`,
   including inline `bash -c '...'` bodies) must not invoke a bare `python`
   OR a bare `python3`. The repo's sanctioned interpreter resolution is
   `uv run python ...` -- uv resolves the project interpreter itself, so the
   hook is independent of whatever the committer's shell happens to expose.
   `python3` is rejected as well as `python`: it happens to exist on macOS, so
   allowing it would let the class come back as a "fix" that quietly bypasses
   the project environment.
2. Shell scripts referenced by such an `entry:` must not invoke a bare
   `python`. Bare `python3` is tolerated in shell scripts, where it is used
   for guarded stdlib-only probes (`command -v python3 && python3 -c ...`).

Allowed forms everywhere: `uv run [flags] python`, an absolute path
(`/opt/homebrew/bin/python3`), a variable (`"$PYTHON"`, `${PY}`), and a
`python -c` string that is an *argument* rather than the command word.

Suppress a reviewed false positive with `# precommit-interp-ok: <reason>` on
the flagged line.

Wired as BOTH a pre-commit hook (`precommit-interpreter-resolution`) and a CI
step in `.github/workflows/precommit-fail-loud-gate.yml`, so the class cannot
regenerate (Operating Rule 5: enforcement, not detection).
"""

from __future__ import annotations

import contextlib
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"

SUPPRESS_MARKER = "precommit-interp-ok"

LOCAL_LANGUAGES = {"system", "script"}

# Command words rejected in a hook `entry:`. `python` is the reported break;
# `python3` is rejected alongside it so the sanctioned `uv run python` form is
# the only way out (see docstring rule 1).
ENTRY_BANNED = {"python", "python3"}

# Command word rejected inside a referenced shell script (docstring rule 2).
# A shell line puts a command in far more positions than "start of line": after
# a separator (`;`, `&&`, `||`, `|`), inside a substitution (`$(`, backticks),
# after a control keyword (`if`, `then`, `while`, ...), behind a transparent
# wrapper (`env`, `exec`, `command`), and behind inline `FOO=bar` assignments.
# All of those are command positions and all of them are scanned.
_ASSIGN = r"""(?:[A-Za-z_]\w*=(?:"[^"]*"|'[^']*'|\S*)\s+)*"""
_CMD_PREFIX = (
    r"(?:^|[;&|(`!]|\$\(|"
    r"\b(?:if|then|else|elif|do|while|until|exec|command|nice|time|env|xargs)\s)"
)
SCRIPT_BANNED_RE = re.compile(_CMD_PREFIX + r"\s*" + _ASSIGN + r"python(?![\w./-])")

# `env FOO=bar <cmd>` and `exec <cmd>` are transparent wrappers: keep scanning
# past them for the real command word.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Shell operators that terminate one command and start another. `shlex` in
# punctuation mode preserves them as their own tokens, so an entry such as
# `bash -c 'true && python x.py'` is scanned in every command position rather
# than only the first (CodeRabbit finding, PR #2223).
_COMMAND_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", ";;"}

# Transparent wrappers whose own arguments are the real command.
_WRAPPERS = {"env", "exec", "command", "nice", "time", "builtin"}

# `uv run` options that consume a FOLLOWING argument. Without this list a
# `uv run --with pyyaml python3 x.py` entry would be read as the command word
# `pyyaml` and silently accepted (CodeRabbit finding, PR #2223). Options in
# `--opt=value` form carry their own value and need no lookahead.
_UV_RUN_VALUE_OPTS = {
    "--with",
    "--with-editable",
    "--with-requirements",
    "--python",
    "-p",
    "--directory",
    "--project",
    "--package",
    "--index",
    "--default-index",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--extra",
    "--group",
    "--only-group",
    "--no-group",
    "--config-file",
    "--cache-dir",
    "--refresh-package",
    "--resolution",
    "--prerelease",
    "--python-preference",
    "--color",
    "--env-file",
    "--constraints",
    "-c",
    "--overrides",
    "--no-binary-package",
    "--no-build-package",
}


def _shell_tokens(text: str) -> list[str]:
    """Tokenize a shell string, keeping operators (`&&`, `|`, `;`) as tokens.

    `shlex.split` collapses operators into the surrounding words, which would
    hide the second command of `bash -c 'true && python x.py'`.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list into per-command segments on shell operators."""
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _COMMAND_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return [seg for seg in segments if seg]


def _command_word(tokens: list[str]) -> str | None:
    """Return the interpreter a single command segment will actually exec.

    Sees through inline `FOO=bar` assignments, transparent wrappers (`env`,
    `exec`, ...) and a full `uv run [options]` prefix -- including options that
    consume a following value -- so the token reported is the one the shell will
    resolve on PATH. Returns None when `uv run` covers the command: uv resolves
    the project interpreter itself, which is the sanctioned form.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _WRAPPERS or _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        if tok == "uv" and i + 1 < len(tokens) and tokens[i + 1] == "run":
            i += 2
            while i < len(tokens) and tokens[i].startswith("-"):
                opt = tokens[i]
                i += 1
                if "=" not in opt and opt in _UV_RUN_VALUE_OPTS:
                    i += 1  # consume the option's value
            return None
        return tok
    return None


def _scan_entry(hook_id: str, entry: str) -> list[str]:
    """Return violation strings for one hook `entry:` value."""
    violations: list[str] = []
    if SUPPRESS_MARKER in entry:
        return violations

    try:
        tokens = _shell_tokens(entry)
    except ValueError:
        # Unbalanced quoting -- fail loud rather than silently skipping.
        return [f"{hook_id}: entry is not shell-parsable: {entry!r}"]

    fragments: list[list[str]] = [tokens]

    # `bash -c '<body>'` / `sh -c '<body>'`: scan every command in the body too.
    for idx, tok in enumerate(tokens):
        if (
            tok in {"bash", "sh", "zsh"}
            and idx + 2 < len(tokens)
            and tokens[idx + 1] == "-c"
        ):
            with contextlib.suppress(ValueError):
                fragments.append(_shell_tokens(tokens[idx + 2]))

    for frag in fragments:
        for segment in _split_segments(frag):
            word = _command_word(segment)
            if word in ENTRY_BANNED:
                violations.append(
                    f"{hook_id}: entry invokes bare `{word}` -- use `uv run python` "
                    f"(macOS has no bare `python`; entry={entry!r})"
                )
    return violations


def _referenced_scripts(entry: str) -> list[Path]:
    """Shell scripts referenced by a hook entry that exist in this repo."""
    out: list[Path] = []
    try:
        tokens = shlex.split(entry)
    except ValueError:
        return out
    for tok in tokens:
        if not tok.endswith((".sh", ".bash")):
            continue
        candidate = REPO_ROOT / tok
        if candidate.is_file():
            out.append(candidate)
    return out


def _scan_script(path: Path) -> list[str]:
    violations: list[str] = []
    rel = path.relative_to(REPO_ROOT)
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if SUPPRESS_MARKER in line:
            continue
        if SCRIPT_BANNED_RE.search(line):
            violations.append(
                f"{rel}:{lineno}: invokes bare `python` -- use `uv run python` "
                f"or a guarded `python3` ({stripped!r})"
            )
    return violations


def main() -> int:
    if not CONFIG_PATH.is_file():
        print(f"ERROR: {CONFIG_PATH} not found", file=sys.stderr)
        return 1

    config: Any = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    seen_scripts: set[Path] = set()
    scanned_hooks = 0

    for repo in config.get("repos", []) or []:
        for hook in repo.get("hooks", []) or []:
            entry = hook.get("entry")
            if not entry:
                continue
            language = hook.get("language", "system")
            if language not in LOCAL_LANGUAGES:
                continue
            hook_id = hook.get("id", "<unnamed>")
            scanned_hooks += 1
            violations.extend(_scan_entry(hook_id, entry))
            for script in _referenced_scripts(entry):
                if script in seen_scripts:
                    continue
                seen_scripts.add(script)
                violations.extend(_scan_script(script))

    # Non-vacuity floor: a parse regression that collapses the scan to zero
    # hooks must fail, not pass silently.
    if scanned_hooks < 10:
        print(
            f"ERROR: interpreter gate scanned only {scanned_hooks} local hooks "
            "-- refusing to report a vacuous pass",
            file=sys.stderr,
        )
        return 1

    if violations:
        print("Bare-interpreter pre-commit hooks found:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            "\nFix: replace the bare interpreter with the repo's sanctioned form, "
            "`uv run python <script>`.\n"
            f"Suppress a reviewed false positive with `# {SUPPRESS_MARKER}: <reason>`.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {scanned_hooks} local pre-commit hooks and {len(seen_scripts)} "
        "referenced shell scripts use a resolvable interpreter"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
