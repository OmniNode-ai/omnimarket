# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostShellScripted: the scripted adapter of ProtocolHostShell for tests (OMN-19130).

Every rendered script opens with a ``# onex-step <target> <probe|act>`` line. The
adapter answers by that key from a queue of results per key; the last result of
a queue repeats. A script with no key, or a key with no queue, is refused loudly:
a test never passes on an answer nobody scripted. ``after_act`` swaps a probe's
queue once its target's action has run, so a probe reads the host as the action
left it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omnimarket.models.host_bringup.model_host_shell import (
    ModelHostShellResult,
    ModelHostTransport,
)

STEP_HEADER = "# onex-step "

OK = ModelHostShellResult(exit_code=0)
NOT_OK = ModelHostShellResult(exit_code=1)
DOWN = ModelHostShellResult(
    exit_code=255, stderr="Host key verification failed.", transport_failed=True
)


class UnscriptedCallError(AssertionError):
    """A script reached the scripted host that no test scripted an answer for."""


def step_key(script: str) -> str:
    first = script.split("\n", 1)[0]
    if not first.startswith(STEP_HEADER):
        raise UnscriptedCallError(f"script has no step header: {first!r}")
    return first[len(STEP_HEADER) :].strip()


@dataclass
class HandlerHostShellScripted:
    answers: dict[str, list[ModelHostShellResult]]
    after_act: dict[str, list[ModelHostShellResult]] = field(default_factory=dict)
    calls: list[tuple[ModelHostTransport, str, bool, int]] = field(default_factory=list)

    def run(
        self,
        transport: ModelHostTransport,
        script: str,
        *,
        privileged: bool,
        timeout_s: int,
    ) -> ModelHostShellResult:
        self.calls.append((transport, script, privileged, timeout_s))
        key = step_key(script)
        queue = self.answers.get(key)
        if not queue:
            raise UnscriptedCallError(f"no scripted answer for {key}")
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        target, _, kind = key.partition(" ")
        if kind == "act" and result.exit_code == 0 and not result.transport_failed:
            swap = self.after_act.get(target)
            if swap is not None:
                self.answers[f"{target} probe"] = list(swap)
        return result


__all__ = [
    "DOWN",
    "NOT_OK",
    "OK",
    "STEP_HEADER",
    "HandlerHostShellScripted",
    "UnscriptedCallError",
    "step_key",
]
