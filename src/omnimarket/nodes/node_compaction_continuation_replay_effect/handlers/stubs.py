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

import yaml

from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    EnumRefusalReason,
    EnumToolOutcome,
    ModelRecordedCall,
    ModelToolCall,
)

from ..models.model_policy import ModelStubPolicy


def load_stub_policy() -> ModelStubPolicy:
    contract = Path(__file__).resolve().parent.parent / "contract.yaml"
    return ModelStubPolicy.model_validate(
        yaml.safe_load(contract.read_text())["sandbox"]
    )


_DEFAULT_POLICY = load_stub_policy()
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


def _unwrap(toks: list[str]) -> list[str]:
    if os.path.basename(toks[0]) == "uv" and len(toks) > 2 and toks[1] == "run":
        index = 2
        while index < len(toks):
            if toks[index] in {"--project", "--directory"}:
                index += 2
            elif toks[index] in {"--frozen", "--locked", "--no-sync"}:
                index += 1
            else:
                break
        if index < len(toks):
            return toks[index:]
    return toks


def call_class_of(
    call: ModelToolCall, policy: ModelStubPolicy = _DEFAULT_POLICY
) -> str:
    """Structural class of a call: the tool, and for a shell command its leading words."""
    if call.tool != "bash":
        return call.tool or "unknown"
    toks = _tokens(call.command)
    if not toks:
        return "bash.malformed"
    toks = _unwrap(toks)
    toks[0] = os.path.basename(toks[0])
    words: list[str] = []
    for tok in toks:
        if tok.startswith("-") or len(words) >= policy.class_depth.get(
            os.path.basename(toks[0]), 1
        ):
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

    def __init__(
        self,
        scratch_ledger: Path,
        frozen: Mapping[str, str | bytes],
        *,
        policy: ModelStubPolicy = _DEFAULT_POLICY,
    ) -> None:
        self._policy = policy
        self._scratch = scratch_ledger
        self._frozen = dict(frozen)

    def _record(
        self,
        turn: int,
        call: ModelToolCall,
        outcome: EnumToolOutcome,
        reason: EnumRefusalReason | None = None,
        detail: str = "",
        content: bytes | None = None,
    ) -> ModelRecordedCall:
        return ModelRecordedCall(
            turn=turn,
            call=call,
            call_class=call_class_of(call, self._policy),
            item_key=item_key_of(call),
            outcome=outcome,
            refusal_reason=reason,
            detail=detail,
            content=content,
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
        if call.tool in self._policy.network_tools:
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
        content = self._frozen[call.path]
        return self._record(
            turn,
            call,
            EnumToolOutcome.PINNED_READ,
            detail=content if isinstance(content, str) else "pinned bytes",
            content=content.encode("utf-8") if isinstance(content, str) else content,
        )

    def _bash(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        toks = _tokens(call.command)
        if toks is None:
            return self._refuse(
                turn, call, EnumRefusalReason.MALFORMED, "empty or unparsable"
            )
        toks = _unwrap(toks)
        prog = os.path.basename(toks[0])
        if prog in self._policy.refused_programs:
            return self._refuse(turn, call, EnumRefusalReason.REFUSED_BY_DESIGN, prog)
        if (
            prog in self._policy.network_programs
            or _URL.search(call.command)
            or (
                prog == "git"
                and any(t in self._policy.network_git_verbs for t in toks[1:3])
            )
        ):
            return self._refuse(turn, call, EnumRefusalReason.NETWORK, prog)
        if prog in self._policy.ledger_programs:
            if "--append" in toks and toks.index("--append") + 1 < len(toks):
                return self._ledger_append(turn, call, toks[toks.index("--append") + 1])
            return self._refuse(
                turn, call, EnumRefusalReason.MALFORMED, "ledger call without --append"
            )
        return self._refuse(turn, call, EnumRefusalReason.UNSUPPORTED_TOOL, prog)
