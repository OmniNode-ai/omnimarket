# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The forced-command gate: only a command on the overlay's allowlist reaches a program (step 0).

The ssh key of the read-only principal is installed with ``restrict,command="<gate>"``, so sshd
runs the gate for every login and hands it the client's request in ``SSH_ORIGINAL_COMMAND``. The gate
splits the request into words, never gives it to a shell, and runs it only when its leading words
are an allowed command, every flag is one that command allows and every operand has the allowed
shape and none of the refused ones. Anything else is refused with a reason that names the rule, not
the text, and the program is never started.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from ..models.model_read_only_principal import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    EnumGateDecision,
    EnumGateReason,
    ModelAllowedCommand,
    ModelGateDecideRequest,
    ModelGateDecision,
    ModelReadOnlyPrincipalOverlay,
    ModelSshGate,
)


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelReadOnlyPrincipalOverlay:
    """The principal's overlay: the pinned file, else ``<root>/node_read_only_principal_effect``."""
    return load_structured_overlay(
        OVERLAY_ID, ModelReadOnlyPrincipalOverlay, environ, file_env=OVERLAY_FILE_ENV
    )


def _refuse(reason: EnumGateReason) -> ModelGateDecision:
    return ModelGateDecision(decision=EnumGateDecision.REFUSE, reason=reason)


def _matching(spec: ModelAllowedCommand, words: list[str]) -> list[str] | None:
    prefix = list(spec.argv_prefix)
    if words[: len(prefix)] != prefix:
        return None
    return words[len(prefix) :]


def decide(command: str, gate: ModelSshGate) -> ModelGateDecision:
    """Decide ``command`` against ``gate``; the answer never holds the command text."""
    if not command.strip():
        return _refuse(EnumGateReason.EMPTY)
    if any(ch in command for ch in ("\n", "\r", "\x00")):
        return _refuse(EnumGateReason.UNPARSEABLE)
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return _refuse(EnumGateReason.UNPARSEABLE)
    if not words:
        return _refuse(EnumGateReason.EMPTY)
    refused = [re.compile(pattern) for pattern in gate.refused_operand_patterns]
    failure = EnumGateReason.NOT_ON_ALLOWLIST
    for spec in gate.allowed_commands:
        rest = _matching(spec, words)
        if rest is None:
            continue
        verdict = _check_rest(spec, rest, refused)
        if verdict is EnumGateReason.ALLOWED:
            return ModelGateDecision(
                decision=EnumGateDecision.ALLOW,
                reason=EnumGateReason.ALLOWED,
                argv=tuple(words),
            )
        failure = verdict
    return _refuse(failure)


def _check_rest(
    spec: ModelAllowedCommand, rest: list[str], refused: list[re.Pattern[str]]
) -> EnumGateReason:
    shape = re.compile(spec.operand_pattern)
    operands = 0
    for word in rest:
        if word.startswith("-"):
            if word not in spec.allowed_flags:
                return EnumGateReason.FLAG_NOT_ALLOWED
            continue
        operands += 1
        if operands > spec.max_operands:
            return EnumGateReason.TOO_MANY_OPERANDS
        if shape.fullmatch(word) is None:
            return EnumGateReason.OPERAND_SHAPE
        if any(pattern.search(word) for pattern in refused):
            return EnumGateReason.OPERAND_REFUSED
    return EnumGateReason.ALLOWED


class HandlerReadOnlyCommandGate:
    """Operation ``gate_decide``: classify one ssh command against the overlay."""

    def __init__(
        self,
        overlay_source: Callable[[], ModelReadOnlyPrincipalOverlay] | None = None,
    ) -> None:
        self._overlay_source = overlay_source or load_overlay

    def handle(self, request: ModelGateDecideRequest) -> ModelGateDecision:
        overlay = (
            request.overlay if request.overlay is not None else self._overlay_source()
        )
        return decide(request.command, overlay.ssh)
