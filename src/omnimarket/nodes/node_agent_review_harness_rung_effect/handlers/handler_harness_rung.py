# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cast one harness vote and prove where it came from.

``handle(request) -> vote`` runs the review prompt through the injected harness runner
with the rung config the request carries, and accepts the run only when its receipt shows
that config's harness and login, a finished run, a published delegation event and the
roster's model. The vote names the endpoint the rung config gives, the model the receipt
names and the event id; the findings come from the answer's JSON. The vote is unsigned:
the dispatch node signs the receipt. Anything else raises ``HarnessRungRefusedError`` and
casts no vote, which the dispatch counts as a missing voter, never as a pass.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from ..adapters.adapter_harness_runner import HarnessDelegateCaller
from ..models import (
    ModelHarnessFinding,
    ModelHarnessRun,
    ModelHarnessVote,
    ModelHarnessVoteRequest,
)
from ..protocols import ProtocolHarnessRunner

_DONE = "done"


class HarnessRungRefusedError(RuntimeError):
    """The harness run is not one this rung may count as a vote."""


def parse_findings(answer: str) -> tuple[ModelHarnessFinding, ...]:
    """The findings of the answer's JSON object; anything else refuses."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise HarnessRungRefusedError("the answer holds no JSON object")
    try:
        document = json.loads(answer[start : end + 1])
    except ValueError:
        raise HarnessRungRefusedError("the answer's JSON does not parse") from None
    findings = document.get("findings") if isinstance(document, dict) else None
    if not isinstance(findings, list):
        raise HarnessRungRefusedError("the answer has no findings list")
    try:
        return tuple(ModelHarnessFinding.model_validate(item) for item in findings)
    except ValidationError as exc:
        raise HarnessRungRefusedError(
            f"a finding is malformed: {exc.error_count()} error(s)"
        ) from None


def vote_from_run(
    request: ModelHarnessVoteRequest, run: ModelHarnessRun
) -> ModelHarnessVote:
    """The vote a harness run supports, or the refusal that names why it supports none."""
    config = request.rung
    harness = run.field("harness")
    if harness != config.harness:
        raise HarnessRungRefusedError(f"the run used harness {harness!r}")
    login = run.field("login")
    if login != config.login:
        raise HarnessRungRefusedError(f"the run used login {login!r}")
    outcome = run.field("outcome")
    if outcome != _DONE:
        detail = run.field("error", "detail") or ""
        raise HarnessRungRefusedError(f"the run ended {outcome!r}: {detail}".strip())
    event_id = run.field("event", "event_id")
    if run.field("event", "published") is not True or not event_id:
        raise HarnessRungRefusedError("the run published no delegation event")
    model = run.field("model")
    if not isinstance(model, str) or not model:
        raise HarnessRungRefusedError("the run names no model")
    if model != request.model:
        raise HarnessRungRefusedError(
            f"the run used model {model!r}, not the roster's {request.model!r}"
        )
    return ModelHarnessVote(
        identity=request.identity,
        endpoint=config.endpoint,
        model=model,
        delegation_event_id=str(event_id),
        findings=parse_findings(run.answer),
    )


class HandlerAgentReviewHarnessRung:
    """One harness vote per request; the runner is injected."""

    def __init__(self, *, runner: ProtocolHarnessRunner | None = None) -> None:
        self._runner = runner

    async def handle(self, request: ModelHarnessVoteRequest) -> ModelHarnessVote:
        runner = self._runner if self._runner is not None else HarnessDelegateCaller()
        run = await runner.run(request.prompt, request.model, request.rung)
        return vote_from_run(request, run)


__all__ = [
    "HandlerAgentReviewHarnessRung",
    "HarnessRungRefusedError",
    "parse_findings",
    "vote_from_run",
]
