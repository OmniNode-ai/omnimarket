# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One run of the architecture handshake policy gate (OMN-20671).

The retired check-policy-gate.sh read GitHub, decided and printed in one loop. Here the loop
is this handler's and every decision is node_handshake_policy_gate_compute's: the handler reads
what the compute node names, passes back what it saw and does what it answers (retry after a
sleep, record a status, print the report). Output, exit code, endpoints read and sleeps taken
equal the script's on the captured cases (tests/fixtures/handshake_policy_gate_parity.json).
"""

from __future__ import annotations

from omnimarket.models.model_handshake_policy_gate import (
    EnumPolicyGateDecisionKind,
    EnumReadOutcome,
    ModelPolicyGateDecisionRequest,
    ModelPolicyGateDecisionResult,
    ModelRepoGateStatus,
)
from omnimarket.nodes.node_handshake_policy_gate_compute.handlers.handler_handshake_policy_gate import (
    HandlerHandshakePolicyGate,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.models.model_handshake_policy_gate_run import (
    ModelPolicyGateRunRequest,
    ModelPolicyGateRunResult,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.protocols.protocol_handshake_policy_gate_run import (
    ProtocolPolicyGateReader,
    ProtocolPolicyGateSleeper,
)

_EMPTY_CONF_STDERR = "ERROR: repos.conf contains no repo entries\n"


class HandlerHandshakePolicyGateRun:
    """EFFECT: check every active repo's latest handshake run and report the verdict."""

    def __init__(
        self,
        reader: ProtocolPolicyGateReader | None = None,
        sleeper: ProtocolPolicyGateSleeper | None = None,
    ) -> None:
        if reader is None or sleeper is None:
            from ..protocols.local_handshake_policy_gate_adapters import (
                GitHubPolicyGateReader,
                TimePolicyGateSleeper,
                resolve_policy_gate_token,
            )

            reader = reader or GitHubPolicyGateReader(resolve_policy_gate_token())
            sleeper = sleeper or TimePolicyGateSleeper()
        self._reader = reader
        self._sleeper = sleeper
        self._decide = HandlerHandshakePolicyGate()

    def _ask(self, **fields: object) -> ModelPolicyGateDecisionResult:
        return self._decide.handle(
            ModelPolicyGateDecisionRequest.model_validate(fields)
        )

    def handle(self, request: ModelPolicyGateRunRequest) -> ModelPolicyGateRunResult:
        try:
            parsed = self._ask(
                kind=EnumPolicyGateDecisionKind.PARSE_REPOS,
                repos_conf_text=request.repos_conf_text,
            )
        except ValueError:
            return ModelPolicyGateRunResult(
                stdout="", stderr=_EMPTY_CONF_STDERR, exit_code=2
            )
        assert parsed.repos is not None
        assert parsed.info_line is not None
        stderr = [parsed.info_line]
        endpoints: list[str] = []
        sleeps: list[int] = []
        statuses: list[ModelRepoGateStatus] = []
        for repo in parsed.repos:
            plan = self._ask(
                kind=EnumPolicyGateDecisionKind.RESOLVE_BRANCH,
                repo=repo,
                default_branch_output=self._read_branch(repo, endpoints),
            )
            assert plan.runs_endpoint is not None
            if plan.info_line:
                stderr.append(plan.info_line)
            attempt = 1
            while True:
                endpoints.append(plan.runs_endpoint)
                read = self._reader.latest_run(plan.runs_endpoint)
                step = self._ask(
                    kind=EnumPolicyGateDecisionKind.CLASSIFY_READ,
                    repo=repo,
                    attempt=attempt,
                    max_attempts_raw=request.max_attempts_raw,
                    base_delay_raw=request.base_delay_raw,
                    api_ok=read.api_ok,
                    api_error_text=read.api_error_text,
                    total_count=read.total_count,
                    conclusion=read.conclusion,
                )
                if step.outcome is EnumReadOutcome.RETRY:
                    assert step.info_line is not None
                    assert step.retry_delay_seconds is not None
                    assert step.next_attempt is not None
                    stderr.append(step.info_line)
                    sleeps.append(step.retry_delay_seconds)
                    self._sleeper.sleep(step.retry_delay_seconds)
                    attempt = step.next_attempt
                    continue
                assert step.outcome is not None
                statuses.append(
                    ModelRepoGateStatus(repo=repo, status=step.outcome.value)
                )
                break
        report = self._ask(
            kind=EnumPolicyGateDecisionKind.REPORT,
            statuses=statuses,
            strict=request.strict,
        )
        assert report.report_text is not None
        assert report.exit_code is not None
        return ModelPolicyGateRunResult(
            stdout=report.report_text,
            stderr="".join(f"{line}\n" for line in stderr),
            exit_code=report.exit_code,
            verdict=report.verdict,
            endpoints=endpoints,
            sleeps=sleeps,
        )

    def _read_branch(self, repo: str, endpoints: list[str]) -> str:
        # The compute node names the repo endpoint whatever the lookup returns, so ask it
        # for the endpoint before the read; the real lookup output is passed on afterwards.
        endpoint = self._ask(
            kind=EnumPolicyGateDecisionKind.RESOLVE_BRANCH,
            repo=repo,
            default_branch_output="",
        ).default_branch_endpoint
        assert endpoint is not None
        endpoints.append(endpoint)
        return self._reader.default_branch(endpoint)
