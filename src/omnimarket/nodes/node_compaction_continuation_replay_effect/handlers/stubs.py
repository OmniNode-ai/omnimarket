# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stubs of the sandboxed continuation (measurement plan section 3.7).

Ledger writes go to a scratch copy; gh, onex and any network tool refuse and log; reads of
frozen files return their pinned text; everything else returns a typed refusal. Nothing here
touches the network, a subprocess or any file other than the scratch ledger.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    EnumRefusalReason,
    EnumToolOutcome,
    ModelRecordedCall,
    ModelToolCall,
)

REFUSED_BY_DESIGN = frozenset(["gh", "onex"])
NETWORK_PROGRAMS = frozenset(
    [
        "curl",
        "wget",
        "ssh",
        "scp",
        "nc",
        "ncat",
        "telnet",
        "ftp",
        "rsync",
        "http",
        "https",
    ]
)
NETWORK_GIT_VERBS = frozenset(["push", "fetch", "pull", "clone", "ls-remote", "remote"])
LEDGER_PROGRAMS = frozenset(["onex-ledger"])
_NETWORK_TOOLS = frozenset(["network", "web_fetch", "web_search", "http"])
_CLASS_DEPTH = {"gh": 3, "onex": 3, "git": 2}
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_TICKET = re.compile(r"OMN-\d+")
_URL = re.compile(r"(?i)\b(?:https?|ssh|ftp)://")


class ProtocolToolStubs(Protocol):
    """What the replay handler needs from a stub set; a mutant of it is what AC1 must catch."""

    def dispatch(self, turn: int, call: ModelToolCall) -> ModelRecordedCall: ...


def _tokens(command: str) -> list[str] | None:
    try:
        toks = shlex.split(command)
    except ValueError:
        return None
    return toks or None


def call_class_of(call: ModelToolCall) -> str:
    """Structural class of a call: the tool, and for a shell command its leading words."""
    if call.tool != "bash":
        return call.tool or "unknown"
    toks = _tokens(call.command)
    if not toks:
        return "bash.malformed"
    words: list[str] = []
    for tok in toks:
        if tok.startswith("-") or len(words) >= _CLASS_DEPTH.get(toks[0], 1):
            break
        words.append(tok)
    return ".".join(words)


def item_key_of(call: ModelToolCall) -> str:
    """The explicit key, else the first ledger stamp or ticket id the call text carries."""
    if call.item_key:
        return call.item_key
    text = " ".join([call.command, call.row, call.path])
    found = _STAMP.search(text) or _TICKET.search(text)
    return found.group(0) if found else ""


class SandboxTools:
    """The standard stub set. Writes only the scratch ledger path it is built with."""

    def __init__(self, scratch_ledger: Path, frozen: Mapping[str, str]) -> None:
        self._scratch = scratch_ledger
        self._frozen = dict(frozen)

    def _record(
        self,
        turn: int,
        call: ModelToolCall,
        outcome: EnumToolOutcome,
        reason: EnumRefusalReason | None = None,
        detail: str = "",
    ) -> ModelRecordedCall:
        return ModelRecordedCall(
            turn=turn,
            call=call,
            call_class=call_class_of(call),
            item_key=item_key_of(call),
            outcome=outcome,
            refusal_reason=reason,
            detail=detail,
        )

    def _refuse(
        self,
        turn: int,
        call: ModelToolCall,
        reason: EnumRefusalReason,
        detail: str = "",
    ) -> ModelRecordedCall:
        return self._record(turn, call, EnumToolOutcome.REFUSED, reason, detail)

    def _append_scratch(self, row: str) -> None:
        with self._scratch.open("a", encoding="utf-8") as fh:
            fh.write(row.rstrip("\n") + "\n")

    def dispatch(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        if call.tool in _NETWORK_TOOLS:
            return self._refuse(turn, call, EnumRefusalReason.NETWORK, call.tool)
        if call.tool == "ledger_append":
            return self._ledger_append(turn, call, call.row)
        if call.tool == "read_file":
            return self._read(turn, call)
        if call.tool == "bash":
            return self._bash(turn, call)
        return self._refuse(turn, call, EnumRefusalReason.UNSUPPORTED_TOOL, call.tool)

    def _ledger_append(
        self, turn: int, call: ModelToolCall, row: str
    ) -> ModelRecordedCall:
        if not row.strip():
            return self._refuse(
                turn, call, EnumRefusalReason.MALFORMED, "empty ledger row"
            )
        self._append_scratch(row)
        named = call.path
        redirected = bool(named) and os.path.realpath(named) != os.path.realpath(
            self._scratch
        )
        return self._record(
            turn,
            call,
            EnumToolOutcome.SCRATCH_WRITE,
            detail=f"redirected from {named}" if redirected else "",
        )

    def _read(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        if not call.path:
            return self._refuse(turn, call, EnumRefusalReason.MALFORMED, "no path")
        if call.path not in self._frozen:
            return self._refuse(turn, call, EnumRefusalReason.UNPINNED_READ, call.path)
        return self._record(
            turn, call, EnumToolOutcome.PINNED_READ, detail=self._frozen[call.path]
        )

    def _bash(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        toks = _tokens(call.command)
        if toks is None:
            return self._refuse(
                turn, call, EnumRefusalReason.MALFORMED, "empty or unparsable"
            )
        prog = os.path.basename(toks[0])
        if prog in REFUSED_BY_DESIGN:
            return self._refuse(turn, call, EnumRefusalReason.REFUSED_BY_DESIGN, prog)
        if (
            prog in NETWORK_PROGRAMS
            or _URL.search(call.command)
            or (prog == "git" and any(t in NETWORK_GIT_VERBS for t in toks[1:3]))
        ):
            return self._refuse(turn, call, EnumRefusalReason.NETWORK, prog)
        if prog in LEDGER_PROGRAMS:
            if "--append" in toks and toks.index("--append") + 1 < len(toks):
                return self._ledger_append(turn, call, toks[toks.index("--append") + 1])
            return self._refuse(
                turn, call, EnumRefusalReason.MALFORMED, "ledger call without --append"
            )
        return self._refuse(turn, call, EnumRefusalReason.UNSUPPORTED_TOOL, prog)
