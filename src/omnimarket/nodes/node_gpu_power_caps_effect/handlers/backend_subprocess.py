# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real power caps backend: argv lists run as subprocesses, behind the host's transport prefix.

No local shell is involved: the overlay's words are passed as a list. A host with a transport
prefix is a remote-shell transport (an ssh invocation): the words after the prefix reach a shell
on the far side, which would re-split them. They are therefore joined into ONE quoted command
string, so each declared word arrives as the one literal word it is. The bounded runner and the
transport join come from the host storage node's backend, which owns both.
"""

from __future__ import annotations

from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    ModelGpuCard,
    ModelGpuSetRun,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsHost,
)
from omnimarket.nodes.node_host_storage_effect.handlers.backend_subprocess import (
    _run_bounded,
    _transport_argv,
)


class BackendGpuPowerCapsSubprocess:
    def __init__(
        self,
        read_argv: tuple[str, ...],
        set_argv: tuple[str, ...],
        read_timeout_seconds: float,
        set_timeout_seconds: float,
    ) -> None:
        self._read_argv = read_argv
        self._set_argv = set_argv
        self._read_timeout = read_timeout_seconds
        self._set_timeout = set_timeout_seconds

    def read_cards(
        self, host: ModelGpuPowerCapsHost
    ) -> tuple[ModelGpuCard, ...] | None:
        argv = _transport_argv(host.transport_prefix, list(self._read_argv))
        try:
            code, timed_out, out, _ = _run_bounded(argv, self._read_timeout)
        except OSError:
            return None
        if timed_out or code != 0:
            return None
        lines = [line for line in out.splitlines() if line.strip()]
        if not lines:
            return None
        cards: list[ModelGpuCard] = []
        for line in lines:
            fields = [part.strip() for part in line.split(",")]
            if len(fields) != 5:
                return None
            try:
                cards.append(
                    ModelGpuCard(
                        index=int(fields[0]),
                        name=fields[1],
                        power_limit_watts=round(float(fields[2])),
                        min_limit_watts=round(float(fields[3])),
                        max_limit_watts=round(float(fields[4])),
                    )
                )
            except ValueError:
                return None
        return tuple(cards)

    def set_limit(
        self, host: ModelGpuPowerCapsHost, index: int, watts: int
    ) -> ModelGpuSetRun:
        words = [
            word.replace("{index}", str(index)).replace("{watts}", str(watts))
            for word in self._set_argv
        ]
        argv = _transport_argv(host.transport_prefix, words)
        try:
            code, timed_out, out, err = _run_bounded(argv, self._set_timeout)
        except OSError as exc:
            return ModelGpuSetRun(exit_code=None, output_tail=type(exc).__name__)
        return ModelGpuSetRun(
            exit_code=None if timed_out else code,
            timed_out=timed_out,
            output_tail=(out + err)[-400:],
        )
