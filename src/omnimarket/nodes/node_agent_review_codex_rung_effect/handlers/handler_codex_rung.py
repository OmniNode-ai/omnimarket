# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cast one codex vote and prove where it came from.

``handle(request) -> vote`` runs the review prompt through the injected harness runner
and accepts the run only when its receipt shows the configured harness, the ChatGPT
login, a finished run and a published delegation event. The vote names the endpoint the
login reaches, the model the receipt names and the event id; the findings come from the
answer's JSON. Anything else raises ``CodexRungRefusedError`` and casts no vote, which
the dispatch counts as a missing voter, never as a pass.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from ..adapters.adapter_harness_runner import HarnessDelegateCaller
from ..models import (
    ModelCodexFinding,
    ModelCodexRungConfig,
    ModelCodexVote,
    ModelCodexVoteRequest,
    ModelHarnessRun,
)
from ..protocols import ProtocolHarnessRunner

_DONE = "done"


class CodexRungRefusedError(RuntimeError):
    """The harness run is not one this rung may count as a vote."""


def parse_findings(answer: str) -> tuple[ModelCodexFinding, ...]:
    """The findings of the answer's JSON object; anything else refuses."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise CodexRungRefusedError("the answer holds no JSON object")
    try:
        document = json.loads(answer[start : end + 1])
    except ValueError:
        raise CodexRungRefusedError("the answer's JSON does not parse") from None
    findings = document.get("findings") if isinstance(document, dict) else None
    if not isinstance(findings, list):
        raise CodexRungRefusedError("the answer has no findings list")
    try:
        return tuple(ModelCodexFinding.model_validate(item) for item in findings)
    except ValidationError as exc:
        raise CodexRungRefusedError(
            f"a finding is malformed: {exc.error_count()} error(s)"
        ) from None


def vote_from_run(
    request: ModelCodexVoteRequest, run: ModelHarnessRun, config: ModelCodexRungConfig
) -> ModelCodexVote:
    """The vote a harness run supports, or the refusal that names why it supports none."""
    harness = run.field("harness")
    if harness != config.harness:
        raise CodexRungRefusedError(f"the run used harness {harness!r}")
    login = run.field("login")
    if login != config.login:
        raise CodexRungRefusedError(f"the run used login {login!r}")
    outcome = run.field("outcome")
    if outcome != _DONE:
        detail = run.field("error", "detail") or ""
        raise CodexRungRefusedError(f"the run ended {outcome!r}: {detail}".strip())
    event_id = run.field("event", "event_id")
    if run.field("event", "published") is not True or not event_id:
        raise CodexRungRefusedError("the run published no delegation event")
    model = run.field("model")
    if not isinstance(model, str) or not model:
        raise CodexRungRefusedError("the run names no model")
    if model != request.model:
        raise CodexRungRefusedError(
            f"the run used model {model!r}, not the roster's {request.model!r}"
        )
    return ModelCodexVote(
        identity=request.identity,
        endpoint=config.endpoint,
        model=model,
        delegation_event_id=str(event_id),
        findings=parse_findings(run.answer),
    )


class HandlerAgentReviewCodexRung:
    """One codex vote per request; the runner and the config are injected."""

    def __init__(
        self,
        *,
        runner: ProtocolHarnessRunner | None = None,
        config: ModelCodexRungConfig | None = None,
    ) -> None:
        self._runner = runner
        self._config = config

    async def handle(self, request: ModelCodexVoteRequest) -> ModelCodexVote:
        config = self._config or ModelCodexRungConfig.from_contract()
        runner = self._runner if self._runner is not None else HarnessDelegateCaller()
        run = await runner.run(request.prompt, request.model, config)
        return vote_from_run(request, run, config)


__all__ = [
    "CodexRungRefusedError",
    "HandlerAgentReviewCodexRung",
    "parse_findings",
    "vote_from_run",
]
