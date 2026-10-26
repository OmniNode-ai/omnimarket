# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render Codex landing commands and reuse the generated-bytecode cleanup operation."""

from __future__ import annotations

import inspect
import json
import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..models.model_codex_landing import (
    ModelCodexLandingRequest,
    ModelCodexLandingResult,
)

BYTECODE_CLEAN_ARGV = (
    "clean",
    "-d",
    "-x",
    "-e",
    ".venv",
    "-e",
    "venv",
    "-e",
    "node_modules",
    "--",
    "*.pyc",
    "*.pyo",
)
BYTECODE_PRUNE_DIRS = (".git", ".venv", "venv", "node_modules")


# What git prints for each refused push. A push to a remote name the host's bare clone lacks reads like an access
# refusal ("does not appear to be a git repository ... correct access rights"); both are one shape.
LANDING_PUSH_SHAPES = (
    (
        "push-hook-refused",
        re.compile(r"\bhook=\S+"),
    ),  # a hook pre-commit reported Failed (OMN-21396)
    ("push-lease-refused", re.compile(r"stale info", re.IGNORECASE)),
    (
        "push-non-fast-forward",
        re.compile(r"non-fast-forward|fetch first|\[rejected\]", re.IGNORECASE),
    ),
    (
        "push-refused-access",
        re.compile(
            r"access rights|does not appear to be a git repository|permission to \S+ denied"
            r"|permission denied|authentication failed|\b403\b",
            re.IGNORECASE,
        ),
    ),
)
# A failed pr-view's own answer, its JSON on stdout, in one line: the reason, never the wrapper's stderr (OMN-21396).
PR_VIEW_FAILURE_READER = (
    "import json,sys\ns=sys.stdin.read()\ntry: d=json.loads(s)\nexcept ValueError: d=None\n"
    "facts={k:d[k] for k in ('status','outcome','timeout_s','error') if isinstance(d,dict) and k in d}\n"
    "print(json.dumps(facts,separators=(',',':')) if facts else s[:200])"
)
# The onex wrapper runs without the lane's github-bin shim on PATH, which it reported as a shadow (OMN-21396).
NO_SHIM_PATH_LINE = (
    'CXNP=$(printf %s "$PATH" | tr : "\\n" | grep -vxF "$C/github-bin" | paste -sd: -)'
)


def push_failed_hooks_line(log: str) -> str:
    """``CXPH``: the ids of the hooks pre-commit reported Failed in a refused push's log (OMN-21396)."""
    return (
        "CXPH=$(awk '/Failed$/ { failed=1; next } /[.]+.*(Passed|Skipped)$/ { failed=0; next } "
        'failed && /^- hook id: / { sub(/^- hook id: /, ""); print }\' '
        f'"{log}" | sort -u | paste -sd, -)'
    )


# One target's line from the landing step of a lane that carries --pr targets (``CodexEngine.target_lines``):
# ``<repo#n> pushed|failed|skipped|handoff <rest>``; a failed target's rest is ``<shape>: <reason>``.
TARGET_LINE_RE = re.compile(
    r"^(\S+) (pushed|failed|skipped|handoff)\b ?(.*)$", re.DOTALL
)


def target_failure(targets: Sequence[re.Match[str]]) -> dict[str, Any] | None:
    """The typed failure of a landing step with ``--pr`` targets (OMN-20864), else None when no target failed. A
    target that was pushed stays pushed: the failure lists only the targets that were not, by name, each with its
    shape and reason. ``shape`` is the first failed target's. A refused push takes the shape git's message names
    (``LANDING_PUSH_SHAPES``): push-hook-refused, push-lease-refused, push-non-fast-forward, push-refused-access,
    else push-failed."""
    failed: list[dict[str, str]] = []
    for m in targets:
        if m.group(2) != "failed":
            continue
        shape, _, reason = m.group(3).partition(": ")
        if shape == "push-failed":
            shape = next(
                (
                    name
                    for name, pattern in LANDING_PUSH_SHAPES
                    if pattern.search(reason)
                ),
                shape,
            )
        failed.append(
            {"pr": m.group(1), "shape": shape, "reason": reason.strip()[:200]}
        )
    if not failed:
        return None
    pushed = [m.group(1) for m in targets if m.group(2) == "pushed"]
    return {
        "kind": "landing-failed",
        "shape": failed[0]["shape"],
        "retryable": False,
        "reason": "targets not pushed: "
        + "; ".join(f"{f['pr']} {f['shape']}" for f in failed)
        + (f"; pushed: {', '.join(pushed)}" if pushed else ""),
        "targets": failed,
        "pushed": pushed,
    }


def landing_failure(landing: Sequence[tuple[str, str]]) -> dict[str, Any] | None:
    """The typed failure of a landing step that ended without a pushed branch, else None (OMN-20841). ``shape`` is
    commit-hook-failed (every commit refused), commit-hook-unchanged (a resume changed nothing), push-detached-head,
    push-hook-refused, push-lease-refused, push-non-fast-forward, push-refused-access or push-failed; ``hook`` names
    the failing hooks."""
    targets = [
        m
        for step, text in landing
        if step == "TARGET" and (m := TARGET_LINE_RE.match(text))
    ]
    if targets:
        return target_failure(targets)
    steps = [(step, text) for step, text in landing if step in ("COMMIT", "PUSH")]
    if not steps or any(
        step == "PUSH" and text.startswith("ok") for step, text in steps
    ):
        return None
    commits = [text for step, text in steps if step == "COMMIT"]
    push = next((text for step, text in reversed(steps) if step == "PUSH"), None)
    hook = next(
        (
            m.group(1)
            for m in (
                re.search(r"\bhook=(\S+?):?(?:\s|$)", t) for t in reversed(commits)
            )
            if m
        ),
        None,
    )
    if any(t.startswith("ok") for t in commits):
        if push is None:
            return None
        if push.startswith("skipped"):
            shape = "push-detached-head"
        else:
            shape = next(
                (name for name, pattern in LANDING_PUSH_SHAPES if pattern.search(push)),
                "push-failed",
            )
            if shape == "push-hook-refused" and (m := re.search(r"\bhook=(\S+)", push)):
                hook = m.group(1)
        reason = push
    elif any(t.startswith("blocked") for t in commits):
        shape = "hook-preflight-blocked"
        reason = commits[-1]
    elif any(t.startswith("failed") for t in commits):
        shape = (
            "commit-hook-unchanged"
            if any(t.startswith("unchanged") for t in commits)
            else "commit-hook-failed"
        )
        reason = commits[-1]
    else:
        return None
    return {
        "kind": "landing-failed",
        "shape": shape,
        "retryable": False,
        "reason": reason[:300],
        **({"hook": hook} if hook and hook != "unnamed" else {}),
    }


def _git_run(worktree: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, check=False
    )


def clean_generated_bytecode(worktree: Path) -> int:
    """Remove the untracked ``.pyc``/``.pyo`` files of ``worktree`` and the ``__pycache__`` directories they leave empty,
    and return how many bytecode files went. Nothing tracked and nothing that is not bytecode is touched."""
    listed = _git_run(
        worktree, BYTECODE_CLEAN_ARGV[0], "-n", *BYTECODE_CLEAN_ARGV[1:]
    ).stdout.decode(errors="replace")
    count = sum(1 for line in listed.splitlines() if line.startswith("Would remove "))
    if count:
        _git_run(worktree, BYTECODE_CLEAN_ARGV[0], "-f", "-q", *BYTECODE_CLEAN_ARGV[1:])
    for root, dirs, _files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in BYTECODE_PRUNE_DIRS]
        if Path(root).name == "__pycache__":
            with suppress(OSError):
                os.rmdir(root)  # only an empty directory goes
    return count


TARGET_JSON_READER = """import json, sys
answer = json.load(sys.stdin)
facts = answer.get("result") if isinstance(answer, dict) else None
if not isinstance(facts, dict) or facts.get("operation") != "pr_text":
    raise ValueError("exact-head response must be a pr_text answer")
state = facts.get("state")
if isinstance(state, str) and state.upper() == "CLOSED" and facts.get("merged") is True:
    state = "MERGED"
values = (facts.get("head_ref"), facts.get("head_sha"), state.upper() if isinstance(state, str) else state)
if not all(isinstance(value, str) and value and
           not any(ord(c) in (9, 10, 13) for c in value) for value in values):
    raise ValueError("exact-head response lacks valid string fields")
if not isinstance(facts.get("is_cross_repository"), bool):
    raise ValueError("exact-head response lacks a boolean fork flag")
print(chr(9).join([values[0], values[1], str(facts["is_cross_repository"]).lower(), values[2]]))
"""


def codex_handoff_refusal(landing: Sequence[tuple[str, str]]) -> dict[str, Any] | None:
    """The opened PR, pushed head and final refusal, for the runner's existing parent notice (OMN-20867)."""
    head = next(
        (
            m.group(1)
            for step, text in reversed(landing)
            if step == "COMMIT" and (m := re.fullmatch(r"ok sha=([0-9a-f]{40})", text))
        ),
        None,
    )
    url = next((text for step, text in reversed(landing) if step == "PR"), "")
    pr = re.fullmatch(r"https://github.com/[^/]+/([^/]+)/pull/([0-9]+)", url)
    answer = next((text for step, text in reversed(landing) if step == "HANDOFF"), "")
    refusal = re.fullmatch(r"rc=([0-9]+)(?: retries=[0-9]+)? (.*)", answer)
    if (
        not head
        or not pr
        or not refusal
        or int(refusal.group(1)) == 0
        or not any(step == "PUSH" and text.startswith("ok ") for step, text in landing)
    ):
        return None
    return {
        "pr": f"{pr.group(1)}#{pr.group(2)}",
        "head": head,
        "rc": int(refusal.group(1)),
        "refusal": refusal.group(2),
    }


def summarize_event(event: Mapping[str, Any]) -> list[str]:
    """Human lines for one stream-json event (the full event goes to the log file)."""
    kind = event.get("type")
    if "loop_run_id" in event and "delegate_run_ids" in event:
        return [
            f"code-edit {event.get('status')} loop={event.get('loop_run_id')} turns={event.get('turns')}"
        ]
    if kind == "assistant":
        lines = []
        for item in (event.get("message") or {}).get("content") or []:
            if item.get("type") == "text" and str(item.get("text") or "").strip():
                lines.append("say " + " ".join(str(item["text"]).split())[:300])
            elif item.get("type") == "tool_use":
                args = json.dumps(item.get("input") or {}, sort_keys=True)
                lines.append(f"tool {item.get('name')} {args[:200]}")
        return lines
    if kind == "result":
        return [
            f"result is_error={event.get('is_error')} turns={event.get('num_turns')} "
            + f"duration_ms={event.get('duration_ms')}"
        ]
    if kind == "system" and event.get("subtype") == "init":
        return [
            f"init model={event.get('model')} plugins={len(event.get('plugins') or [])} "
            + f"skills={len(event.get('skills') or event.get('slash_commands') or [])}"
        ]
    # codex exec --json (OMN-20363)
    if kind == "thread.started":
        return [f"init codex thread={event.get('thread_id')}"]
    if kind == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and str(item.get("text") or "").strip():
            return ["say " + " ".join(str(item["text"]).split())[:300]]
        if item.get("type") == "command_execution":
            return [
                f"tool shell exit={item.get('exit_code')} {' '.join(str(item.get('command') or '').split())[:200]}"
            ]
        if item.get("type") == "file_change":
            return [
                f"tool patch {json.dumps(item.get('changes') or [], sort_keys=True)[:200]}"
            ]
        return []
    if kind == "turn.completed":
        return [
            f"turn completed usage={json.dumps(event.get('usage') or {}, sort_keys=True)[:200]}"
        ]
    if kind in ("turn.failed", "error"):
        detail = (
            (event.get("error") or {}).get("message")
            if kind == "turn.failed"
            else event.get("message")
        )
        return [f"{kind} {' '.join(str(detail or '').split())[:300]}"]
    return []


class HandlerCodexLanding:
    """Compute shell fragments without GitHub, filesystem or process effects during rendering."""

    def handle(self, request: ModelCodexLandingRequest) -> ModelCodexLandingResult:
        program = (
            "import os, subprocess, sys\nfrom pathlib import Path\nfrom contextlib import suppress\n"
            f"BYTECODE_CLEAN_ARGV = {BYTECODE_CLEAN_ARGV!r}\n"
            f"BYTECODE_PRUNE_DIRS = {BYTECODE_PRUNE_DIRS!r}\n"
            + inspect.getsource(_git_run)
            + "\n"
            + inspect.getsource(clean_generated_bytecode)
            + "\nclean_generated_bytecode(Path(sys.argv[1]))\n"
        )
        command = f'python3 -B -c {shlex.quote("exec(" + repr(program) + ")")} "{request.worktree}"'
        return ModelCodexLandingResult(
            clean_command=command, json_reader=TARGET_JSON_READER
        )
