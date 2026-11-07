# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe the read-only principal: a read that works and a real mutating attempt that is refused (step 0).

Section 11 accepts the principal when "a mutating step under it is refused by the credential, for
ssh, GitHub and the ledger". This handler is that reading. For each channel it runs the overlay's
read attempt, which must exit 0, and the overlay's mutating attempt, which must exit non-zero with
one of the channel's refusal words in what it printed. A mutation that exits 0 is ACCEPTED and fails
the verdict; one that fails without a refusal word is INDETERMINATE and fails it too, so a dead
network or a missing program never reads as a refusal. The receipt holds outcomes only.

When the overlay declares an ``emission`` the probe runs under the floor's emission contract (OMN-22566): a start,
then a success when every probed channel read and refused, or a typed failure. A mutation that goes through is
``mutation_accepted`` and names its channels; a read that failed or a mutation that neither succeeded nor was
refused by word is ``probe_inconclusive``; a probe that raises is ``probe_cannot_run``. All of them go to the
floor's journal under one correlation id, where the receipt fold counts the chain for the overlay's piece.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from ..models.model_read_only_principal import (
    EnumAttemptOutcome,
    EnumChannel,
    ModelChannelProbe,
    ModelChannelReceipt,
    ModelProbeEmission,
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

EMIT_NODE = "node_read_only_principal_effect:probe"
FAILURE_MUTATION_ACCEPTED = "mutation_accepted"
FAILURE_INCONCLUSIVE = "probe_inconclusive"
FAILURE_CANNOT_RUN = "probe_cannot_run"
FAILURE_INTERRUPTED = "interrupted"


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
        *,
        sink: ProtocolEmissionSink | None = None,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._executor = (
            executor if executor is not None else SubprocessAttemptExecutor()
        )
        self._overlay_source = overlay_source or load_overlay
        self._sink = sink
        self._clock = clock
        self._environ = environ

    def handle(self, request: ModelProbeRequest) -> ModelProbeResult:
        overlay = (
            request.overlay if request.overlay is not None else self._overlay_source()
        )
        chain = self._chain(overlay)
        if chain is None:
            return self._probe(overlay, request)
        chain.start()
        try:
            result = self._probe(overlay, request)
        except KeyboardInterrupt:
            chain.failure(FAILURE_INTERRUPTED, "KeyboardInterrupt")
            raise
        except Exception as exc:
            chain.failure(FAILURE_CANNOT_RUN, type(exc).__name__)
            raise
        self._conclude(chain, result)
        return result

    def _chain(self, overlay: ModelReadOnlyPrincipalOverlay) -> ChainEmitter | None:
        emission = overlay.emission
        if emission is None:
            return None
        return ChainEmitter(
            self._sink or self._journal(emission),
            node=EMIT_NODE,
            piece=emission.piece,
            clock=self._clock,
        )

    def _journal(self, emission: ModelProbeEmission) -> ProtocolEmissionSink:
        env = os.environ if self._environ is None else self._environ
        home = env.get(emission.home_env, "").strip()
        if not home:
            raise NodeOverlayError(
                f"{emission.home_env} is not set (fail fast, no default): "
                "the probe's emission journal is relative to it"
            )
        return JsonlEmissionSink(Path(home) / emission.journal_relpath)

    @staticmethod
    def _conclude(chain: ChainEmitter, result: ModelProbeResult) -> None:
        accepted = [
            r.channel.value
            for r in result.receipts
            if r.mutate == EnumAttemptOutcome.MUTATION_ACCEPTED
        ]
        if accepted:
            chain.failure(
                FAILURE_MUTATION_ACCEPTED,
                "a mutation went through on: " + ", ".join(accepted),
            )
            return
        unproven = [
            f"{r.channel.value} read={r.read.value} mutate={r.mutate.value}"
            for r in result.receipts
            if not r.passed
        ]
        if unproven:
            chain.failure(FAILURE_INCONCLUSIVE, "; ".join(unproven))
            return
        chain.success(
            {
                "channels": len(result.receipts),
                "reads_ok": len(result.receipts),
                "mutations_refused": len(result.receipts),
            }
        )

    def _probe(
        self, overlay: ModelReadOnlyPrincipalOverlay, request: ModelProbeRequest
    ) -> ModelProbeResult:
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
