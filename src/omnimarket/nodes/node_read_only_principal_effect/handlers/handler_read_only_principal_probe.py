# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe the read-only principal: a read that works and a real mutating attempt that is refused (step 0).

Section 11 accepts the principal when "a mutating step under it is refused by the credential, for
ssh, GitHub and the ledger". This handler is that reading. For each channel it runs the overlay's
read attempt, which must exit 0, and the overlay's mutating attempt, which must exit non-zero with
one of the channel's refusal words in what it printed. A mutation that exits 0 is ACCEPTED and fails
the verdict; one that fails without a refusal word is INDETERMINATE and fails it too, so a dead
network or a missing program never reads as a refusal. The receipt holds outcomes only.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models.model_read_only_principal import (
    EnumAttemptOutcome,
    EnumChannel,
    ModelChannelProbe,
    ModelChannelReceipt,
    ModelProbeRequest,
    ModelProbeResult,
    ModelReadOnlyPrincipalOverlay,
)
from ..protocols.protocol_attempt_executor import (
    ModelAttemptRun,
    ProtocolAttemptExecutor,
)
from .adapter_subprocess_attempt_executor import SubprocessAttemptExecutor
from .handler_read_only_command_gate import load_overlay


def read_outcome(run: ModelAttemptRun) -> EnumAttemptOutcome:
    return (
        EnumAttemptOutcome.READ_OK
        if run.exit_code == 0
        else EnumAttemptOutcome.READ_FAILED
    )


def mutate_outcome(
    run: ModelAttemptRun, refusal_markers: tuple[str, ...]
) -> EnumAttemptOutcome:
    if run.exit_code == 0:
        return EnumAttemptOutcome.MUTATION_ACCEPTED
    text = run.text.lower()
    if any(marker.lower() in text for marker in refusal_markers):
        return EnumAttemptOutcome.MUTATION_REFUSED
    return EnumAttemptOutcome.MUTATION_INDETERMINATE


class HandlerReadOnlyPrincipalProbe:
    """Operation ``probe``: one receipt per channel and a verdict that is PASS only if all three refuse."""

    def __init__(
        self,
        executor: ProtocolAttemptExecutor | None = None,
        overlay_source: Callable[[], ModelReadOnlyPrincipalOverlay] | None = None,
    ) -> None:
        self._executor = (
            executor if executor is not None else SubprocessAttemptExecutor()
        )
        self._overlay_source = overlay_source or load_overlay

    def handle(self, request: ModelProbeRequest) -> ModelProbeResult:
        overlay = (
            request.overlay if request.overlay is not None else self._overlay_source()
        )
        chosen = set(request.channels) or set(EnumChannel)
        receipts = tuple(
            self._channel(probe) for probe in overlay.probes if probe.channel in chosen
        )
        return ModelProbeResult(
            principal=overlay.principal,
            passed=all(receipt.passed for receipt in receipts),
            complete=chosen == set(EnumChannel),
            receipts=receipts,
        )

    def _channel(self, probe: ModelChannelProbe) -> ModelChannelReceipt:
        read = read_outcome(self._executor.run(probe.read))
        mutate = mutate_outcome(self._executor.run(probe.mutate), probe.refusal_markers)
        return ModelChannelReceipt(
            channel=probe.channel,
            read=read,
            mutate=mutate,
            passed=read == EnumAttemptOutcome.READ_OK
            and mutate == EnumAttemptOutcome.MUTATION_REFUSED,
        )
