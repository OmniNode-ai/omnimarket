# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shell the verifier runs a contract command check with, read from its source.

A contract check is judged under the verifier's own shell. A test that carries a
local literal for that shell proves nothing about the verifier: it stays green when
the verifier stops using pipefail. This reads the argv prefix out of the
verifier's ``subprocess.run`` call instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

EVIDENCE_COLLECTOR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dod_verify"
    / "services"
    / "evidence_collector.py"
)


def verifier_shell_from_source(source: str) -> list[str]:
    """The argv prefix of the one ``subprocess.run([... , cmd_str])`` call.

    The call is the verifier's command-check runner: a list whose first element is
    ``bash`` and whose last element is the command, ``cmd_str``. Anything else (no
    such call, or more than one) raises, so a refactor of the runner cannot leave
    a test reading a stale shape.
    """
    runs: list[list[str]] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            continue
        elts = node.args[0].elts
        last = elts[-1] if elts else None
        if not (
            isinstance(last, ast.Name)
            and last.id == "cmd_str"
            and isinstance(elts[0], ast.Constant)
            and elts[0].value == "bash"
            and all(isinstance(e, ast.Constant) for e in elts[:-1])
        ):
            continue
        runs.append([str(e.value) for e in elts[:-1] if isinstance(e, ast.Constant)])
    if len(runs) != 1:
        raise AssertionError(
            f"expected exactly one bash command-check runner, found {len(runs)}"
        )
    return runs[0]


def verifier_shell() -> list[str]:
    """The shell prefix of the verifier in this tree."""
    return verifier_shell_from_source(EVIDENCE_COLLECTOR.read_text(encoding="utf-8"))


# The hosted gate does not run this tree's collector: the caller installs the
# verifier at its ``verifier-tag``. So the shell a contract check runs under there
# is the one that tag's runner names. The lines below are copied verbatim from
# ``EVIDENCE_COLLECTOR`` at ``VERIFIER_TAG`` (``git show <tag>:<path>``); a test
# compares the tag the caller pins with this one, so moving the pin without
# re-reading these lines fails, and compares them with the tag's file wherever
# that object is in the clone.
VERIFIER_TAG = "dev-20261101000200-949993edf3b7"

# ``check_type in ("command", "test_passes")`` takes the one command path, and
# that path hands the check's own text to ``_run_command_check``.
VERIFIER_TAG_DISPATCH = (
    '            if check_type in ("command", "test_passes"):',
    "                ok, msg = self._run_command_check(",
)

# ``cmd_str`` is the check's own text, and it is what the runner executes.
VERIFIER_TAG_CMD = (
    '        cmd_str = check.get("command") or check.get("check_value", "")',
)

# The runner: bash with pipefail, list form, the command last.
VERIFIER_TAG_RUN = (
    "            result = subprocess.run(",
    '                ["bash", "-o", "pipefail", "-c", cmd_str],',
    "                capture_output=True,",
    "                text=True,",
    "                timeout=timeout_s,",
    "                cwd=run_cwd,",
    "                env=run_env,",
    "            )",
)

# Any non-zero status is a failed check, so pytest's exit 5 (nothing selected)
# fails it exactly as exit 1 does.
VERIFIER_TAG_STATUS = (
    "        if result.returncode != 0:",
    '        return True, f"OK ({elapsed_ms}ms): {stdout[:200]}"',
)


def verifier_shell_at_tag() -> list[str]:
    """The argv prefix of the runner at ``VERIFIER_TAG``, from the copied lines."""
    import textwrap

    return verifier_shell_from_source(textwrap.dedent("\n".join(VERIFIER_TAG_RUN)))
